# Tests for the simulation transport (Unix socket): the wire format, one configurator at a
# time, discovery, malformed records on both sides, the socket file (stale, live, not a
# socket, permissions), and the real protocol through it: ProvisioningClient against
# DeviceService. The transport contract shared with the memory pair is in
# test_transport_contract.py (review Q71). No fixed sleeps: every wait ends on an event.

import asyncio
import io
import os
import socket
import stat
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from fake_configurator import CODE, DEVICE_ID, FakeTime, instant, settle

from teton_common.messages import Info, encode
from teton_common.protocol import CHUNK_SIZE_BYTES, Channel, DeviceState
from teton_common.sim_wire import (
    CLIENT_RECEIVES,
    DEVICE_RECEIVES,
    SOCKET_NAME,
    WireError,
    pack,
    read_record,
    socket_path,
)
from teton_common.transport import TransportError
from teton_configurator.client import ConnectionLostError, ProvisioningClient
from teton_configurator.flow import FoundDevice
from teton_configurator.sim_client import SimClientTransport, discover
from teton_device.buttons import Button, QueueButtons, StatusLine
from teton_device.service import DeviceService
from teton_device.sim_server import SimServerTransport
from teton_device.storage import Storage
from teton_device.wifi.fake import FakeWifiBackend

WAIT_S = 2  # real seconds; never reached when the other side answers
MARKER = b"NEEDLE-bytes"  # a payload that must never show up in a log line


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / SOCKET_NAME


@pytest.fixture
async def server(path: Path) -> AsyncIterator[SimServerTransport]:
    """Started and advertising, with an info value set."""
    s = SimServerTransport(path)
    s.set_info(b'{"v":1}')
    await s.start()
    await s.set_advertising(True)
    yield s
    await s.close()


def _info_json() -> bytes:
    return encode(Info(device_id=DEVICE_ID, state=DeviceState.ADVERTISING))


async def until(condition: Callable[[], bool]) -> None:
    """Let the event loop run until `condition` holds (a few turns, at most WAIT_S)."""
    async with asyncio.timeout(WAIT_S):
        while not condition():
            await asyncio.sleep(0)


def reader_with(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


# --- the wire format -----------------------------------------------------------------------


async def test_records_round_trip() -> None:
    reader = reader_with(pack(Channel.SESSION, b"x" * 20) + pack(Channel.INFO, b""))
    assert await read_record(reader, DEVICE_RECEIVES) == (Channel.SESSION, b"x" * 20)
    assert await read_record(reader, DEVICE_RECEIVES) == (Channel.INFO, b"")
    assert await read_record(reader, DEVICE_RECEIVES) is None  # a clean end


def test_record_header_is_channel_then_big_endian_length() -> None:
    assert pack(Channel.STATUS, b"ab") == b"\x03\x00\x02ab"


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"\x09\x00\x01" + MARKER[:1], "unknown channel byte 9"),
        (pack(Channel.STATUS, b"x"), "not allowed in this direction"),  # notify-only
        (b"\x01\x00\x15" + bytes(21), "limit is 20"),
        (pack(Channel.INFO, MARKER), "limit is 0"),  # an info request is empty
        (b"\x01\x00", "inside a header"),
        (b"\x01\x00\x0c" + MARKER[:5], "ended after 5"),
    ],
)
async def test_malformed_records_are_rejected(data: bytes, reason: str) -> None:
    with pytest.raises(WireError, match=reason) as exc:
        await read_record(reader_with(data), DEVICE_RECEIVES)
    assert MARKER.decode()[:5] not in str(exc.value)  # never the bytes


async def test_info_response_fits_one_gatt_read() -> None:
    await read_record(reader_with(pack(Channel.INFO, bytes(180))), CLIENT_RECEIVES)
    with pytest.raises(WireError):
        await read_record(reader_with(pack(Channel.INFO, bytes(181))), CLIENT_RECEIVES)


def test_socket_path_in_the_runtime_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert socket_path() == Path("/run/user/1000/teton-sim.sock")


def test_socket_path_falls_back_to_a_per_user_temp_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr("tempfile.tempdir", None)  # re-read TMPDIR
    assert socket_path() == tmp_path / f"teton-sim-{os.getuid()}.sock"


# --- one configurator at a time, discovery (review Q64, Q70, Q72) ------------------------


async def test_discover_finds_the_device_only_while_advertising(
    path: Path, server: SimServerTransport
) -> None:
    server.set_info(_info_json())
    assert await discover(path) == [FoundDevice(name="Teton-9A2C", signal=100, address=str(path))]
    await server.set_advertising(False)
    assert await discover(path) == []
    with pytest.raises(TransportError):
        await SimClientTransport(path).connect()


async def test_discover_without_a_device_finds_nothing(path: Path) -> None:
    assert await discover(path) == []


async def test_a_second_configurator_is_refused_until_the_first_leaves(
    path: Path, server: SimServerTransport
) -> None:
    server.set_info(_info_json())
    first, second = SimClientTransport(path), SimClientTransport(path)
    await first.connect()
    with pytest.raises(TransportError):
        await second.connect()
    assert await discover(path) == []  # like BLE: a connected device isn't advertising

    await first.close()
    await second.connect()  # the slot was freed before close() returned
    await second.close()


async def test_connect_right_after_discover_is_never_refused(
    path: Path, server: SimServerTransport
) -> None:
    server.set_info(_info_json())
    for _ in range(5):
        [found] = await discover(path)  # its probe connection has fully gone (Q70)
        transport = SimClientTransport(Path(found.address))
        await transport.connect()
        await transport.close()


async def test_advertising_on_again_still_refuses_while_connected(
    path: Path, server: SimServerTransport
) -> None:
    first = SimClientTransport(path)
    await first.connect()
    await server.set_advertising(False)
    await server.set_advertising(True)  # [P] pressed during the session
    with pytest.raises(TransportError):
        await SimClientTransport(path).connect()
    await first.close()


# --- malformed records drop the connection (review Q67) -----------------------------------


@pytest.mark.parametrize(
    "record",
    [
        b"\x07\x00\x05" + MARKER[:5],  # unknown channel
        b"\x02\x00\x15" + MARKER + bytes(9),  # 21 bytes on command
        pack(Channel.STATUS, MARKER),  # the configurator can't send on status
        pack(Channel.INFO, MARKER),  # an info request carries nothing
        b"\x01\x00\x10" + MARKER[:4],  # short: the connection ends inside the record
    ],
)
async def test_a_malformed_record_from_the_configurator_drops_it(
    path: Path, server: SimServerTransport, log_stream: io.StringIO, record: bytes
) -> None:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(record)
    writer.write_eof()
    async with asyncio.timeout(WAIT_S):
        assert await reader.read() == b""  # the device closed the connection
    writer.close()
    await until(lambda: "sim_client_left" in log_stream.getvalue())

    logs = log_stream.getvalue()
    assert "sim_bad_record" in logs
    assert MARKER.decode()[:4] not in logs  # the bytes are never logged
    client = SimClientTransport(path)
    await client.connect()  # the slot is free again
    await client.close()


async def test_a_malformed_record_from_the_device_drops_the_connection(
    path: Path, log_stream: io.StringIO
) -> None:
    async def bad_device(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readexactly(3)  # the info request
        writer.write(pack(Channel.STATUS, b"") + b"\x02\x00\x04" + MARKER[:4])  # command
        await writer.drain()
        await reader.read()
        writer.close()

    fake = await asyncio.start_unix_server(bad_device, path=str(path))
    try:
        with pytest.raises(TransportError):
            await SimClientTransport(path).connect()
    finally:
        fake.close()
        await fake.wait_closed()
    logs = log_stream.getvalue()
    assert "sim_bad_record" in logs
    assert MARKER.decode()[:4] not in logs


# --- the socket file (review Q68, Q69) ------------------------------------------------------


async def test_a_stale_socket_file_is_replaced(path: Path) -> None:
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(path))
    stale.close()  # the file stays, nobody listens: a device that crashed
    assert path.exists()

    server = SimServerTransport(path)
    await server.start()
    await server.set_advertising(True)
    client = SimClientTransport(path)
    await client.connect()
    await client.close()
    await server.close()
    assert not path.exists()  # removed on close


async def test_a_running_device_is_never_replaced(path: Path, server: SimServerTransport) -> None:
    second = SimServerTransport(path)
    with pytest.raises(TransportError, match="already running"):
        await second.start()
    await second.close()  # must not remove the first device's socket
    assert path.exists()
    client = SimClientTransport(path)
    await client.connect()  # the first device still serves
    await client.close()


async def test_a_file_that_is_not_a_socket_is_left_alone(path: Path) -> None:
    path.write_text("not a socket")
    with pytest.raises(TransportError, match="not a socket"):
        await SimServerTransport(path).start()
    assert path.read_text() == "not a socket"


async def test_only_the_user_can_connect(path: Path, server: SimServerTransport) -> None:
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


async def test_advertising_needs_a_started_server(path: Path) -> None:
    with pytest.raises(TransportError):
        await SimServerTransport(path).set_advertising(True)


async def test_chunks_over_the_socket_are_at_most_20_bytes(
    path: Path, server: SimServerTransport
) -> None:
    client = SimClientTransport(path)
    await client.connect()
    with pytest.raises(TransportError):
        await client.send(Channel.COMMAND, bytes(CHUNK_SIZE_BYTES + 1))
    await client.close()


# --- the real protocol over the socket ------------------------------------------------------


class SimDevice:
    """A running DeviceService on the socket, with fake Wi-Fi, injected buttons and fake
    time. The configurator side is the real ProvisioningClient."""

    def __init__(self, path: Path, state_dir: Path) -> None:
        self.transport = SimServerTransport(path)
        self.buttons = QueueButtons()
        self.time = FakeTime()
        storage = Storage(state_dir, new_code=lambda: CODE, new_id=lambda: DEVICE_ID)
        self.service = DeviceService(
            self.transport,
            FakeWifiBackend(sleep=instant),
            storage,
            self.buttons,
            clock=self.time,
            sleep=self.time.sleep,
            status_line=StatusLine(io.StringIO()),
        )
        self.task = asyncio.create_task(self.service.run())

    async def pair(self) -> None:
        self.buttons.press(Button.PAIR)
        await until(lambda: self.transport._advertising)

    async def power_off(self) -> None:
        self.buttons.press(Button.POWER)
        await asyncio.wait_for(self.task, WAIT_S)


@pytest.fixture
async def sim_device(path: Path, state_dir: Path) -> AsyncIterator[SimDevice]:
    d = SimDevice(path, state_dir)
    await settle()
    yield d
    if not d.task.done():
        await d.power_off()


def client_for(path: Path) -> ProvisioningClient:
    return ProvisioningClient(
        SimClientTransport(path), reply_wait_s=WAIT_S, scan_wait_s=WAIT_S, result_wait_s=WAIT_S
    )


async def test_handshake_and_scan_through_the_real_client_and_device(
    path: Path, sim_device: SimDevice
) -> None:
    assert await discover(path) == []  # nothing before [P]
    await sim_device.pair()
    [found] = await discover(path)
    assert found.name == "Teton-9A2C"

    client = client_for(Path(found.address))
    await client.connect()
    info = await client.handshake(CODE)
    assert info.device_id == DEVICE_ID
    networks = await client.scan()  # the scan list: about 15 chunks of 20 bytes
    assert [n.ssid for n in networks] == ["Home-PSK", "Hospital-EAP", "Cafe-Portal", "Weak-Signal"]
    await client.close()

    await sim_device.power_off()
    assert not path.exists()


async def test_a_device_that_powers_off_mid_session_is_a_lost_connection(
    path: Path, sim_device: SimDevice
) -> None:
    await sim_device.pair()
    client = client_for(path)
    await client.connect()
    await client.handshake(CODE)
    await sim_device.power_off()
    with pytest.raises(ConnectionLostError):
        await client.scan()
    await client.close()
