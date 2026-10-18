# Tests for the device service: the whole device (state machine, storage, session, buttons,
# pairing window, radio) over the memory transport with the fake Wi-Fi, injected buttons and
# fake time. The configurator side is the minimal one from fake_configurator.py.

import asyncio
import io
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
    FakeTime,
    instant,
    settle,
)
from structlog.testing import capture_logs

from teton_common.crypto import HandshakeError
from teton_common.memory_transport import MemoryClientTransport, MemoryDeviceTransport
from teton_common.messages import (
    Error,
    HelloReply,
    Ready,
    Scan,
    ScanResult,
    SetCredentials,
    Status,
    decode_info,
)
from teton_common.protocol import (
    Channel,
    DeviceState,
    ErrorCode,
    FailureReason,
    Security,
    StatusState,
)
from teton_common.transport import TransportError
from teton_device.buttons import MSG_ALREADY_CONNECTED, MSG_BUSY, Button, QueueButtons, StatusLine
from teton_device.service import DeviceService
from teton_device.storage import Storage
from teton_device.wifi.fake import FakeWifiBackend

S = DeviceState
NEW_CODE = "ZYXWVTSRQPNMKJHG"
WRONG_PSK = SetCredentials(ssid="Home-PSK", security=Security.WPA2_PSK, psk="not-the-password")


class Device:
    """A running DeviceService plus everything it talks to."""

    def __init__(
        self,
        tmp_path: Path,
        wifi: FakeWifiBackend | None = None,
        transport: MemoryDeviceTransport | None = None,
    ) -> None:
        self.time = FakeTime()
        self.transport = transport or MemoryDeviceTransport()
        self.configurator = Configurator(MemoryClientTransport(self.transport))
        codes = iter([CODE, NEW_CODE])
        self.storage = Storage(tmp_path, new_code=lambda: next(codes), new_id=lambda: DEVICE_ID)
        self.wifi = wifi or FakeWifiBackend(sleep=instant)
        self.buttons = QueueButtons()
        self.screen = io.StringIO()  # not a terminal: plain lines
        self.service = DeviceService(
            self.transport,
            self.wifi,
            self.storage,
            self.buttons,
            clock=self.time,
            sleep=self.time.sleep,
            status_line=StatusLine(self.screen),
        )
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self.service.run())
        await settle()

    async def press(self, *buttons: Button) -> None:
        self.buttons.press(*buttons)
        await settle()

    async def connect(self) -> None:
        await self.configurator.transport.connect()

    @property
    def state(self) -> DeviceState:
        return self.service.state()

    @property
    def advertising(self) -> bool:
        return self.transport._advertising  # what the radio is doing right now

    def info_state(self) -> DeviceState:
        return decode_info(self.transport._info).state

    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def stop(self) -> None:
        if self.running():
            self.buttons.press(Button.POWER)
            assert self.task is not None
            await asyncio.wait_for(self.task, 1)


@pytest.fixture
async def device(tmp_path: Path) -> AsyncIterator[Device]:
    d = Device(tmp_path)
    await d.start()
    yield d
    await d.stop()


async def provisioned_device(tmp_path: Path) -> Device:
    stored = Storage(tmp_path, new_code=lambda: CODE, new_id=lambda: DEVICE_ID)
    stored.load_or_create()
    stored.mark_provisioned()
    d = Device(tmp_path)
    await d.start()
    return d


async def pair_and_establish(d: Device) -> None:
    await d.press(Button.PAIR)
    await d.connect()
    assert await d.configurator.establish() == Ready()


# --- start and the pairing window -------------------------------------------------------


async def test_nothing_is_advertised_before_pair(device: Device) -> None:
    assert device.state == S.UNPROVISIONED
    assert not device.advertising
    with pytest.raises(TransportError):
        await device.connect()
    await device.time.advance(400)
    assert not device.advertising
    assert device.info_state() == S.UNPROVISIONED


async def test_start_shows_the_code_qr_and_note(device: Device) -> None:
    screen = device.screen.getvalue()
    assert "Setup code: 7K2M-QX9D-4HTR-B8WZ" in screen
    assert "For demo purposes the code is shown on screen" in screen
    assert "█" in screen  # the QR


async def test_pair_advertises_and_expiry_without_session_goes_back_to_idle(
    device: Device,
) -> None:
    await device.press(Button.PAIR)
    assert device.advertising and device.state == S.ADVERTISING
    assert device.info_state() == S.ADVERTISING
    await device.time.advance(300)
    assert not device.advertising and device.state == S.UNPROVISIONED
    assert device.info_state() == S.UNPROVISIONED


# --- the full flow ----------------------------------------------------------------------


async def test_happy_path_ends_provisioned_with_advertising_off_and_stays_on(
    device: Device,
) -> None:
    await pair_and_establish(device)
    await device.configurator.command(Scan())
    assert isinstance(await device.configurator.status(), ScanResult)
    await device.configurator.command(HOME)
    assert await device.configurator.status() == Status(StatusState.CONNECTING)
    connected = await device.configurator.status()
    assert isinstance(connected, Status) and connected.state == StatusState.CONNECTED
    await settle()

    assert device.state == S.PROVISIONED
    assert not device.advertising  # already off, the configurator is still connected (D2)
    assert device.storage.current.provisioned
    assert device.wifi.profiles == {"teton-Home-PSK"}
    assert device.info_state() == S.PROVISIONED
    await device.configurator.transport.close()
    await device.time.advance(400)
    assert device.running()  # stays on in PROVISIONED
    assert not device.advertising
    assert "LED green" in device.screen.getvalue()


async def test_wrong_password_rolls_back_and_the_retry_works(device: Device) -> None:
    await pair_and_establish(device)
    await device.configurator.command(WRONG_PSK)
    await device.configurator.status()  # connecting
    assert await device.configurator.status() == Status(
        StatusState.FAILED, reason=FailureReason.WRONG_PASSWORD
    )
    await device.time.advance(1)  # one tick redraws the LED
    assert device.wifi.profiles == set()
    assert device.state == S.SESSION_ESTABLISHED
    assert device.service.last_failure() == FailureReason.WRONG_PASSWORD
    assert "LED red: last attempt failed: wrong_password" in device.screen.getvalue()

    await device.configurator.command(HOME)
    await device.configurator.status()  # connecting
    connected = await device.configurator.status()
    assert isinstance(connected, Status) and connected.state == StatusState.CONNECTED
    assert device.service.last_failure() is None


async def test_second_hello_mid_session_starts_a_new_session(device: Device) -> None:
    await pair_and_establish(device)
    assert await device.configurator.establish() == Ready()
    assert device.state == S.SESSION_ESTABLISHED


async def test_wrong_code_limiter(device: Device) -> None:
    await device.press(Button.PAIR)
    await device.connect()
    for _ in range(5):
        await device.configurator.hello(WRONG_CODE)
    assert await device.configurator.hello() == Error(ErrorCode.RATE_LIMITED, retry_after_s=30)
    await device.time.advance(30)
    assert await device.configurator.establish() == Ready()


async def test_session_survives_window_expiry_then_ends_unprovisioned(device: Device) -> None:
    await pair_and_establish(device)
    await device.time.advance(200)
    await device.configurator.command(Scan())  # traffic: not idle
    await device.configurator.status()
    await device.time.advance(100)  # the window expires mid-session
    assert not device.advertising
    assert device.state == S.SESSION_ESTABLISHED

    await device.configurator.command(Scan())  # the session may finish
    assert isinstance(await device.configurator.status(), ScanResult)
    await device.configurator.send_session(device.configurator.handshake.hello())  # type: ignore[union-attr]
    await device.configurator.assert_silent()  # no new handshake after expiry (Q22)

    await device.time.advance(300)  # idle timeout ends the session
    assert device.state == S.UNPROVISIONED  # not back to advertising
    assert not device.advertising


async def test_pair_during_an_expired_session_reopens_the_window(device: Device) -> None:
    await pair_and_establish(device)
    await device.configurator.command(Scan())
    await device.configurator.status()
    await device.time.advance(299)
    await device.configurator.command(Scan())
    await device.configurator.status()
    await device.time.advance(1)
    assert not device.advertising
    await device.press(Button.PAIR)
    assert device.advertising and device.state == S.SESSION_ESTABLISHED


# --- provisioned device -----------------------------------------------------------------


async def test_provisioned_device_stays_off_air_even_after_pair(tmp_path: Path) -> None:
    d = await provisioned_device(tmp_path)
    try:
        assert d.state == S.PROVISIONED and not d.advertising
        await d.press(Button.PAIR)
        assert MSG_ALREADY_CONNECTED in d.screen.getvalue()
        assert not d.advertising
        assert "LED green" in d.screen.getvalue()
    finally:
        await d.stop()


# --- reset (Q21) ------------------------------------------------------------------------


async def test_reset_forgets_profiles_rotates_the_code_and_stays_off_air(
    device: Device,
) -> None:
    await pair_and_establish(device)
    await device.configurator.command(HOME)
    await device.configurator.status()
    await device.configurator.status()
    await device.configurator.transport.close()
    await settle()
    assert device.state == S.PROVISIONED

    await device.press(Button.RESET, Button.RESET)
    assert device.wifi.profiles == set()  # forget_all
    assert device.storage.current.setup_code == NEW_CODE
    assert not device.storage.current.provisioned
    assert device.storage.current.device_id == DEVICE_ID
    assert device.state == S.UNPROVISIONED and device.info_state() == S.UNPROVISIONED
    assert "Setup code: ZYXW-VTSR-QPNM-KJHG" in device.screen.getvalue()  # the new code + QR
    await device.time.advance(400)
    assert not device.advertising  # off until [P]

    await device.press(Button.PAIR)
    await device.connect()
    assert await device.configurator.establish(NEW_CODE) == Ready()  # the new code works


async def test_old_code_stops_working_after_reset(device: Device) -> None:
    await device.press(Button.RESET, Button.RESET, Button.PAIR)
    await device.connect()
    reply = await device.configurator.hello(CODE)
    assert device.configurator.handshake is not None
    assert isinstance(reply, HelloReply)
    with pytest.raises(HandshakeError):
        device.configurator.handshake.on_hello_reply(reply)


async def test_reset_mid_session_drops_the_session(device: Device) -> None:
    await pair_and_establish(device)
    await device.press(Button.RESET, Button.RESET)
    assert device.state == S.UNPROVISIONED
    assert not device.advertising
    await device.configurator.command(Scan())
    await device.configurator.assert_silent()  # the old keys are gone


async def test_reset_is_refused_while_connecting(tmp_path: Path) -> None:
    release = asyncio.Event()

    async def slow(_: float) -> None:
        await release.wait()

    d = Device(tmp_path, wifi=FakeWifiBackend(sleep=slow))
    await d.start()
    try:
        await pair_and_establish(d)
        await d.configurator.command(HOME)
        await d.configurator.status()  # connecting
        await d.press(Button.RESET, Button.RESET)
        assert MSG_BUSY in d.screen.getvalue()
        assert d.state == S.CONNECTING
        release.set()
        connected = await d.configurator.status()
        assert isinstance(connected, Status) and connected.state == StatusState.CONNECTED
        assert d.storage.current.setup_code == CODE  # never rotated
    finally:
        await d.stop()


# --- status and power -------------------------------------------------------------------


async def test_status_shows_wifi_and_the_code_again(device: Device) -> None:
    await pair_and_establish(device)
    await device.configurator.command(HOME)
    await device.configurator.status()
    await device.configurator.status()
    before = device.screen.getvalue().count("Setup code: 7K2M-QX9D-4HTR-B8WZ")
    await device.press(Button.STATUS)
    screen = device.screen.getvalue()
    assert "State: provisioned" in screen
    assert "Wi-Fi: Home-PSK, IP 192.168.1.23, internet full" in screen
    assert "Pairing window: closed" in screen
    assert screen.count("Setup code: 7K2M-QX9D-4HTR-B8WZ") == before + 1  # Q23


async def test_power_closes_the_transport(device: Device) -> None:
    await device.press(Button.PAIR)
    await device.press(Button.POWER)
    assert not device.running()
    assert not device.advertising
    with pytest.raises(TransportError):
        await device.transport.send(Channel.STATUS, b"x")  # closed


async def test_ctrl_c_also_closes_the_transport(device: Device) -> None:
    await device.press(Button.PAIR)
    assert device.task is not None
    device.task.cancel()  # what asyncio.run does on Ctrl+C
    with pytest.raises(asyncio.CancelledError):
        await device.task
    assert device.transport._closed


async def test_a_radio_error_does_not_crash_the_device(tmp_path: Path) -> None:
    class FlakyRadio(MemoryDeviceTransport):
        async def set_advertising(self, on: bool) -> None:
            raise TransportError("BlueZ said no")

    d = Device(tmp_path, transport=FlakyRadio())
    await d.start()
    try:
        await d.press(Button.PAIR)
        await d.time.advance(301)
        assert d.running()
        assert "advertising_failed" not in d.screen.getvalue()  # logged, not on the screen
    finally:
        await d.stop()


async def test_info_never_contains_the_code(device: Device) -> None:
    await device.press(Button.PAIR)
    info = json.loads(device.transport._info)
    assert CODE not in json.dumps(info)
    assert info == {
        "v": 1,
        "device_id": DEVICE_ID,
        "name": "Teton-9A2C",
        "state": "advertising",
        "caps": ["wpa2-psk", "wpa3-sae", "wpa2-eap-peap"],
    }


async def test_the_setup_code_never_reaches_the_log(tmp_path: Path) -> None:
    with capture_logs() as logs:
        d = Device(tmp_path)
        await d.start()
        await d.press(Button.STATUS, Button.RESET, Button.RESET, Button.PAIR)
        await d.stop()
    text = repr(logs)
    for code in (CODE, NEW_CODE, "7K2M-QX9D", "ZYXW-VTSR"):
        assert code not in text  # only on the screen, as the stand-in for the label
    assert "Setup code: ZYXW-VTSR-QPNM-KJHG" in d.screen.getvalue()
