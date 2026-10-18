# Tests for the device session: handshake, rate limit, encrypted commands, Wi-Fi results,
# protocol errors, the pairing window and the idle timeout. A minimal configurator built
# from teton_common.crypto talks to the session over the memory transport, with the fake
# Wi-Fi and a fake clock: no hardware and no real waiting.

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fake_configurator import (
    CODE,
    DEVICE_ID,
    HOME,
    WRONG_CODE,
    Configurator,
    FakeClock,
    instant,
    settle,
)
from structlog.testing import capture_logs

from teton_common.crypto import ConfiguratorHandshake, HandshakeError
from teton_common.memory_transport import pair
from teton_common.messages import (
    Confirm,
    Envelope,
    Error,
    HandshakeFailed,
    HelloReply,
    NetworkInfo,
    Ready,
    Scan,
    ScanResult,
    SetCredentials,
    Status,
    encode,
)
from teton_common.protocol import (
    Channel,
    DeviceState,
    ErrorCode,
    FailureReason,
    InternetStatus,
    Security,
    StatusState,
)
from teton_common.transport import FramedTransport
from teton_device.session import DeviceSession, HandshakeLimiter
from teton_device.state import StateMachine
from teton_device.storage import Storage
from teton_device.wifi.fake import HOME_PSK_PASSWORD, FakeWifiBackend


class Rig:
    """A device session with everything around it faked, plus a connected configurator."""

    def __init__(self, tmp_path: Path, wifi: FakeWifiBackend | None = None) -> None:
        self.device, client = pair()
        self.clock = FakeClock()
        self.storage = Storage(tmp_path, new_code=lambda: CODE, new_id=lambda: DEVICE_ID)
        self.storage.load_or_create()
        self.state = StateMachine(DeviceState.ADVERTISING)  # [P] was pressed
        self.wifi = wifi or FakeWifiBackend(sleep=instant)
        self.window_is_open = True
        self.provisioned_calls = 0
        self.limiter = HandshakeLimiter(self.clock)
        self.session = DeviceSession(
            FramedTransport(self.device, self.clock),
            self.wifi,
            self.storage,
            self.state,
            self.limiter,
            window_open=lambda: self.window_is_open,
            on_provisioned=self._on_provisioned,
            clock=self.clock,
        )
        self.configurator = Configurator(client)

    async def _on_provisioned(self) -> None:
        self.provisioned_calls += 1

    async def start(self) -> None:
        await self.device.start()
        await self.device.set_advertising(True)
        await self.configurator.transport.connect()

    async def stop(self) -> None:
        await self.configurator.transport.close()
        await self.device.close()


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    r = Rig(tmp_path)
    await r.start()
    yield r
    await r.stop()


@pytest.fixture
async def ready(rig: Rig) -> Rig:
    """A rig with an established session."""
    assert await rig.configurator.establish() == Ready()
    return rig


# --- handshake --------------------------------------------------------------------------


async def test_good_handshake_then_ready(rig: Rig) -> None:
    assert await rig.configurator.establish() == Ready()
    assert rig.state.state == DeviceState.SESSION_ESTABLISHED
    assert rig.session.active


async def test_handshake_accepts_the_code_as_typed(rig: Rig) -> None:
    assert await rig.configurator.establish("7k2m-qx9d-4htr-b8wz") == Ready()


async def test_hello_reply_carries_the_device_id(rig: Rig) -> None:
    reply = await rig.configurator.hello()
    assert isinstance(reply, HelloReply) and reply.device_id == DEVICE_ID
    assert rig.state.state == DeviceState.ADVERTISING  # a handshake in progress (Q13)


async def test_wrong_code_is_caught_by_the_configurator_first(rig: Rig) -> None:
    reply = await rig.configurator.hello(WRONG_CODE)
    assert isinstance(reply, HelloReply)
    assert rig.configurator.handshake is not None
    with pytest.raises(HandshakeError):
        rig.configurator.handshake.on_hello_reply(reply)  # so it never sends credentials


async def test_forged_confirm_gets_handshake_failed_and_ends_the_session(rig: Rig) -> None:
    await rig.configurator.hello()
    await rig.configurator.send_session(Confirm(confirm=bytes(32)))
    assert await rig.configurator.session_reply() == HandshakeFailed()
    await settle()
    assert not rig.session.active
    assert rig.state.state == DeviceState.ADVERTISING


async def test_retyping_the_code_is_a_new_hello_on_the_same_connection(rig: Rig) -> None:
    await rig.configurator.hello(WRONG_CODE)  # the configurator gives up: no confirm (Q4)
    assert await rig.configurator.establish(CODE) == Ready()


async def test_confirm_without_hello_ends_nothing_and_gets_no_reply(rig: Rig) -> None:
    await rig.configurator.send_session(Confirm(confirm=bytes(32)))
    await rig.configurator.assert_silent()
    assert rig.state.state == DeviceState.ADVERTISING


async def test_garbage_on_session_is_ignored_and_the_device_keeps_working(rig: Rig) -> None:
    await rig.configurator.channel.send_bytes(Channel.SESSION, b"{not json")
    await rig.configurator.assert_silent()
    assert await rig.configurator.establish() == Ready()


async def test_new_hello_mid_session_starts_a_new_session(ready: Rig) -> None:
    old_cipher = ready.configurator.cipher
    assert await ready.configurator.establish() == Ready()
    assert ready.configurator.cipher is not old_cipher
    assert ready.state.state == DeviceState.SESSION_ESTABLISHED
    await ready.configurator.command(Scan())  # the new keys work
    assert isinstance(await ready.configurator.status(), ScanResult)


# --- rate limit (SPEC §5.4, Q4, Q5) -----------------------------------------------------


async def test_sixth_attempt_is_refused_and_accepted_after_the_backoff(rig: Rig) -> None:
    for _ in range(5):
        assert isinstance(await rig.configurator.hello(WRONG_CODE), HelloReply)
    assert await rig.configurator.hello() == Error(ErrorCode.RATE_LIMITED, retry_after_s=30)

    rig.clock.advance(29)
    assert await rig.configurator.hello() == Error(ErrorCode.RATE_LIMITED, retry_after_s=1)
    rig.clock.advance(1)
    assert await rig.configurator.establish() == Ready()  # resets the counter


async def test_backoff_doubles_while_failures_go_on(rig: Rig) -> None:
    for _ in range(5):
        await rig.configurator.hello(WRONG_CODE)
    rig.clock.advance(30)
    await rig.configurator.hello(WRONG_CODE)  # 6th failure
    assert await rig.configurator.hello() == Error(ErrorCode.RATE_LIMITED, retry_after_s=60)


async def test_success_resets_the_counter(rig: Rig) -> None:
    for _ in range(4):
        await rig.configurator.hello(WRONG_CODE)
    assert await rig.configurator.establish() == Ready()  # 5th hello, and it succeeds
    for _ in range(4):
        assert isinstance(await rig.configurator.hello(WRONG_CODE), HelloReply)


async def test_rate_limited_hello_leaves_the_current_session_alone(ready: Rig) -> None:
    for _ in range(5):
        ready.limiter.attempt()
    assert isinstance(await ready.configurator.hello(), Error)
    await settle()
    assert ready.session.active
    assert ready.state.state == DeviceState.SESSION_ESTABLISHED


@pytest.mark.parametrize(
    ("failures", "wait"),
    [(4, 0), (5, 30), (6, 60), (7, 120), (8, 240), (9, 480), (10, 600), (40, 600)],
)
def test_limiter_backoff_table(failures: int, wait: int) -> None:
    clock = FakeClock()
    limiter = HandshakeLimiter(clock)
    for _ in range(failures):
        limiter.attempt()
    assert limiter.retry_after_s() == wait


def test_limiter_unblocks_after_the_wait() -> None:
    clock = FakeClock()
    limiter = HandshakeLimiter(clock)
    for _ in range(5):
        limiter.attempt()
    clock.advance(29.5)
    assert limiter.retry_after_s() == 1  # rounded up
    clock.advance(0.5)
    assert limiter.retry_after_s() == 0


# --- commands ---------------------------------------------------------------------------


async def test_scan_returns_the_fake_list(ready: Rig) -> None:
    await ready.configurator.command(Scan())
    result = await ready.configurator.status()
    assert isinstance(result, ScanResult)
    assert result.networks[0] == NetworkInfo("Home-PSK", 82, Security.WPA2_PSK)
    assert [n.ssid for n in result.networks] == [
        "Home-PSK",
        "Hospital-EAP",
        "Cafe-Portal",
        "Weak-Signal",
    ]


async def test_set_credentials_connecting_then_connected(ready: Rig) -> None:
    await ready.configurator.command(HOME)
    assert await ready.configurator.status() == Status(StatusState.CONNECTING)
    assert await ready.configurator.status() == Status(
        StatusState.CONNECTED, ip="192.168.1.23", internet=InternetStatus.FULL
    )
    await settle()
    assert ready.state.state == DeviceState.PROVISIONED
    assert ready.storage.current.provisioned
    assert ready.provisioned_calls == 1  # advertising goes off (D2)
    assert ready.wifi.profiles == {"teton-Home-PSK"}
    assert not ready.session.active  # done: the session ends after success


async def test_wrong_password_fails_then_retry_works_in_the_same_session(ready: Rig) -> None:
    wrong = SetCredentials(ssid="Home-PSK", security=Security.WPA2_PSK, psk="not-the-password")
    await ready.configurator.command(wrong)
    assert await ready.configurator.status() == Status(StatusState.CONNECTING)
    assert await ready.configurator.status() == Status(
        StatusState.FAILED, reason=FailureReason.WRONG_PASSWORD
    )
    await settle()
    assert ready.state.state == DeviceState.SESSION_ESTABLISHED
    assert not ready.storage.current.provisioned

    await ready.configurator.command(HOME)
    assert await ready.configurator.status() == Status(StatusState.CONNECTING)
    status = await ready.configurator.status()
    assert isinstance(status, Status) and status.state == StatusState.CONNECTED


async def test_invalid_decrypted_command_gets_bad_request_and_the_session_stays(
    ready: Rig,
) -> None:
    short_psk = {"type": "set_credentials", "ssid": "Home-PSK", "hidden": False}
    short_psk |= {"security": "wpa2-psk", "psk": "12345"}
    await ready.configurator.send_plaintext_command(json.dumps(short_psk).encode())
    assert await ready.configurator.status() == Error(ErrorCode.BAD_REQUEST)
    await ready.configurator.command(Scan())  # still usable (Q6)
    assert isinstance(await ready.configurator.status(), ScanResult)


async def test_open_network_gets_unsupported_security(ready: Rig) -> None:
    await ready.configurator.command(SetCredentials(ssid="Cafe", security=Security.OPEN))
    assert await ready.configurator.status() == Error(ErrorCode.UNSUPPORTED_SECURITY)
    assert ready.state.state == DeviceState.SESSION_ESTABLISHED


# --- protocol violations end the session (Q6) -------------------------------------------


async def assert_session_ended(rig: Rig) -> None:
    await rig.configurator.assert_silent()
    assert not rig.session.active
    assert rig.state.state == DeviceState.ADVERTISING
    await rig.configurator.command(Scan())  # the old keys are gone: no answer
    await rig.configurator.assert_silent()


async def test_bad_envelope_ends_the_session(ready: Rig) -> None:
    await ready.configurator.channel.send_bytes(Channel.COMMAND, b'{"seq": 0}')
    await assert_session_ended(ready)


async def test_tampered_ciphertext_ends_the_session(ready: Rig) -> None:
    assert ready.configurator.cipher is not None
    good = ready.configurator.cipher.seal(encode(Scan()))
    tampered = Envelope(seq=good.seq, ct=bytes([good.ct[0] ^ 1]) + good.ct[1:])
    await ready.configurator.channel.send_bytes(Channel.COMMAND, encode(tampered))
    await assert_session_ended(ready)


async def test_wrong_seq_ends_the_session(ready: Rig) -> None:
    assert ready.configurator.cipher is not None
    ready.configurator.cipher.seal(b"skipped")  # seq 0 is never sent, so 1 arrives first
    await ready.configurator.command(Scan())
    await assert_session_ended(ready)


async def test_command_before_the_handshake_ends_the_session(rig: Rig) -> None:
    await rig.configurator.hello()  # handshake in progress, no keys yet
    await rig.configurator.channel.send_bytes(
        Channel.COMMAND, encode(Envelope(seq=0, ct=bytes(32)))
    )
    await rig.configurator.assert_silent()
    assert not rig.session.active
    await rig.configurator.send_session(Confirm(confirm=bytes(32)))  # too late
    await rig.configurator.assert_silent()


# --- pairing window (D2, Q22) and idle timeout ------------------------------------------


async def test_hello_after_the_window_closed_gets_no_reply(rig: Rig) -> None:
    rig.window_is_open = False
    await rig.configurator.send_session(ConfiguratorHandshake(CODE).hello())
    await rig.configurator.assert_silent()
    assert not rig.session.active


async def test_session_survives_window_expiry_and_ends_unprovisioned(ready: Rig) -> None:
    ready.window_is_open = False  # expired mid-session: the session may finish
    await ready.configurator.command(Scan())
    assert isinstance(await ready.configurator.status(), ScanResult)
    await ready.configurator.send_session(ConfiguratorHandshake(CODE).hello())  # Q22
    await ready.configurator.assert_silent()
    assert ready.session.active  # the refused hello didn't end it

    ready.clock.advance(300)
    ready.session.check_idle()
    assert ready.state.state == DeviceState.UNPROVISIONED  # not back to advertising


async def test_window_closing_mid_handshake_refuses_the_confirm(rig: Rig) -> None:
    reply = await rig.configurator.hello()
    assert isinstance(reply, HelloReply) and rig.configurator.handshake is not None
    confirm, _ = rig.configurator.handshake.on_hello_reply(reply)
    rig.window_is_open = False
    rig.state.transition(DeviceState.UNPROVISIONED, reason="window expired")  # the service
    await rig.configurator.send_session(confirm)
    await rig.configurator.assert_silent()
    assert not rig.session.active
    assert rig.state.state == DeviceState.UNPROVISIONED


async def test_idle_timeout_ends_the_session_after_300_s(ready: Rig) -> None:
    ready.clock.advance(299)
    ready.session.check_idle()
    assert ready.session.active
    ready.clock.advance(1)
    ready.session.check_idle()
    assert not ready.session.active
    assert ready.state.state == DeviceState.ADVERTISING


async def test_idle_timeout_also_drops_an_unfinished_handshake(rig: Rig) -> None:
    await rig.configurator.hello()
    rig.clock.advance(300)
    rig.session.check_idle()
    assert not rig.session.active


async def test_a_started_attempt_is_never_idle_and_always_finishes(tmp_path: Path) -> None:
    release = asyncio.Event()

    async def slow(_: float) -> None:
        await release.wait()

    rig = Rig(tmp_path, wifi=FakeWifiBackend(sleep=slow))
    await rig.start()
    try:
        assert await rig.configurator.establish() == Ready()
        await rig.configurator.command(HOME)
        assert await rig.configurator.status() == Status(StatusState.CONNECTING)
        rig.clock.advance(1000)
        rig.session.check_idle()  # the attempt holds the session
        assert rig.state.state == DeviceState.CONNECTING
        release.set()
        status = await rig.configurator.status()
        assert isinstance(status, Status) and status.state == StatusState.CONNECTED
    finally:
        await rig.stop()


# --- secrets ----------------------------------------------------------------------------


async def test_no_secret_in_the_session_logs(rig: Rig) -> None:
    with capture_logs() as logs:
        await rig.configurator.hello(WRONG_CODE)
        await rig.configurator.establish()
        await rig.configurator.command(HOME)
        await rig.configurator.status()
        await rig.configurator.status()
        await settle()
    text = repr(logs)
    for secret in (HOME_PSK_PASSWORD, CODE, WRONG_CODE, "7K2M-QX9D"):
        assert secret not in text
    assert "connected" in {entry["event"] for entry in logs}


async def test_end_during_a_scan_drops_the_reply_without_crashing(tmp_path: Path) -> None:
    # [R][R] can end the session while a scan is running (Q21).
    release = asyncio.Event()

    async def slow(_: float) -> None:
        await release.wait()

    rig = Rig(tmp_path, wifi=FakeWifiBackend(sleep=slow))
    await rig.start()
    try:
        assert await rig.configurator.establish() == Ready()
        await rig.configurator.command(Scan())
        await settle()
        with capture_logs() as logs:
            rig.session.end("reset")
            release.set()
            await rig.configurator.assert_silent()
        assert "reply_dropped" in {entry["event"] for entry in logs}
        assert rig.state.state == DeviceState.ADVERTISING
    finally:
        await rig.stop()


async def test_last_result_keeps_the_ssid_and_outcome(ready: Rig) -> None:
    await ready.configurator.command(HOME)
    await ready.configurator.status()
    await ready.configurator.status()
    assert ready.session.last_result is not None
    ssid, result = ready.session.last_result
    assert ssid == "Home-PSK" and result.ok
    assert HOME_PSK_PASSWORD not in repr(ready.session.last_result)
