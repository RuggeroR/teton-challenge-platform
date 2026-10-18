# Tests for the device buttons and status line: key mapping, the pairing window, the reset
# double press, [P]/[S]/[Q], the LED priority, the status line on a terminal and on a pipe,
# and the terminal key reader on a pseudo-terminal. Injected buttons, a fake clock and a
# fake DeviceActions: no service, no transport, no real keyboard.

import asyncio
import io
import os
import termios
from collections.abc import Iterator

import pytest
from structlog.testing import capture_logs

from teton_common.protocol import DeviceState, FailureReason
from teton_device.buttons import (
    MSG_ALREADY_CONNECTED,
    MSG_BUSY,
    MSG_RESET_CONFIRM,
    Button,
    ButtonController,
    Led,
    PairingWindow,
    QueueButtons,
    ResetDetector,
    StatusLine,
    TerminalButtons,
    buttons_in,
    format_countdown,
    key_to_button,
    led_for,
)

S = DeviceState


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeActions:
    """Records what the buttons asked the device to do."""

    def __init__(self) -> None:
        self.current = S.UNPROVISIONED
        self.failure: FailureReason | None = None
        self.advertising: list[bool] = []
        self.resets = 0
        self.powered_off = False
        self.ticks = 0

    def state(self) -> DeviceState:
        return self.current

    def last_failure(self) -> FailureReason | None:
        return self.failure

    async def set_advertising(self, on: bool) -> None:
        self.advertising.append(on)

    async def reset(self) -> None:
        self.resets += 1

    def status_text(self) -> str:
        return "State: unprovisioned\nSSID: -"

    async def power_off(self) -> None:
        self.powered_off = True

    def on_tick(self) -> None:
        self.ticks += 1


class Rig:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.actions = FakeActions()
        self.out = io.StringIO()  # not a terminal: plain lines
        self.window = PairingWindow(self.clock)
        self.controller = ButtonController(
            QueueButtons(), self.actions, self.window, StatusLine(self.out), self.clock
        )

    async def press(self, *buttons: Button) -> None:
        for button in buttons:
            await self.controller.handle(button)

    async def wait(self, seconds: int) -> None:
        """Let time pass, one tick per second, like the real loop."""
        for _ in range(seconds):
            self.clock.advance(1)
            await self.controller.tick()

    def lines(self) -> list[str]:
        return self.out.getvalue().splitlines()


@pytest.fixture
def rig() -> Rig:
    return Rig()


# --- keys -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "button"),
    [
        ("p", Button.PAIR),
        ("P", Button.PAIR),
        ("r", Button.RESET),
        ("R", Button.RESET),
        ("s", Button.STATUS),
        ("q", Button.POWER),
        ("Q", Button.POWER),
    ],
)
def test_key_mapping_is_case_insensitive(key: str, button: Button) -> None:
    assert key_to_button(key) == button


@pytest.mark.parametrize("key", ["x", "1", " ", "\n", "\x1b", "é", "pq"])
def test_other_keys_mean_nothing(key: str) -> None:
    assert key_to_button(key) is None


@pytest.mark.parametrize(
    ("data", "buttons"),
    [
        (b"p", [Button.PAIR]),
        (b"rR", [Button.RESET, Button.RESET]),
        (b"help", [Button.PAIR]),  # typed text: only its button keys count
        (b"\x1bOP", []),  # F1
        (b"\x1bOS", []),  # F4
        (b"\x1b[A", []),  # arrow up
        (b"\x1b[15~", []),  # F5
        (b"\x1b[1;5P", []),  # Ctrl+F1
        (b"\x1bp", []),  # Alt+p
        (b"\x1b", []),  # Esc alone
        (b"\x1bOPs", [Button.STATUS]),  # a key right after a sequence still counts
        ("é".encode(), []),
    ],
)
def test_buttons_in_terminal_input(data: bytes, buttons: list[Button]) -> None:
    assert buttons_in(data) == buttons


# --- pairing window ---------------------------------------------------------------------


async def test_pair_opens_the_window_and_turns_advertising_on(rig: Rig) -> None:
    await rig.press(Button.PAIR)
    assert rig.window.is_open()
    assert rig.actions.advertising == [True]
    assert "Pairing window open: 5:00." in rig.lines()


async def test_nothing_is_advertised_before_pair(rig: Rig) -> None:
    await rig.wait(400)
    assert rig.actions.advertising == []


async def test_window_expiry_turns_advertising_off_once(rig: Rig) -> None:
    await rig.press(Button.PAIR)
    await rig.wait(299)
    assert rig.actions.advertising == [True]
    await rig.wait(1)
    assert rig.actions.advertising == [True, False]
    await rig.wait(60)
    assert rig.actions.advertising == [True, False]  # not repeated on every tick


async def test_pair_again_restarts_the_window(rig: Rig) -> None:
    await rig.press(Button.PAIR)
    await rig.wait(200)
    await rig.press(Button.PAIR)
    assert "Pairing window restarted: 5:00." in rig.lines()
    await rig.wait(200)  # 400 s after the first press
    assert rig.window.is_open()
    assert rig.actions.advertising == [True]  # still on, never switched twice
    await rig.wait(100)  # 300 s after the second press
    assert rig.actions.advertising == [True, False]


async def test_pair_on_a_provisioned_device_only_shows_a_message(rig: Rig) -> None:
    rig.actions.current = S.PROVISIONED
    await rig.press(Button.PAIR)
    assert MSG_ALREADY_CONNECTED in rig.lines()
    assert rig.actions.advertising == []
    assert not rig.window.is_open()


def test_pairing_window_helper() -> None:
    clock = FakeClock()
    window = PairingWindow(clock, duration_s=300)
    assert not window.is_open() and window.remaining() == 0
    window.open()
    clock.advance(120)
    assert window.is_open() and window.remaining() == 180
    window.close()
    assert not window.is_open() and window.remaining() == 0


@pytest.mark.parametrize(
    ("seconds", "text"), [(300, "5:00"), (299.2, "5:00"), (61, "1:01"), (9, "0:09"), (0.4, "0:01")]
)
def test_countdown_format(seconds: float, text: str) -> None:
    assert format_countdown(seconds) == text


# --- reset (double press) ---------------------------------------------------------------


async def test_one_reset_press_only_asks_for_confirmation(rig: Rig) -> None:
    await rig.press(Button.RESET)
    assert MSG_RESET_CONFIRM in rig.lines()
    assert rig.actions.resets == 0


async def test_two_presses_within_3_s_reset(rig: Rig) -> None:
    rig.actions.current = S.PROVISIONED
    await rig.press(Button.RESET)
    rig.clock.advance(3)
    await rig.press(Button.RESET)
    assert rig.actions.resets == 1


async def test_two_presses_4_s_apart_do_not_reset(rig: Rig) -> None:
    await rig.press(Button.RESET)
    rig.clock.advance(4)
    await rig.press(Button.RESET)  # this one arms it again
    assert rig.actions.resets == 0
    assert rig.lines().count(MSG_RESET_CONFIRM) == 2


async def test_a_third_quick_press_does_not_reset_again(rig: Rig) -> None:
    await rig.press(Button.RESET, Button.RESET, Button.RESET)
    assert rig.actions.resets == 1


@pytest.mark.parametrize("state", [S.UNPROVISIONED, S.ADVERTISING, S.SESSION_ESTABLISHED])
async def test_reset_works_outside_provisioned_and_closes_the_window(
    rig: Rig, state: DeviceState
) -> None:
    await rig.press(Button.PAIR)
    rig.actions.current = state
    await rig.press(Button.RESET, Button.RESET)  # Q21
    assert rig.actions.resets == 1
    assert not rig.window.is_open()
    await rig.wait(400)
    assert rig.actions.advertising == [True]  # the reset itself turns advertising off


async def test_reset_is_refused_while_connecting(rig: Rig) -> None:
    rig.actions.current = S.CONNECTING
    await rig.press(Button.RESET, Button.RESET)
    assert MSG_BUSY in rig.lines()  # Q21: a started attempt always finishes
    assert rig.actions.resets == 0


def test_reset_detector_helper() -> None:
    clock = FakeClock()
    detector = ResetDetector(clock, within_s=3)
    assert not detector.press()
    clock.advance(2.9)
    assert detector.press()
    assert not detector.press()  # starts over


# --- status and power -------------------------------------------------------------------


async def test_status_prints_the_device_text_and_the_window(rig: Rig) -> None:
    await rig.press(Button.STATUS)
    assert rig.lines()[:3] == ["State: unprovisioned", "SSID: -", "Pairing window: closed"]
    await rig.press(Button.PAIR)
    await rig.wait(40)
    await rig.press(Button.STATUS)
    assert "Pairing window: open, 4:20 left" in rig.lines()


async def test_power_calls_power_off_and_stops(rig: Rig) -> None:
    assert await rig.controller.handle(Button.POWER) is True
    assert rig.actions.powered_off


async def test_every_tick_reaches_the_device(rig: Rig) -> None:
    await rig.wait(5)
    assert rig.actions.ticks == 5


async def test_run_handles_presses_until_power(rig: Rig) -> None:
    buttons = QueueButtons()
    controller = ButtonController(buttons, rig.actions, rig.window, StatusLine(rig.out), rig.clock)
    buttons.press(Button.PAIR, Button.STATUS, Button.POWER, Button.PAIR)
    await asyncio.wait_for(controller.run(), timeout=1)
    assert rig.actions.advertising == [True] and rig.actions.powered_off  # last P never read


async def test_run_keeps_ticking_when_there_are_no_buttons(rig: Rig) -> None:
    # No terminal (Q23): the device must stay on, not exit.
    ticks = 0

    async def fake_sleep(_: float) -> None:
        nonlocal ticks
        ticks += 1
        await asyncio.sleep(0)

    buttons = QueueButtons()
    buttons.close()
    controller = ButtonController(
        buttons, rig.actions, rig.window, StatusLine(rig.out), rig.clock, sleep=fake_sleep
    )
    task = asyncio.create_task(controller.run())
    for _ in range(50):
        await asyncio.sleep(0)
    assert not task.done() and ticks > 3
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- LED --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "window_open", "failure", "led"),
    [
        (S.UNPROVISIONED, False, None, Led.OFF),
        (S.UNPROVISIONED, True, None, Led.BLINKING_BLUE),
        (S.ADVERTISING, True, None, Led.BLINKING_BLUE),
        (S.SESSION_ESTABLISHED, True, None, Led.BLINKING_BLUE),
        (S.SESSION_ESTABLISHED, False, None, Led.OFF),
        (S.SESSION_ESTABLISHED, True, FailureReason.WRONG_PASSWORD, Led.RED),  # red > blue
        (S.UNPROVISIONED, False, FailureReason.TIMEOUT, Led.RED),
        (S.CONNECTING, True, FailureReason.WRONG_PASSWORD, Led.YELLOW),  # yellow > everything
        (S.PROVISIONED, False, None, Led.GREEN),
        (S.PROVISIONED, True, FailureReason.WRONG_PASSWORD, Led.GREEN),  # green > red, blue
    ],
)
def test_led_priority(
    state: DeviceState, window_open: bool, failure: FailureReason | None, led: Led
) -> None:
    assert led_for(state, window_open, failure) == led


async def test_led_follows_the_device(rig: Rig) -> None:
    await rig.press(Button.PAIR)
    rig.actions.current = S.CONNECTING
    await rig.wait(1)
    rig.actions.current, rig.actions.failure = S.SESSION_ESTABLISHED, FailureReason.NO_IP
    await rig.wait(1)
    rig.actions.current, rig.actions.failure = S.PROVISIONED, None
    await rig.wait(1)
    leds = [line for line in rig.lines() if line.startswith("LED")]
    assert leds == [
        "LED blinking blue",
        "LED yellow",
        "LED red: last attempt failed: no_ip",
        "LED green",
    ]  # on a pipe: one line per change, no countdown ticks


# --- status line ------------------------------------------------------------------------


class FakeTerminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_status_line_on_a_pipe_has_no_escape_codes() -> None:
    out = io.StringIO()
    line = StatusLine(out)
    line.show(Led.BLINKING_BLUE, countdown_s=299)
    line.show(Led.BLINKING_BLUE, countdown_s=298)  # countdown only: nothing new
    line.message("hello")
    assert out.getvalue() == "LED blinking blue\nhello\n"
    assert "\x1b" not in out.getvalue()


def test_status_line_on_a_terminal_redraws_in_place() -> None:
    out = FakeTerminal()
    line = StatusLine(out)
    line.show(Led.BLINKING_BLUE, countdown_s=299)
    line.show(Led.BLINKING_BLUE, countdown_s=298)
    text = out.getvalue()
    assert text.count("\r\x1b[2K") == 2 and "\n" not in text  # same line, twice
    assert "4:59" in text and "4:58" in text
    assert "\x1b[34m" in text  # blue


def test_messages_and_log_lines_go_above_the_status_line() -> None:
    out = FakeTerminal()
    line = StatusLine(out)
    line.show(Led.GREEN)
    out.seek(0)
    out.truncate()
    line.write('{"event": "connected"}')  # print() writes the text, then "\n"
    line.write("\n")
    assert out.getvalue().startswith('\r\x1b[2K{"event": "connected"}\n')
    assert out.getvalue().endswith("LED green")  # redrawn underneath


# --- the terminal key reader ------------------------------------------------------------


@pytest.fixture
def pty() -> Iterator[tuple[int, int]]:
    controller, terminal = os.openpty()
    yield controller, terminal
    os.close(controller)
    os.close(terminal)


async def test_terminal_buttons_read_single_keys_and_restore_the_terminal(
    pty: tuple[int, int],
) -> None:
    controller, terminal = pty
    before = termios.tcgetattr(terminal)
    presses = aiter(TerminalButtons(terminal))
    first = asyncio.ensure_future(anext(presses))
    await asyncio.sleep(0)
    during = termios.tcgetattr(terminal)
    assert not during[3] & termios.ICANON  # single keys, no Enter needed
    assert not during[3] & termios.ECHO  # keys aren't printed over the status line
    assert during[3] & termios.ISIG  # cbreak, not raw: Ctrl+C still works

    os.write(controller, b"help-xPq")  # typed text yields only its button keys: p, P, q
    assert await asyncio.wait_for(first, 1) == Button.PAIR
    assert await asyncio.wait_for(anext(presses), 1) == Button.PAIR
    assert await asyncio.wait_for(anext(presses), 1) == Button.POWER
    await presses.aclose()
    assert termios.tcgetattr(terminal) == before


async def test_terminal_buttons_skip_escape_sequences(pty: tuple[int, int]) -> None:
    controller, terminal = pty
    presses = aiter(TerminalButtons(terminal))
    nxt = asyncio.ensure_future(anext(presses))
    await asyncio.sleep(0)
    os.write(controller, b"\x1bOPs")  # F1 (ESC O P) is not [P]; the s after it is [S]
    assert await asyncio.wait_for(nxt, 1) == Button.STATUS
    await presses.aclose()


async def test_terminal_is_restored_after_a_crash(pty: tuple[int, int], rig: Rig) -> None:
    controller, terminal = pty
    before = termios.tcgetattr(terminal)

    def broken_status() -> str:
        raise RuntimeError("bug while handling a button")

    rig.actions.status_text = broken_status  # type: ignore[method-assign]
    buttons = ButtonController(
        TerminalButtons(terminal), rig.actions, rig.window, StatusLine(rig.out), rig.clock
    )
    os.write(controller, b"s")
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(buttons.run(), 1)
    assert termios.tcgetattr(terminal) == before  # restored at once, not at garbage collection


async def test_without_a_terminal_there_are_no_buttons() -> None:
    read_end, write_end = os.pipe()
    try:
        with capture_logs() as logs:
            presses = [b async for b in TerminalButtons(read_end)]
        assert presses == []
        assert logs[0]["event"] == "no_terminal"
    finally:
        os.close(read_end)
        os.close(write_end)
