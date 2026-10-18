# Per-connection protocol handler: handshake, encrypted command dispatch (scan,
# set_credentials), status notifications, handshake rate limiting, idle timeout.
# See SPEC §5.1, §5.4, §6 and DESIGN §5, §6.
#
# Rules decided in review (plan_v5, "Phase 2 review questions"):
#   S3   a new hello ends any previous session and starts a new one (no connection tracking)
#   Q4   every hello counts as a failed attempt until its confirm succeeds
#   Q5   a rate-limited hello gets error rate_limited + retry_after_s
#   Q6   fails before decryption -> end the session; invalid after decryption -> bad_request
#   Q22  a hello is accepted only while the pairing window is open

import asyncio
import math
import secrets
import time
from collections.abc import Awaitable, Callable

import structlog

from teton_common.crypto import DeviceHandshake, EnvelopeError, HandshakeError, Role, SessionCipher
from teton_common.messages import (
    Confirm,
    Error,
    HandshakeFailed,
    Hello,
    MessageError,
    Ready,
    Scan,
    ScanResult,
    SessionMessage,
    Status,
    StatusMessage,
    decode_command,
    decode_envelope,
    decode_session,
    encode,
)
from teton_common.protocol import (
    CAPS,
    CONNECT_TIMEOUT_S,
    HANDSHAKE_BACKOFF_INITIAL_S,
    HANDSHAKE_BACKOFF_MAX_S,
    HANDSHAKE_MAX_FAILURES,
    SESSION_IDLE_TIMEOUT_S,
    Channel,
    DeviceState,
    ErrorCode,
    StatusState,
)
from teton_common.transport import FramedTransport, TransportError
from teton_device.state import StateMachine
from teton_device.storage import Storage
from teton_device.wifi.backend import ConnectResult, WifiBackend

log = structlog.get_logger()

Clock = Callable[[], float]


class HandshakeLimiter:
    """Slows down setup code guessing (SPEC §5.4).

    Every accepted hello counts as a failure until its confirm succeeds (review Q4). A wrong
    code never reaches confirm: the configurator notices first and gives up, and so would an
    attacker collecting tags. After 5 in a row, new hellos are refused for 30 s; each
    further failure doubles that, up to 600 s. A successful handshake resets everything.
    """

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self._failures = 0
        self._blocked_until = 0.0

    def retry_after_s(self) -> int:
        """0 if a hello may go ahead, else the whole seconds left (rounded up)."""
        return max(0, math.ceil(self._blocked_until - self._clock()))

    def attempt(self) -> None:
        self._failures += 1
        if self._failures >= HANDSHAKE_MAX_FAILURES:
            # 30, 60, 120, 240, 480, then 600. Capping the exponent keeps the number small.
            doublings = min(self._failures - HANDSHAKE_MAX_FAILURES, 5)
            backoff = min(HANDSHAKE_BACKOFF_INITIAL_S * 2**doublings, HANDSHAKE_BACKOFF_MAX_S)
            self._blocked_until = self._clock() + backoff

    def success(self) -> None:
        self._failures = 0
        self._blocked_until = 0.0


class DeviceSession:
    """The device's half of the protocol, for whichever configurator is connected.

    One object for the life of the service. It holds at most one session at a time: a
    handshake in progress, or an established session with its cipher. Messages are handled
    one at a time (a lock), so a Wi-Fi attempt is one step: a message that arrives during
    it waits for the result, and the attempt always finishes (DESIGN §4).
    """

    def __init__(
        self,
        channels: FramedTransport,
        wifi: WifiBackend,
        storage: Storage,
        state: StateMachine,
        limiter: HandshakeLimiter,
        window_open: Callable[[], bool],
        on_provisioned: Callable[[], Awaitable[None]],
        clock: Clock = time.monotonic,
    ) -> None:
        self._channels = channels
        self._wifi = wifi
        self._storage = storage
        self._state = state
        self._limiter = limiter
        self._window_open = window_open  # owned by the service (buttons.PairingWindow)
        self._on_provisioned = on_provisioned  # the service turns advertising off (D2)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._handshake: DeviceHandshake | None = None
        self._cipher: SessionCipher | None = None
        self._session_id: str | None = None
        self._last_activity = clock()
        self._log = log
        self.last_result: tuple[str, ConnectResult] | None = (
            None  # (ssid, result) of the last attempt
        )
        channels.on_message(Channel.SESSION, self.handle_session_msg)
        channels.on_message(Channel.COMMAND, self.handle_command)

    @property
    def active(self) -> bool:
        """A handshake is in progress or a session is established."""
        return self._session_id is not None

    # --- entry points ------------------------------------------------------------------

    async def handle_session_msg(self, data: bytes) -> None:
        """Plaintext handshake messages: hello, confirm."""
        async with self._lock:
            self._last_activity = self._clock()
            try:
                msg = decode_session(data)
            except MessageError as exc:
                self.end(f"bad session message: {exc}")
                return
            if isinstance(msg, Hello):
                await self._on_hello(msg)
            elif isinstance(msg, Confirm):
                await self._on_confirm(msg)
            else:
                self.end(f"unexpected {msg.TYPE} from the configurator")
            self._last_activity = self._clock()

    async def handle_command(self, data: bytes) -> None:
        """Encrypted commands: scan, set_credentials."""
        async with self._lock:
            self._last_activity = self._clock()
            await self._on_command(data)
            self._last_activity = self._clock()

    def check_idle(self) -> None:
        """Called by the service every second: end a session after 300 s without traffic.
        A Wi-Fi attempt in progress holds the lock, so it never counts as idle."""
        if (
            self.active
            and not self._lock.locked()
            and self._clock() - self._last_activity >= SESSION_IDLE_TIMEOUT_S
        ):
            self.end("idle timeout")

    # --- handshake ---------------------------------------------------------------------

    async def _on_hello(self, hello: Hello) -> None:
        # Refusals leave any current session alone: it may finish (D2, Q22).
        if not self._window_open():
            log.info("hello_refused", reason="pairing window closed")
            return
        wait = self._limiter.retry_after_s()
        if wait:
            log.warning("hello_refused", reason="rate limited", retry_after_s=wait)
            await self._send(Channel.SESSION, Error(ErrorCode.RATE_LIMITED, retry_after_s=wait))
            return
        if self.active:
            self.end("new hello")  # S3: the new configurator wins
        if self._state.state != DeviceState.ADVERTISING:
            log.warning("hello_refused", reason="not advertising", state=self._state.state)
            return

        self._limiter.attempt()
        self._channels.reset()  # SPEC §4.1: a new session starts with empty buffers
        self._session_id = secrets.token_hex(4)
        self._log = log.bind(session_id=self._session_id)
        stored = self._storage.current
        self._handshake = DeviceHandshake(stored.device_id, stored.setup_code)
        try:
            reply = self._handshake.on_hello(hello)
        except HandshakeError as exc:
            self.end(f"bad hello: {exc}")
            return
        self._log.info("handshake_started")
        await self._send(Channel.SESSION, reply)

    async def _on_confirm(self, confirm: Confirm) -> None:
        handshake, self._handshake = self._handshake, None
        if handshake is None:
            self.end("confirm without a hello")
            return
        if self._state.state != DeviceState.ADVERTISING:
            # The window expired between hello and confirm: no new sessions after expiry.
            self.end("pairing window closed during the handshake")
            return
        try:
            keys = handshake.on_confirm(confirm)
        except HandshakeError:
            await self._send(Channel.SESSION, HandshakeFailed())
            self.end("wrong setup code")  # already counted at hello (Q4)
            return
        self._limiter.success()
        self._cipher = SessionCipher(keys, Role.DEVICE)
        self._state.transition(DeviceState.SESSION_ESTABLISHED, reason="handshake ok")
        await self._send_encrypted(Ready())

    # --- commands ----------------------------------------------------------------------

    async def _on_command(self, data: bytes) -> None:
        if self._cipher is None:
            self.end("command before the handshake")
            return
        try:
            plaintext = self._cipher.open(decode_envelope(data))
        except (MessageError, EnvelopeError) as exc:
            # Not proven to come from our configurator: could be an attacker (Q6).
            self.end(f"bad envelope: {exc}")
            return
        try:
            command = decode_command(plaintext)
        except MessageError as exc:
            # Authenticated, so it's a configurator bug, not an attack: say so, keep going.
            self._log.warning("bad_request", reason=str(exc))
            await self._send_encrypted(Error(ErrorCode.BAD_REQUEST))
            return

        if isinstance(command, Scan):
            networks = await self._wifi.scan()
            self._log.info("scan_done", networks=len(networks))
            await self._send_encrypted(ScanResult(networks=tuple(networks)))
            return

        if command.security not in CAPS:
            await self._send_encrypted(Error(ErrorCode.UNSUPPORTED_SECURITY))
            return
        self._state.transition(DeviceState.CONNECTING, reason="set_credentials")
        await self._send_encrypted(Status(StatusState.CONNECTING))
        self._log.info("connecting", ssid=command.ssid, security=command.security)
        result = await self._wifi.connect(command, timeout_s=CONNECT_TIMEOUT_S)
        # The secrets are in NetworkManager now; drop our references (best effort: Python
        # can't wipe memory, but nothing keeps them alive any longer).
        self.last_result = (command.ssid, result)  # for the LED and [S]; no secrets in it
        del command, plaintext
        await self._report(result)

    async def _report(self, result: ConnectResult) -> None:
        if not result.ok:
            self._log.info("connect_failed", reason=result.reason)
            self._state.transition(DeviceState.SESSION_ESTABLISHED, reason="wifi attempt failed")
            await self._send_encrypted(Status(StatusState.FAILED, reason=result.reason))
            return  # the session stays open for a retry
        assert result.ip is not None and result.internet is not None
        self._log.info("connected", ip=result.ip, internet=result.internet)
        status = Status(
            StatusState.CONNECTED, ip=result.ip, internet=result.internet, warnings=result.warnings
        )
        await self._send_encrypted(status)
        self._storage.mark_provisioned()
        self._state.transition(DeviceState.PROVISIONED, reason="wifi connected")
        await self._on_provisioned()  # advertising off before the configurator leaves (D2)
        self.end("provisioned")

    # --- helpers -----------------------------------------------------------------------

    def end(self, reason: str) -> None:
        """Forget the session's keys. Also called by the service on [R][R] (Q21). Never
        raises: a protocol error must not crash us."""
        if self.active:
            self._log.info("session_ended", reason=reason)
        else:
            log.warning("protocol_error", reason=reason)
        self._handshake = None
        self._cipher = None
        self._session_id = None
        self._log = log
        self._channels.reset()
        if self._state.state == DeviceState.SESSION_ESTABLISHED:
            # DESIGN §4: back to advertising, or to unprovisioned once the window expired.
            to = DeviceState.ADVERTISING if self._window_open() else DeviceState.UNPROVISIONED
            self._state.transition(to, reason=f"session ended: {reason}")

    async def _send(self, channel: Channel, msg: SessionMessage | StatusMessage) -> None:
        await self._send_bytes(channel, encode(msg))

    async def _send_encrypted(self, msg: StatusMessage) -> None:
        if self._cipher is None:
            # Ended while we were busy (e.g. [R][R] during a scan): nobody to answer.
            self._log.info("reply_dropped", reason="session ended")
            return
        await self._send_bytes(Channel.STATUS, encode(self._cipher.seal(encode(msg))))

    async def _send_bytes(self, channel: Channel, data: bytes) -> None:
        try:
            await self._channels.send_message(channel, data)
        except TransportError as exc:
            # The configurator is gone. Nothing to do: the next hello starts over.
            self._log.warning("send_failed", channel=channel, reason=str(exc))
