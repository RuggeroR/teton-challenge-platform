# Tests for the configurator's protocol client, against the real DeviceService over the
# memory transport, with the fake Wi-Fi, injected buttons and fake time (Step 3.2).
# The client's own waits are real seconds, so they are set to fractions of a second here.

import asyncio
from pathlib import Path

import pytest
from fake_configurator import (
    CODE,
    DEVICE_ID,
    HOME,
    NO_REPLY_S,
    WAIT_S,
    WRONG_CODE,
    WRONG_PSK,
    Device,
    Wire,
    settle,
)
from structlog.testing import capture_logs

from teton_common.messages import Info, SetCredentials, Status, encode
from teton_common.protocol import (
    Channel,
    DeviceState,
    ErrorCode,
    FailureReason,
    InternetStatus,
    Security,
    StatusState,
)
from teton_configurator.client import (
    ConnectionLostError,
    DeviceRefusedError,
    NoReplyError,
    ProtocolError,
    ProvisioningClient,
    RateLimitedError,
    WrongCodeError,
)
from teton_device.buttons import Button
from teton_device.wifi.fake import HOME_PSK_PASSWORD, FakeWifiBackend


async def connect(d: Device, wait_s: float = WAIT_S) -> tuple[ProvisioningClient, Wire]:
    """[P] on the device, then a client connected to it."""
    await d.press(Button.PAIR)
    wire = Wire(d.transport)
    client = ProvisioningClient(wire, reply_wait_s=wait_s, scan_wait_s=wait_s, result_wait_s=wait_s)
    await client.connect()
    return client, wire


async def provision(client: ProvisioningClient, creds: SetCredentials) -> list[Status]:
    return [status async for status in client.provision(creds)]


# --- the three scenarios of Step 3.2 ----------------------------------------------------


async def test_full_success(device: Device) -> None:
    client, _ = await connect(device)
    info = await client.handshake("7k2m-qx9d-4htr-b8wz")  # typed as people do
    assert info.device_id == DEVICE_ID and info.state == DeviceState.ADVERTISING

    networks = await client.scan()
    assert [n.ssid for n in networks] == ["Home-PSK", "Hospital-EAP", "Cafe-Portal", "Weak-Signal"]

    connecting, connected = await provision(client, HOME)
    assert connecting == Status(StatusState.CONNECTING)
    assert connected == Status(
        StatusState.CONNECTED, ip="192.168.1.23", internet=InternetStatus.FULL
    )
    await settle()
    assert device.state == DeviceState.PROVISIONED
    await client.close()


async def test_wrong_code_sends_nothing_after_hello_and_the_retype_works(
    device: Device,
) -> None:
    client, wire = await connect(device)
    with pytest.raises(WrongCodeError):
        await client.handshake(WRONG_CODE)
    assert wire.session_types() == ["hello"]  # no confirm: a fake device learns nothing
    with pytest.raises(ProtocolError):
        await provision(client, HOME)  # no session: refused before anything is sent
    assert wire.messages(Channel.COMMAND) == []  # no set_credentials on the wire, ever

    await client.handshake(CODE)  # Q4: a new hello on the same connection
    assert wire.session_types() == ["hello", "hello", "confirm"]
    assert len(await client.scan()) == 4


async def test_retry_after_failed(device: Device) -> None:
    client, _ = await connect(device)
    await client.handshake(CODE)
    assert (await provision(client, WRONG_PSK))[-1] == Status(
        StatusState.FAILED, reason=FailureReason.WRONG_PASSWORD
    )
    assert device.state == DeviceState.SESSION_ESTABLISHED  # the session stays (SPEC §8.6)
    assert (await provision(client, HOME))[-1].state == StatusState.CONNECTED


# --- handshake refusals -----------------------------------------------------------------


async def test_rate_limited_says_how_long_to_wait(device: Device) -> None:
    client, _ = await connect(device)
    for _ in range(5):
        with pytest.raises(WrongCodeError):
            await client.handshake(WRONG_CODE)
    with pytest.raises(RateLimitedError) as refused:
        await client.handshake(CODE)
    assert refused.value.retry_after_s == 30  # Q5
    await device.time.advance(30)
    await client.handshake(CODE)  # the client is still usable (Q35)


async def test_hello_after_the_window_closed_gets_no_reply(device: Device) -> None:
    client, _ = await connect(device, wait_s=NO_REPLY_S)
    await device.time.advance(300)  # the pairing window expires; the connection stays
    assert not device.advertising
    with pytest.raises(NoReplyError):  # Q22: no reply at all
        await client.handshake(CODE)


async def test_a_different_device_id_is_a_protocol_error(device: Device) -> None:
    client, wire = await connect(device)
    device.transport.set_info(encode(Info(device_id="TETON-000000", state=DeviceState.ADVERTISING)))
    with pytest.raises(ProtocolError):
        await client.handshake(CODE)
    assert wire.session_types() == ["hello"]  # checked before the tag: nothing sent (Q34)


async def test_unreadable_info_is_a_protocol_error(device: Device) -> None:
    client, _ = await connect(device)
    device.transport.set_info(b"{not json")
    with pytest.raises(ProtocolError):
        await client.read_info()


# --- commands -------------------------------------------------------------------------


async def test_unsupported_security_is_refused_and_the_session_stays(device: Device) -> None:
    client, _ = await connect(device)
    await client.handshake(CODE)
    with pytest.raises(DeviceRefusedError) as refused:
        await provision(client, SetCredentials(ssid="Cafe", security=Security.OPEN))
    assert refused.value.code == ErrorCode.UNSUPPORTED_SECURITY
    assert len(await client.scan()) == 4


async def test_no_result_in_time(tmp_path: Path) -> None:
    release = asyncio.Event()

    async def stuck(_: float) -> None:
        await release.wait()

    d = Device(tmp_path, wifi=FakeWifiBackend(sleep=stuck))
    await d.start()
    try:
        client, _ = await connect(d, wait_s=NO_REPLY_S)
        await client.handshake(CODE)
        statuses = client.provision(HOME)
        assert await anext(statuses) == Status(StatusState.CONNECTING)
        with pytest.raises(NoReplyError):  # the flow says "didn't report back in time"
            await anext(statuses)
    finally:
        release.set()  # the device's attempt always finishes
        await settle()
        await d.stop()


# --- the connection ------------------------------------------------------------------


async def test_connect_while_not_advertising_fails(device: Device) -> None:
    client = ProvisioningClient(Wire(device.transport))  # no [P]
    with pytest.raises(ConnectionLostError):
        await client.connect()


async def test_device_powered_off_mid_session(device: Device) -> None:
    client, _ = await connect(device)
    await client.handshake(CODE)
    await device.press(Button.POWER)
    with pytest.raises(ConnectionLostError):
        await client.scan()


# --- secrets ----------------------------------------------------------------------------


async def test_errors_and_logs_carry_no_secrets(device: Device) -> None:
    errors: list[Exception] = []
    with capture_logs() as logs:
        client, _ = await connect(device)
        try:
            await client.handshake(WRONG_CODE)
        except WrongCodeError as exc:
            errors.append(exc)
        await client.handshake(CODE)
        try:
            await provision(client, SetCredentials(ssid="Cafe", security=Security.OPEN))
        except DeviceRefusedError as exc:
            errors.append(exc)
        await provision(client, HOME)
    assert len(errors) == 2
    text = repr(logs) + " ".join(f"{e!s} {e!r}" for e in errors)
    for secret in (CODE, WRONG_CODE, "7K2M-QX9D", HOME_PSK_PASSWORD):
        assert secret not in text
