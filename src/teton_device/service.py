# Orchestrator: loads state, prints setup code + QR, starts the transport without
# advertising, and ties the session, the state machine, the pairing window and the buttons
# together until [Q]. See SPEC §9.1, DESIGN §4.
#
# Who owns what:
#   ButtonController   the pairing window and the reset double press (buttons.py)
#   DeviceSession      the protocol, and the transitions a configurator causes (session.py)
#   DeviceService      the radio (advertising on/off), reset, power off, info, the screen
# No connection tracking and no `busy`: the radio allows one configurator at a time
# (plan_v5 S1, S3).

import asyncio
import time
from collections.abc import Awaitable, Callable

import structlog

from teton_common.messages import Info, encode
from teton_common.protocol import DeviceState, FailureReason
from teton_common.transport import DeviceTransport, FramedTransport, TransportError
from teton_device import qr
from teton_device.buttons import ButtonController, ButtonSource, PairingWindow, StatusLine
from teton_device.session import DeviceSession, HandshakeLimiter
from teton_device.state import StateMachine
from teton_device.storage import Storage
from teton_device.wifi.backend import WifiBackend

log = structlog.get_logger()

S = DeviceState


class DeviceService:
    """The Teton Device. Implements DeviceActions for the buttons."""

    def __init__(
        self,
        transport: DeviceTransport,
        wifi: WifiBackend,
        storage: Storage,
        buttons: ButtonSource,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        status_line: StatusLine | None = None,
    ) -> None:
        self._transport = transport
        self._wifi = wifi
        self._storage = storage
        self._buttons = buttons
        self._clock = clock
        self._sleep = sleep
        self._screen = status_line or StatusLine()
        self._window = PairingWindow(clock)
        self._advertising = False
        self._closed = False
        # Built in run(), once the stored state is loaded.
        self._state = StateMachine()
        self._session: DeviceSession | None = None

    async def run(self) -> None:
        """Until [Q] (or Ctrl+C, which cancels this). The transport is always closed."""
        stored = self._storage.load_or_create()
        structlog.contextvars.bind_contextvars(device_id=stored.device_id)
        self._state = StateMachine.at_start(stored.provisioned, on_change=self._publish_info)
        self._session = DeviceSession(
            FramedTransport(self._transport, self._clock),
            self._wifi,
            self._storage,
            self._state,
            HandshakeLimiter(self._clock),
            window_open=self._window.is_open,
            on_provisioned=self._provisioned,
            clock=self._clock,
        )
        self._show_code()
        self._publish_info(self._state.state)
        await self._transport.start()  # the service exists, but nothing is advertised (D2)
        log.info("device_started", state=self._state.state)
        controller = ButtonController(
            self._buttons, self, self._window, self._screen, self._clock, self._sleep
        )
        try:
            await controller.run()
        finally:
            await self._close()

    # --- DeviceActions (called by the buttons) -------------------------------------------

    def state(self) -> DeviceState:
        return self._state.state

    def last_failure(self) -> FailureReason | None:
        last = self._session.last_result if self._session else None
        return last[1].reason if last is not None and not last[1].ok else None

    async def set_advertising(self, on: bool) -> None:
        """The pairing window opened, or closed/expired."""
        if on and self._state.state == S.UNPROVISIONED:
            self._state.transition(S.ADVERTISING, reason="pairing button")
        if not on and self._state.state == S.ADVERTISING:
            # No session: back to idle. A session in progress may finish; when it ends it
            # goes to UNPROVISIONED itself, because the window is closed (DESIGN §4).
            self._state.transition(S.UNPROVISIONED, reason="pairing window expired")
        await self._radio(on)

    async def reset(self) -> None:
        """[R][R]. The controller has closed the window and refused CONNECTING (Q21)."""
        assert self._session is not None
        # First, before any await, so no message can slip into the old session.
        self._session.end("reset")  # SESSION_ESTABLISHED -> UNPROVISIONED: window closed
        self._session.last_result = None  # clears the red LED
        await self._radio(False)
        removed = await self._wifi.forget_all()  # every teton-* profile (Q24)
        self._storage.reset()  # new setup code; provisioned cleared; device_id kept
        if self._state.state in (S.ADVERTISING, S.PROVISIONED):
            self._state.transition(S.UNPROVISIONED, reason="reset button")
        log.info("device_reset", profiles_removed=len(removed))
        self._show_code()

    def status_text(self) -> str:
        lines = [f"State: {self._state.state}"]
        last = self._session.last_result if self._session else None
        if last is None:
            lines.append("Wi-Fi: -")
        elif last[1].ok:
            ssid, result = last
            lines.append(f"Wi-Fi: {ssid}, IP {result.ip}, internet {result.internet}")
        else:
            lines.append(f"Wi-Fi: last attempt on {last[0]} failed: {last[1].reason}")
        # The code + QR again, because log lines scroll the first print away (Q23).
        return "\n".join(lines) + "\n" + self._code_text()

    async def power_off(self) -> None:
        log.info("power_off")
        self._window.close()
        await self._close()

    def on_tick(self) -> None:
        if self._session is not None:
            self._session.check_idle()

    # --- internals -----------------------------------------------------------------------

    async def _provisioned(self) -> None:
        """Called by the session right after it sent `connected` and saved `provisioned`:
        close the window and stop advertising now, before the configurator disconnects,
        or BlueZ would re-enable advertising after the disconnect (spike check g, D2)."""
        self._window.close()
        await self._radio(False)

    async def _radio(self, on: bool) -> None:
        if on == self._advertising or self._closed:
            return
        try:
            await self._transport.set_advertising(on)
        except TransportError as exc:
            log.error("advertising_failed", on=on, reason=str(exc))  # never crash the device
            return
        self._advertising = on
        log.info("advertising", on=on)

    def _publish_info(self, state: DeviceState) -> None:
        stored = self._storage.current
        self._transport.set_info(encode(Info(device_id=stored.device_id, state=state)))

    def _code_text(self) -> str:
        stored = self._storage.current
        return qr.render(stored.device_id, stored.setup_code)

    def _show_code(self) -> None:
        self._screen.message(self._code_text())  # to the screen only, never to the log

    async def _close(self) -> None:
        if self._closed:
            return
        await self._radio(False)
        self._closed = True
        try:
            await self._transport.close()
        except TransportError as exc:
            log.error("transport_close_failed", reason=str(exc))
        log.info("device_stopped")
