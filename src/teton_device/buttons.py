# The device's only input: single keypresses acting as physical buttons ([P] pairing,
# [R][R] reset, [S] status, [Q] power), and its status "LED". See SPEC §9.1, DESIGN §4.1.
#
# No text is ever collected: every key that isn't a button is dropped as it arrives
# (plan_v5 D2). Time comes from an injected clock and sleep, so the pairing window, the
# reset double press and the LED are all tested without a terminal or real waiting.

import asyncio
import contextlib
import math
import os
import sys
import termios
import time
import tty
from collections.abc import AsyncGenerator, Awaitable, Callable
from enum import StrEnum
from typing import Protocol, TextIO

import structlog

from teton_common.protocol import DeviceState, FailureReason

log = structlog.get_logger()

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]

PAIRING_WINDOW_S = 300  # SPEC §4, §9.1
RESET_WITHIN_S = 3  # two presses this close together = a long press
TICK_S = 1  # countdown redraw, window expiry, session idle check

MSG_ALREADY_CONNECTED = "Already connected. Hold reset to set up again."
MSG_RESET_CONFIRM = "Press reset again to confirm."
MSG_BUSY = "Busy connecting. Try again in a moment."  # reset refused while CONNECTING (Q21)


class Button(StrEnum):
    PAIR = "pair"
    RESET = "reset"
    STATUS = "status"
    POWER = "power"


KEYS = {"p": Button.PAIR, "r": Button.RESET, "s": Button.STATUS, "q": Button.POWER}


def key_to_button(key: str) -> Button | None:
    """Case-insensitive. Every other key means nothing."""
    return KEYS.get(key.lower())


ESC = 0x1B


def buttons_in(data: bytes) -> list[Button]:
    """The buttons in what the terminal sent, in order.

    Keys like arrows, F1 (ESC O P) or Alt+p (ESC p) arrive as escape sequences that contain
    button letters, so each sequence is skipped as a whole: ESC [ <parameters> <final byte>,
    ESC O <one byte>, or ESC <one key>. Everything else that isn't a button is dropped.
    """
    buttons = []
    i = 0
    while i < len(data):
        if data[i] != ESC:
            button = key_to_button(chr(data[i]))  # only ASCII letters can match
            if button is not None:
                buttons.append(button)
            i += 1
            continue
        i += 1  # the ESC
        if i < len(data) and data[i] == ord("["):  # CSI: arrows, F5 and up, ...
            i += 1
            while i < len(data) and not 0x40 <= data[i] <= 0x7E:
                i += 1  # parameter bytes
        elif i < len(data) and data[i] == ord("O"):  # SS3: F1-F4
            i += 1
        i += 1  # the final byte, or the key after ESC (Alt+key)
    return buttons


# ---------------------------------------------------------------------------
# Button sources


class ButtonSource(Protocol):
    # An async generator, not just an iterator: aclose() lets the controller always run the
    # source's cleanup (TerminalButtons restores the terminal there).
    def __aiter__(self) -> AsyncGenerator[Button, None]: ...


class QueueButtons:
    """In-memory ButtonSource for tests: press() a button, close() to end the stream."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[Button | None] = asyncio.Queue()

    def press(self, *buttons: Button) -> None:
        for button in buttons:
            self._queue.put_nowait(button)

    def close(self) -> None:
        self._queue.put_nowait(None)

    async def __aiter__(self) -> AsyncGenerator[Button, None]:
        while (button := await self._queue.get()) is not None:
            yield button


class TerminalButtons:
    """Single keys from the terminal, without Enter.

    cbreak mode, not raw, so Ctrl+C still sends SIGINT. The original terminal settings are
    restored in a `finally`, so also after a crash or a cancel. Without a terminal (systemd,
    redirected stdin) there are no buttons: it logs a warning and yields nothing (Q23).
    """

    def __init__(self, fd: int | None = None) -> None:
        self._fd = fd  # None: stdin, looked up only when iteration starts

    async def __aiter__(self) -> AsyncGenerator[Button, None]:
        fd = sys.stdin.fileno() if self._fd is None else self._fd
        if not os.isatty(fd):
            log.warning("no_terminal", detail="no buttons; a real device would use GPIO")
            return
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Button | None] = asyncio.Queue()
        saved = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            loop.add_reader(fd, self._on_readable, fd, queue)
            while (button := await queue.get()) is not None:
                yield button
        finally:
            loop.remove_reader(fd)
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    @staticmethod
    def _on_readable(fd: int, queue: "asyncio.Queue[Button | None]") -> None:
        data = os.read(fd, 64)
        if not data:  # end of input: no more buttons
            asyncio.get_running_loop().remove_reader(fd)
            queue.put_nowait(None)
            return
        for button in buttons_in(data):
            queue.put_nowait(button)  # the only thing kept: never the text itself


# ---------------------------------------------------------------------------
# Pure helpers with an injected clock


class PairingWindow:
    """The 300 s window opened by [P], the only time the device advertises (plan_v5 D2).
    Not a device state: the service owns one and shares it with the session (Q22)."""

    def __init__(self, clock: Clock = time.monotonic, duration_s: float = PAIRING_WINDOW_S):
        self._clock = clock
        self._duration_s = duration_s
        self._closes_at: float | None = None

    def open(self) -> None:
        """Open, or restart at the full duration."""
        self._closes_at = self._clock() + self._duration_s

    def close(self) -> None:
        self._closes_at = None

    def is_open(self) -> bool:
        return self._closes_at is not None and self._clock() < self._closes_at

    def remaining(self) -> float:
        if self._closes_at is None:
            return 0.0
        return max(0.0, self._closes_at - self._clock())


class ResetDetector:
    """[R] twice within 3 s stands in for a long press."""

    def __init__(self, clock: Clock = time.monotonic, within_s: float = RESET_WITHIN_S):
        self._clock = clock
        self._within_s = within_s
        self._armed_at: float | None = None

    def press(self) -> bool:
        """True on the second press within the limit. Otherwise this press arms it."""
        now = self._clock()
        if self._armed_at is not None and now - self._armed_at <= self._within_s:
            self._armed_at = None
            return True
        self._armed_at = now
        return False


def format_countdown(seconds: float) -> str:
    """299.2 -> '5:00' (rounded up, so it never shows 0:00 while still open)."""
    whole = math.ceil(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


# ---------------------------------------------------------------------------
# The LED and the status line


class Led(StrEnum):
    OFF = "off"
    BLINKING_BLUE = "blinking blue"
    YELLOW = "yellow"
    GREEN = "green"
    RED = "red"


def led_for(state: DeviceState, window_open: bool, last_failure: FailureReason | None) -> Led:
    """DESIGN §4.1 priority: yellow > green > red > blinking blue > off."""
    if state == DeviceState.CONNECTING:
        return Led.YELLOW
    if state == DeviceState.PROVISIONED:
        return Led.GREEN
    if last_failure is not None:
        return Led.RED
    if window_open:
        return Led.BLINKING_BLUE
    return Led.OFF


_COLOURS = {Led.BLINKING_BLUE: "34", Led.YELLOW: "33", Led.GREEN: "32", Led.RED: "31"}
_CLEAR_LINE = "\r\x1b[2K"


class StatusLine:
    """The "LED": one line kept at the bottom of a terminal.

    On a terminal it is redrawn in place and every other line (messages, and log lines if
    logging writes here) is printed above it. On a pipe or file (e.g. `| tee` for
    evidence) there are no escape codes: only LED changes, as ordinary lines.

    Log lines written here go to `log_stream` (the CLI passes stderr, review Q73): the LED
    line is cleared, the log line written, and the LED redrawn, so `2> file` keeps the logs
    apart from the screen, which shows the setup code.
    """

    def __init__(self, stream: TextIO | None = None, log_stream: TextIO | None = None) -> None:
        self._stream = stream or sys.stdout
        self._log = log_stream or self._stream
        self._tty = self._stream.isatty()
        self._line = ""  # what is drawn now (terminal)
        self._last_change: tuple[Led, str] | None = None  # what was printed last (pipe)
        self._pending = ""  # write() text waiting for its newline

    def show(self, led: Led, detail: str = "", countdown_s: float | None = None) -> None:
        if self._tty:
            dot = "●"
            if led == Led.BLINKING_BLUE and countdown_s is not None and math.ceil(countdown_s) % 2:
                dot = "○"  # alternates every second: the blink
            if led in _COLOURS:
                dot = f"\x1b[{_COLOURS[led]}m{dot}\x1b[0m"
            text = f"{dot} LED {led}"
            if countdown_s is not None:
                text += f" {format_countdown(countdown_s)}"
            if detail:
                text += f"  {detail}"
            self._line = text
            self._stream.write(_CLEAR_LINE + text)
            self._stream.flush()
        elif self._last_change != (led, detail):  # no countdown ticks on a pipe
            self._last_change = (led, detail)
            self._print(f"LED {led}" + (f": {detail}" if detail else ""))

    def message(self, text: str) -> None:
        """Print text above the status line."""
        for line in text.splitlines() or [""]:
            self._print(line)

    def end(self) -> None:
        """At power off: move below the LED line, so the shell prompt doesn't overwrite it."""
        if self._tty and self._line:
            self._stream.write("\n")
            self._stream.flush()
            self._line = ""

    # A file-like interface, so logging can write here and stay above the status line.
    def write(self, text: str) -> int:
        self._pending += text
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._print_log(line)
        return len(text)

    def flush(self) -> None:
        self._log.flush()

    def _print(self, line: str) -> None:
        if self._tty:
            self._stream.write(_CLEAR_LINE + line + "\n" + self._line)  # then redraw the LED
        else:
            self._stream.write(line + "\n")
        self._stream.flush()

    def _print_log(self, line: str) -> None:
        if self._log is self._stream:
            self._print(line)
            return
        if self._tty:
            self._stream.write(_CLEAR_LINE)  # the LED line, out of the way
            self._stream.flush()
        self._log.write(line + "\n")
        self._log.flush()
        if self._tty:
            self._stream.write(self._line)  # and back
            self._stream.flush()


# ---------------------------------------------------------------------------
# The controller


class DeviceActions(Protocol):
    """What the buttons may do to the device (implemented by DeviceService)."""

    def state(self) -> DeviceState: ...

    def last_failure(self) -> FailureReason | None: ...  # red LED until the next attempt

    async def set_advertising(self, on: bool) -> None: ...  # window opened, or closed/expired

    async def reset(self) -> None: ...  # forget profiles, rotate code, print code + QR

    def status_text(self) -> str: ...  # state, SSID, IP, internet, code + QR (Q23)

    async def power_off(self) -> None: ...

    def on_tick(self) -> None: ...  # every second: e.g. the session idle check


class ButtonController:
    """Turns button presses and the passing of time into DeviceActions calls."""

    def __init__(
        self,
        buttons: ButtonSource,
        actions: DeviceActions,
        window: PairingWindow,
        status_line: StatusLine,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._buttons = buttons
        self._actions = actions
        self._window = window
        self._status = status_line
        self._reset = ResetDetector(clock)
        self._sleep = sleep
        self._advertising = False  # what we last asked for, so each change is sent once

    async def run(self) -> None:
        """Until [Q]. With no buttons at all (no terminal) it keeps ticking: the device
        stays on and only Ctrl+C or SIGTERM stops it."""
        ticker = asyncio.create_task(self._tick_forever())
        try:
            async with contextlib.aclosing(aiter(self._buttons)) as presses:
                async for button in presses:
                    if await self.handle(button):
                        return
            await ticker  # the buttons ended, the device doesn't
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker

    async def handle(self, button: Button) -> bool:
        """Act on one press. Returns True when the device powers off."""
        log.info("button", button=button)
        if button == Button.PAIR:
            await self._pair()
        elif button == Button.RESET:
            await self._press_reset()
        elif button == Button.STATUS:
            self._status.message(self._actions.status_text() + "\n" + self._window_text())
        elif button == Button.POWER:
            await self._actions.power_off()
            return True
        await self.tick()
        return False

    async def tick(self) -> None:
        """Close an expired window, let the device do its periodic work, redraw the LED."""
        if self._advertising and not self._window.is_open():
            self._advertising = False
            log.info("pairing_window_closed")
            await self._actions.set_advertising(False)
        self._actions.on_tick()
        self._draw()

    async def _pair(self) -> None:
        if self._actions.state() == DeviceState.PROVISIONED:
            self._status.message(MSG_ALREADY_CONNECTED)
            return
        restart = self._window.is_open()
        self._window.open()
        self._status.message(
            f"Pairing window {'restarted' if restart else 'open'}: "
            f"{format_countdown(self._window.remaining())}."
        )
        if not self._advertising:
            self._advertising = True
            await self._actions.set_advertising(True)

    async def _press_reset(self) -> None:
        if not self._reset.press():
            self._status.message(MSG_RESET_CONFIRM)
            return
        if self._actions.state() == DeviceState.CONNECTING:
            self._status.message(MSG_BUSY)  # a started attempt always finishes (Q21)
            return
        # Close the window first, so the reset can't leave the device advertising.
        self._window.close()
        self._advertising = False
        await self._actions.reset()

    def _window_text(self) -> str:
        if not self._window.is_open():
            return "Pairing window: closed"
        return f"Pairing window: open, {format_countdown(self._window.remaining())} left"

    def _draw(self) -> None:
        window_open = self._window.is_open()
        failure = self._actions.last_failure()
        led = led_for(self._actions.state(), window_open, failure)
        detail = f"last attempt failed: {failure}" if led == Led.RED else ""
        countdown = self._window.remaining() if led == Led.BLINKING_BLUE else None
        self._status.show(led, detail, countdown)

    async def _tick_forever(self) -> None:
        while True:
            await self._sleep(TICK_S)
            await self.tick()
