# The whole system in one process: the real DeviceService and the real guided flow
# (run_flow) over the memory transport. Only the radio, the Wi-Fi, the buttons and the
# clock are faked. Each test checks what the technician saw (ui_text) and what the device
# ended up doing (state, LED, advertising). Step 4.1, SPEC §12, DESIGN §7.
#
# Differences from test_flow.py (review Q55): the scan for devices sees the device only
# while it advertises, like a BLE scan (Q51), the radio traffic is recorded, and several
# scenarios use more than one configurator run against the same device.

from pathlib import Path

import pytest
from fake_configurator import (
    CODE,
    NEW_CODE,
    NO_REPLY_S,
    TETON,
    WAIT_S,
    WRONG_CODE,
    Answer,
    Device,
    LimitedWifi,
    RecordingWifi,
    ScriptedIO,
    Wire,
    ca_pem,
    settle,
    start_paired,
)
from structlog.testing import capture_logs

from teton_common.framing import Reassembler
from teton_common.memory_transport import MemoryClientTransport
from teton_common.messages import decode_session
from teton_common.protocol import (
    Channel,
    DeviceState,
    ErrorCode,
    FailureReason,
    InternetStatus,
    Security,
    StatusWarning,
)
from teton_common.transport import ClientTransport, TransportError
from teton_configurator import ui_text
from teton_configurator.client import ProvisioningClient
from teton_configurator.flow import FoundDevice, run_flow
from teton_device.buttons import Button
from teton_device.wifi.backend import Credentials, Network
from teton_device.wifi.fake import (
    DEFAULT_NETWORKS,
    HOME_PSK_PASSWORD,
    HOSPITAL_PASSWORD,
    HOSPITAL_USER,
    FakeNetwork,
)

S = DeviceState
# Menu numbers of the default fake networks, strongest first, then the two extra options.
HOME, HOSPITAL, CAFE, HIDDEN = "1", "2", "3", "5"
CONNECTED = ui_text.connected_text("192.168.1.23", InternetStatus.FULL)


class Technician:
    """The configurator laptop: run_flow with scripted answers. Its scan finds the device
    only while it advertises (Q51), and it records what it sends over the air."""

    def __init__(self, device: Device, *answers: Answer, wait_s: float = WAIT_S) -> None:
        self.device = device
        self.io = ScriptedIO(*answers)
        self.wait_s = wait_s
        self.wire: Wire | None = None

    async def run(self) -> int:
        async def discover() -> list[FoundDevice]:
            return [TETON] if self.device.advertising else []

        def connect(_: FoundDevice) -> Wire:
            self.wire = Wire(self.device.transport)
            return self.wire

        def make_client(transport: ClientTransport) -> ProvisioningClient:
            w = self.wait_s
            return ProvisioningClient(transport, reply_wait_s=w, scan_wait_s=w, result_wait_s=w)

        code = await run_flow(discover, connect, self.io, make_client=make_client)
        await settle()
        return code

    @property
    def saw(self) -> str:
        return self.io.output

    def sent(self) -> list[str]:
        """Every message sent, in order: the handshake by type, commands as "command"
        (they are encrypted, so their type can't be read on the wire)."""
        assert self.wire is not None
        reassemblers = {Channel.SESSION: Reassembler(), Channel.COMMAND: Reassembler()}
        out = []
        for channel, chunk in self.wire.sent:
            msg = reassemblers[channel].feed(chunk)
            if msg is not None:
                out.append(decode_session(msg).TYPE if channel == Channel.SESSION else "command")
        return out


def looking_at(device: Device, seen: list[tuple[DeviceState, str]], answer: str) -> Answer:
    """An answer typed after a glance at the device: records its state and LED then."""

    async def glance() -> str:
        seen.append((device.state, await device.led()))
        return answer

    return glance


def requests(device: Device) -> list[Credentials]:
    assert isinstance(device.wifi, RecordingWifi)
    return device.wifi.requests


async def assert_provisioned(device: Device) -> None:
    assert device.state == S.PROVISIONED
    assert device.info_state() == S.PROVISIONED
    assert device.storage.current.provisioned
    assert not device.advertising
    assert device.running()  # the device stays on after success (D2)
    assert await device.led() == "LED green"


# --- success ----------------------------------------------------------------------------


async def test_psk_success_shows_the_ip_and_the_device_stops_advertising(
    paired_device: Device,
) -> None:
    tech = Technician(paired_device, "", CODE, HOME, HOME_PSK_PASSWORD)
    assert await tech.run() == 0
    tech.io.assert_all_used()
    assert CONNECTED in tech.saw
    assert HOME_PSK_PASSWORD not in tech.saw
    await assert_provisioned(paired_device)
    assert paired_device.wifi.profiles == {"teton-Home-PSK"}


async def test_limited_internet_succeeds_with_a_warning(state_dir: Path) -> None:
    d = await start_paired(state_dir, LimitedWifi())
    try:
        tech = Technician(d, "", CODE, HOME, HOME_PSK_PASSWORD)
        assert await tech.run() == 0
        assert ui_text.connected_text("192.168.1.23", InternetStatus.LIMITED) in tech.saw
        assert ui_text.warning_text(StatusWarning.LIMITED_INTERNET) in tech.saw
        await assert_provisioned(d)
    finally:
        await d.stop()


async def test_peap_with_a_ca_file_has_no_warning(paired_device: Device, tmp_path: Path) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_bytes(ca_pem())
    tech = Technician(
        paired_device, "", CODE, HOSPITAL, HOSPITAL_USER, HOSPITAL_PASSWORD, str(ca), ""
    )
    assert await tech.run() == 0
    assert ui_text.connected_text("10.20.0.57", InternetStatus.FULL) in tech.saw
    assert ui_text.warning_text(StatusWarning.SERVER_NOT_VERIFIED) not in tech.saw
    [sent] = requests(paired_device)
    assert sent.eap is not None and sent.eap.ca_cert_pem == ca.read_text()
    await assert_provisioned(paired_device)


async def test_peap_without_a_ca_or_domain_warns_the_server_is_not_verified(
    paired_device: Device,
) -> None:
    tech = Technician(paired_device, "", CODE, HOSPITAL, HOSPITAL_USER, HOSPITAL_PASSWORD, "", "")
    assert await tech.run() == 0
    assert ui_text.connected_text("10.20.0.57", InternetStatus.FULL) in tech.saw
    assert ui_text.warning_text(StatusWarning.SERVER_NOT_VERIFIED) in tech.saw
    await assert_provisioned(paired_device)


class HiddenWifi(RecordingWifi):
    """The default networks plus one that doesn't broadcast its name (Q53)."""

    LAB = FakeNetwork("Lab-Hidden", 70, Security.WPA2_PSK, psk="lab-network-key")

    def __init__(self) -> None:
        super().__init__(DEFAULT_NETWORKS + (self.LAB,))

    async def scan(self) -> list[Network]:
        return [n for n in await super().scan() if n.ssid != self.LAB.ssid]


async def test_a_hidden_network_not_in_the_scan_is_joined_by_name(state_dir: Path) -> None:
    d = await start_paired(state_dir, HiddenWifi())
    try:
        tech = Technician(d, "", CODE, HIDDEN, "Lab-Hidden", "1", "lab-network-key")
        assert await tech.run() == 0
        menu = tech.saw.split(ui_text.ASK_NETWORK)[0]
        assert ui_text.OTHER_NETWORK in menu and "Lab-Hidden" not in menu
        assert CONNECTED in tech.saw
        [sent] = requests(d)
        assert sent.ssid == "Lab-Hidden" and sent.hidden
        await assert_provisioned(d)
    finally:
        await d.stop()


# --- failures and retries in one session ------------------------------------------------


async def test_wrong_password_then_retry_in_the_same_session(paired_device: Device) -> None:
    seen: list[tuple[DeviceState, str]] = []
    tech = Technician(
        paired_device,
        "", CODE, HOME, "not-the-password",
        looking_at(paired_device, seen, "1"), HOME_PSK_PASSWORD,
    )  # fmt: skip
    assert await tech.run() == 0
    assert ui_text.failure_text(FailureReason.WRONG_PASSWORD) in tech.saw
    assert seen == [(S.SESSION_ESTABLISHED, "LED red: last attempt failed: wrong_password")]
    assert CONNECTED in tech.saw
    assert tech.sent() == ["hello", "confirm", "command", "command", "command"]  # one session
    await assert_provisioned(paired_device)


async def test_wrong_code_then_retype_sends_no_credentials_before_the_good_code(
    paired_device: Device,
) -> None:
    tech = Technician(paired_device, "", WRONG_CODE, CODE, HOME, HOME_PSK_PASSWORD)
    assert await tech.run() == 0
    assert ui_text.WRONG_CODE in tech.saw
    assert CONNECTED in tech.saw
    # The wrong code stops at the device's tag: no confirm, and no command before the
    # good code's session exists (SPEC §12 MUST #4). Then scan and set_credentials.
    assert tech.sent() == ["hello", "hello", "confirm", "command", "command"]
    assert len(requests(paired_device)) == 1
    await assert_provisioned(paired_device)


async def test_captive_portal_is_explained_and_the_session_stays_open(
    paired_device: Device,
) -> None:
    seen: list[tuple[DeviceState, str]] = []
    tech = Technician(
        paired_device, "", CODE, CAFE, "any-password", looking_at(paired_device, seen, "q")
    )
    assert await tech.run() == 1
    assert ui_text.failure_text(FailureReason.CAPTIVE_PORTAL) in tech.saw
    assert seen == [(S.SESSION_ESTABLISHED, "LED red: last attempt failed: captive_portal")]
    assert paired_device.wifi.profiles == set()  # a failed attempt leaves nothing behind


async def test_a_second_hello_mid_session_restarts_the_session(paired_device: Device) -> None:
    first = Technician(paired_device, "", CODE, "q")  # walks away at the network menu
    assert await first.run() == 1
    # No connection tracking (S3): the device still holds the first session.
    assert paired_device.state == S.SESSION_ESTABLISHED
    assert paired_device.advertising  # the pairing window is still open
    second = Technician(paired_device, "", CODE, HOME, HOME_PSK_PASSWORD)
    with capture_logs() as logs:
        assert await second.run() == 0
    assert any(e["event"] == "session_ended" and e["reason"] == "new hello" for e in logs)
    assert first.sent().count("hello") == second.sent().count("hello") == 1
    await assert_provisioned(paired_device)


# --- the rate limit and the pairing window ----------------------------------------------


async def test_rate_limit_says_how_long_to_wait_then_works(paired_device: Device) -> None:
    async def wait_then_code() -> str:
        await paired_device.time.advance(30)
        return CODE

    wrong = [WRONG_CODE] * 5
    tech = Technician(paired_device, "", *wrong, CODE, wait_then_code, HOME, HOME_PSK_PASSWORD)
    assert await tech.run() == 0
    assert ui_text.error_text(ErrorCode.RATE_LIMITED, retry_after_s=30) in tech.saw
    assert tech.sent().count("hello") == 7  # 5 wrong, 1 refused, 1 good
    await assert_provisioned(paired_device)


async def test_window_expiry_before_hello_gets_no_reply_then_pair_again_works(
    paired_device: Device,
) -> None:
    async def window_expires_then_code() -> str:
        await paired_device.time.advance(300)  # the technician took too long (Q22)
        return CODE

    late = Technician(paired_device, "", window_expires_then_code, wait_s=NO_REPLY_S)
    assert await late.run() == 1
    assert ui_text.NO_REPLY in late.saw
    assert paired_device.state == S.UNPROVISIONED and not paired_device.advertising
    assert await paired_device.led() == "LED off"

    await paired_device.press(Button.PAIR)
    assert await paired_device.led() == "LED blinking blue"
    again = Technician(paired_device, "", CODE, HOME, HOME_PSK_PASSWORD)
    assert await again.run() == 0
    await assert_provisioned(paired_device)


async def test_after_success_only_reset_and_pair_allow_a_new_setup(
    paired_device: Device,
) -> None:
    assert await Technician(paired_device, "", CODE, HOME, HOME_PSK_PASSWORD).run() == 0

    nobody = Technician(paired_device, "q")  # nothing advertises: nothing to find
    assert await nobody.run() == 1
    assert ui_text.NO_DEVICE_FOUND in nobody.saw
    with pytest.raises(TransportError):
        await MemoryClientTransport(paired_device.transport).connect()

    await paired_device.press(Button.RESET, Button.RESET)
    assert paired_device.state == S.UNPROVISIONED and not paired_device.advertising
    assert paired_device.wifi.profiles == set()  # forget_all (Q24)
    await paired_device.press(Button.PAIR)
    # The code on the label changed: the old one is now wrong.
    tech = Technician(paired_device, "", CODE, NEW_CODE, HOME, HOME_PSK_PASSWORD)
    assert await tech.run() == 0
    assert ui_text.WRONG_CODE in tech.saw
    await assert_provisioned(paired_device)
