# Tests for the guided flow: every answer is scripted, and the flow runs against the real
# DeviceService over the memory transport with the fake Wi-Fi (Step 3.3, SPEC §9.2).

import asyncio
import os
from pathlib import Path

from fake_configurator import (
    CODE,
    NO_REPLY_S,
    TETON,
    WAIT_S,
    WRONG_CODE,
    Device,
    LimitedWifi,
    RecordingWifi,
    ScriptedIO,
    ca_pem,
    settle,
    start_paired,
)

from teton_common.crypto import Role, SessionCipher, SessionKeys
from teton_common.framing import frame
from teton_common.memory_transport import MemoryClientTransport
from teton_common.messages import EapCredentials, SetCredentials, encode
from teton_common.protocol import (
    MAX_CA_PEM_BYTES,
    DeviceState,
    ErrorCode,
    FailureReason,
    Security,
    StatusWarning,
)
from teton_common.transport import ClientTransport
from teton_configurator import ui_text
from teton_configurator.client import ProvisioningClient
from teton_configurator.flow import FoundDevice, run_flow
from teton_device.wifi.backend import ConnectResult, Credentials
from teton_device.wifi.fake import (
    HOME_PSK_PASSWORD,
    HOSPITAL_PASSWORD,
    HOSPITAL_USER,
)

FORMATTED_CODE = "7K2M-QX9D-4HTR-B8WZ"
# Menu numbers of the default fake networks, strongest first.
HOME, HOSPITAL, CAFE, WEAK, HIDDEN, SCAN_AGAIN = "1", "2", "3", "4", "5", "6"


class StuckWifi(RecordingWifi):
    """Never finishes an attempt until released."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def connect(self, creds: Credentials, timeout_s: float) -> ConnectResult:
        await self.release.wait()
        return await super().connect(creds, timeout_s)


async def run(
    device: Device,
    io: ScriptedIO,
    found: list[list[FoundDevice]] | None = None,
    wait_s: float = WAIT_S,
) -> int:
    """run_flow against `device`. `found`: what each call of discover() returns."""
    rounds = iter(found or [[TETON]])

    async def discover() -> list[FoundDevice]:
        return next(rounds)

    def connect(_: FoundDevice) -> MemoryClientTransport:
        return MemoryClientTransport(device.transport)

    def make_client(transport: ClientTransport) -> ProvisioningClient:
        return ProvisioningClient(
            transport, reply_wait_s=wait_s, scan_wait_s=wait_s, result_wait_s=wait_s
        )

    code = await run_flow(discover, connect, io, make_client=make_client)
    await settle()
    return code


def requests(device: Device) -> list[Credentials]:
    assert isinstance(device.wifi, RecordingWifi)
    return device.wifi.requests


# --- the scenarios of Step 3.3 ----------------------------------------------------------


async def test_happy_path(paired_device: Device) -> None:
    io = ScriptedIO("", FORMATTED_CODE, HOME, HOME_PSK_PASSWORD)  # Enter: the only device
    assert await run(paired_device, io) == 0
    io.assert_all_used()
    out = io.output
    assert ui_text.PRESS_PAIRING_BUTTON in out
    assert "1) Teton-9A2C  ▂▄▆█" in out
    assert "1) Home-PSK      ▂▄▆█  password" in out  # names padded so the bars line up
    assert "2) Hospital-EAP  ▂▄▆   enterprise" in out
    assert f"5) {ui_text.OTHER_NETWORK}" in out and f"6) {ui_text.SCAN_AGAIN}" in out
    assert ui_text.connecting_text("Home-PSK") in out
    assert "Connected. IP 192.168.1.23, internet OK." in out
    assert io.secret_prompts == [ui_text.ASK_PASSWORD]  # getpass, never echoed
    assert HOME_PSK_PASSWORD not in out
    assert paired_device.state == DeviceState.PROVISIONED


async def test_wrong_code_then_retype_then_success(paired_device: Device) -> None:
    io = ScriptedIO("", "7K2M", WRONG_CODE, CODE, HOME, HOME_PSK_PASSWORD)
    assert await run(paired_device, io) == 0
    out = io.output
    assert ui_text.INVALID_CODE in out  # bad format: explained, nothing sent (Q28)
    assert ui_text.WRONG_CODE in out
    assert "Connected." in out


async def test_wrong_password_then_retry_then_success(paired_device: Device) -> None:
    io = ScriptedIO("", CODE, HOME, "short", "not-the-password", "1", HOME_PSK_PASSWORD)
    assert await run(paired_device, io) == 0
    out = io.output
    assert ui_text.INVALID_PASSWORD_LENGTH in out  # checked before sending (Q28)
    assert ui_text.failure_text(FailureReason.WRONG_PASSWORD) in out
    assert f"1) {ui_text.RETRY_PASSWORD}" in out
    assert "Connected." in out
    assert [r.psk for r in requests(paired_device)] == ["not-the-password", HOME_PSK_PASSWORD]


async def test_failure_then_another_network(paired_device: Device) -> None:
    io = ScriptedIO("", CODE, CAFE, "any-password", "2", HOME, HOME_PSK_PASSWORD)
    assert await run(paired_device, io) == 0
    assert ui_text.failure_text(FailureReason.CAPTIVE_PORTAL) in io.output
    assert [r.ssid for r in requests(paired_device)] == ["Cafe-Portal", "Home-PSK"]


async def test_hidden_network(paired_device: Device) -> None:
    io = ScriptedIO("", CODE, HIDDEN, "", "Home-PSK", "1", HOME_PSK_PASSWORD)
    assert await run(paired_device, io) == 0
    out = io.output
    assert ui_text.INVALID_NETWORK_NAME in out  # an empty name
    for label in (
        "WPA2/WPA3 Personal (password)",
        "WPA3 Personal only (password)",
        "WPA2 Enterprise (username and password)",
    ):
        assert label in out  # Q42
    [sent] = requests(paired_device)
    assert sent.hidden and sent.ssid == "Home-PSK" and sent.security == Security.WPA2_PSK


async def test_peap_without_ca_warns_server_not_verified(paired_device: Device) -> None:
    io = ScriptedIO("", CODE, HOSPITAL, "", HOSPITAL_USER, HOSPITAL_PASSWORD, "", "")
    assert await run(paired_device, io) == 0
    out = io.output
    assert ui_text.REQUIRED in out  # an empty username
    assert "Connected. IP 10.20.0.57, internet OK." in out
    assert ui_text.warning_text(StatusWarning.SERVER_NOT_VERIFIED) in out
    assert io.secret_prompts == [ui_text.ASK_PASSWORD]
    assert HOSPITAL_PASSWORD not in out


async def test_peap_with_a_ca_file_and_bad_files_first(
    paired_device: Device, tmp_path: Path
) -> None:
    good = tmp_path / "ca.pem"
    good.write_bytes(ca_pem())
    big = tmp_path / "big.pem"
    big.write_bytes(b"x" * (MAX_CA_PEM_BYTES + 1))
    text = tmp_path / "notes.txt"
    text.write_text("not a certificate")
    io = ScriptedIO(
        "", CODE, HOSPITAL, HOSPITAL_USER, HOSPITAL_PASSWORD,
        str(tmp_path / "missing.pem"), str(big), str(text), str(good), "radius.example.org",
    )  # fmt: skip
    assert await run(paired_device, io) == 0
    out = io.output
    for problem in (
        ui_text.CA_FILE_UNREADABLE,
        ui_text.CA_FILE_TOO_LARGE,
        ui_text.CA_FILE_NOT_CERTIFICATE,
    ):
        assert problem in out
    assert ui_text.warning_text(StatusWarning.SERVER_NOT_VERIFIED) not in out
    [sent] = requests(paired_device)
    assert sent.eap is not None
    assert sent.eap.ca_cert_pem == good.read_text()
    assert sent.eap.domain_suffix_match == "radius.example.org"


async def test_limited_internet_warning(tmp_path: Path) -> None:
    d = await start_paired(tmp_path, LimitedWifi())
    try:
        io = ScriptedIO("", CODE, HOME, HOME_PSK_PASSWORD)
        assert await run(d, io) == 0
        out = io.output
        assert "Connected. IP 192.168.1.23, internet not confirmed." in out
        assert ui_text.warning_text(StatusWarning.LIMITED_INTERNET) in out
    finally:
        await d.stop()


# --- menus ------------------------------------------------------------------------------


async def test_no_device_found_then_look_again(paired_device: Device) -> None:
    io = ScriptedIO("", "", CODE, HOME, HOME_PSK_PASSWORD)  # Enter looks again
    assert await run(paired_device, io, found=[[], [TETON]]) == 0
    assert ui_text.NO_DEVICE_FOUND in io.output


async def test_no_device_found_then_quit(paired_device: Device) -> None:
    io = ScriptedIO("q")
    assert await run(paired_device, io, found=[[]]) == 1
    assert ui_text.STOPPED in io.output


async def test_scan_again(paired_device: Device) -> None:
    io = ScriptedIO("", CODE, SCAN_AGAIN, HOME, HOME_PSK_PASSWORD)
    assert await run(paired_device, io) == 0
    assert io.lines.count(ui_text.LOOKING_FOR_NETWORKS) == 2


async def test_a_number_not_on_the_list_is_asked_again(paired_device: Device) -> None:
    io = ScriptedIO("", CODE, "9", "x", HOME, HOME_PSK_PASSWORD)
    assert await run(paired_device, io) == 0
    assert io.lines.count(ui_text.INVALID_CHOICE) == 2


async def test_rate_limited_says_how_long_then_works(paired_device: Device) -> None:
    async def wait_then_code() -> str:
        await paired_device.time.advance(30)
        return CODE

    io = ScriptedIO("", *[WRONG_CODE] * 5, CODE, wait_then_code, HOME, HOME_PSK_PASSWORD)
    assert await run(paired_device, io) == 0
    assert ui_text.error_text(ErrorCode.RATE_LIMITED, retry_after_s=30) in io.output


# --- the run ends -----------------------------------------------------------------------


async def test_q_quits_and_closes_the_connection(paired_device: Device) -> None:
    io = ScriptedIO("", CODE, "q")
    assert await run(paired_device, io) == 1
    assert ui_text.STOPPED in io.output
    assert paired_device.transport._client is None  # closed: the device is free again


async def test_end_of_input_quits(paired_device: Device) -> None:
    io = ScriptedIO("", CODE)  # stdin closes at the network menu
    assert await run(paired_device, io) == 1
    assert ui_text.STOPPED in io.output
    assert paired_device.transport._client is None


async def test_window_closed_before_the_code_gets_no_reply(paired_device: Device) -> None:
    async def window_expires_then_code() -> str:
        await paired_device.time.advance(300)  # the technician took too long (Q22)
        return CODE

    io = ScriptedIO("", window_expires_then_code)
    assert await run(paired_device, io, wait_s=NO_REPLY_S) == 1
    assert ui_text.NO_REPLY in io.output


async def test_no_result_in_time(tmp_path: Path) -> None:
    wifi = StuckWifi()
    d = await start_paired(tmp_path, wifi)
    try:
        io = ScriptedIO("", CODE, HOME, HOME_PSK_PASSWORD)
        assert await run(d, io, wait_s=NO_REPLY_S) == 1
        assert ui_text.RESULT_TIMEOUT in io.output
        assert ui_text.failure_text(FailureReason.TIMEOUT) not in io.output
    finally:
        wifi.release.set()  # the device's attempt always finishes
        await settle()
        await d.stop()


# --- limits -----------------------------------------------------------------------------


def test_the_largest_allowed_request_fits_one_message() -> None:
    """Review Q43: MAX_CA_PEM_BYTES leaves room for the other fields once the request is
    encrypted, base64-encoded and framed."""
    creds = SetCredentials(
        ssid="S" * 32,
        security=Security.WPA2_EAP_PEAP,
        hidden=True,
        eap=EapCredentials(
            identity="u" * 64,
            password="p" * 64,
            anonymous_identity="a" * 64,
            # 64-character lines, as in a real PEM: each newline costs 2 bytes in JSON.
            ca_cert_pem=("A" * 64 + "\n") * (MAX_CA_PEM_BYTES // 65),
            domain_suffix_match="d" * 253,
        ),
    )
    keys = SessionKeys(c2d=os.urandom(32), d2c=os.urandom(32))
    envelope = SessionCipher(keys, Role.CONFIGURATOR).seal(encode(creds))
    frame(encode(envelope))  # raises FramingError above MAX_MESSAGE_BYTES
