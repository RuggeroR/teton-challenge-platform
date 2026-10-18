# Tests for the BLE device transport without a radio (review Q87): the characteristic table,
# the local name and the `info` limit, and GattServerTransport against a fake server with the
# methods of _BlessGattServer, the only code that touches bless. The fake records what the
# transport asks for and plays bless's synchronous read and write handlers. The real radio is
# checked by hand in Step 6.1's Verify. Nothing here imports bless.

import asyncio
import io
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from fake_configurator import DEVICE_ID, instant, settle

from teton_common.messages import Info, encode
from teton_common.protocol import (
    CHANNEL_UUIDS,
    MAX_INFO_BYTES,
    Channel,
    DeviceState,
    local_name,
)
from teton_common.transport import TransportError
from teton_device.buttons import Button, QueueButtons, StatusLine
from teton_device.gatt_server import (
    CHARACTERISTICS,
    CharacteristicSpec,
    GattServerTransport,
    OnRead,
    OnWrite,
)
from teton_device.service import DeviceService
from teton_device.storage import Storage
from teton_device.wifi.fake import FakeWifiBackend

NAME = "Teton-9A2C"
INFO, SESSION = CHANNEL_UUIDS[Channel.INFO], CHANNEL_UUIDS[Channel.SESSION]
COMMAND, STATUS = CHANNEL_UUIDS[Channel.COMMAND], CHANNEL_UUIDS[Channel.STATUS]
MARKER = b"NEEDLE-bytes"  # a chunk that must never show up in a log line


class FakeServer:
    """Stands in for _BlessGattServer. `fail` names the steps that raise; `register_error`
    is what register() raises, if anything."""

    def __init__(self, fail: Sequence[str] = (), register_error: Exception | None = None):
        self.name = ""
        self.fail = set(fail)
        self.register_error = register_error
        self.calls: list[str] = []
        self.notified: list[tuple[str, bytes]] = []
        self.characteristics: Sequence[CharacteristicSpec] = ()
        self._on_read: OnRead | None = None
        self._on_write: OnWrite | None = None

    async def register(
        self, characteristics: Sequence[CharacteristicSpec], on_read: OnRead, on_write: OnWrite
    ) -> None:
        self._step("register")
        if self.register_error is not None:
            raise self.register_error
        self.characteristics = characteristics
        self._on_read, self._on_write = on_read, on_write

    async def start_advertising(self) -> None:
        self._step("start_advertising")

    async def stop_advertising(self) -> None:
        self._step("stop_advertising")

    def notify(self, uuid: str, data: bytes) -> None:
        self._step("notify")
        self.notified.append((uuid, data))

    async def unregister(self) -> None:
        self._step("unregister")

    async def disconnect(self) -> None:
        self._step("disconnect")

    # What a configurator does, through bless's handlers.
    def read(self, uuid: str) -> bytes:
        assert self._on_read is not None
        return self._on_read(uuid)

    def write(self, uuid: str, data: bytes) -> None:
        assert self._on_write is not None
        self._on_write(uuid, data)

    def _step(self, step: str) -> None:
        self.calls.append(step)
        if step in self.fail:
            raise RuntimeError(f"org.bluez.Error.Failed: {step} went wrong")


def transport_with(*fail: str) -> tuple[GattServerTransport, FakeServer]:
    server = FakeServer(fail)

    def make_server(name: str) -> FakeServer:
        server.name = name
        return server

    return GattServerTransport(NAME, make_server), server


async def started(*fail: str) -> tuple[GattServerTransport, FakeServer]:
    transport, server = transport_with(*fail)
    await transport.start()
    return transport, server


# --- pure helpers ------------------------------------------------------------------------


def test_characteristic_table_matches_spec() -> None:
    """SPEC §4: info read; session write + notify; command write; status notify."""
    table = {spec.channel: spec for spec in CHARACTERISTICS}
    assert set(table) == set(Channel)
    assert {spec.uuid for spec in CHARACTERISTICS} == set(CHANNEL_UUIDS.values())
    flags = {c: (s.read, s.write, s.notify) for c, s in table.items()}
    assert flags == {
        Channel.INFO: (True, False, False),
        Channel.SESSION: (False, True, True),
        Channel.COMMAND: (False, True, False),
        Channel.STATUS: (False, False, True),
    }


def test_local_name() -> None:
    assert local_name(DEVICE_ID) == "Teton-9A2C"
    assert Info(device_id=DEVICE_ID, state=DeviceState.UNPROVISIONED).name == "Teton-9A2C"


def test_info_limit() -> None:
    transport, _ = transport_with()
    transport.set_info(b"x" * MAX_INFO_BYTES)
    with pytest.raises(TransportError, match="180"):
        transport.set_info(b"x" * (MAX_INFO_BYTES + 1))


def test_building_the_transport_does_not_import_bless() -> None:
    """bless is imported in start() only (review Q84), so the CLI can build the transport
    and the tests can import the module without it."""
    script = (
        "import sys\n"
        "from teton_device.gatt_server import GattServerTransport\n"
        "GattServerTransport('Teton-0000')\n"
        "print('bless' in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


# --- start, advertising and close -------------------------------------------------------


async def test_start_registers_without_advertising() -> None:
    transport, server = await started()
    assert server.name == NAME
    assert server.calls == ["register"]  # [P] opens the window, not start (plan_v5 D2)
    assert server.characteristics == CHARACTERISTICS
    await transport.start()  # again: nothing new
    assert server.calls == ["register"]
    await transport.close()


async def test_advertising_toggles_and_repeats_do_nothing() -> None:
    """A second start_advertising would register a second advertisement (review Q82)."""
    transport, server = await started()
    await transport.set_advertising(True)
    await transport.set_advertising(True)
    await transport.set_advertising(False)
    await transport.set_advertising(False)
    assert server.calls == ["register", "start_advertising", "stop_advertising"]
    await transport.close()


async def test_advertising_needs_a_started_transport() -> None:
    transport, _ = transport_with()
    with pytest.raises(TransportError, match="not started"):
        await transport.set_advertising(True)


async def test_a_failed_advertising_change_is_a_transport_error() -> None:
    transport, server = await started("start_advertising")
    with pytest.raises(TransportError, match="advertising did not start: org.bluez"):
        await transport.set_advertising(True)
    server.fail.clear()
    await transport.set_advertising(True)  # still off as far as we know, so it tries again
    assert server.calls.count("start_advertising") == 2
    await transport.close()


async def test_close_with_advertising_off() -> None:
    """bless's own stop() would fail here: it expects an advertisement (spike, Q85)."""
    transport, server = await started()
    await transport.close()
    await transport.close()  # a second call does nothing
    assert server.calls == ["register", "unregister", "disconnect"]


async def test_close_while_advertising_stops_it_first() -> None:
    transport, server = await started()
    await transport.set_advertising(True)
    await transport.close()
    assert server.calls == [
        "register",
        "start_advertising",
        "stop_advertising",
        "unregister",
        "disconnect",
    ]


async def test_close_logs_a_failing_step_and_goes_on(log_stream: io.StringIO) -> None:
    transport, server = await started("stop_advertising", "unregister")
    await transport.set_advertising(True)
    await transport.close()  # does not raise
    assert server.calls[-3:] == ["stop_advertising", "unregister", "disconnect"]
    logs = log_stream.getvalue()
    assert '"step": "stop_advertising"' in logs and '"step": "unregister"' in logs


async def test_close_before_start() -> None:
    transport, server = transport_with()
    await transport.close()
    with pytest.raises(TransportError, match="closed"):
        await transport.start()


@pytest.mark.parametrize(
    ("error", "text"),
    [
        (TransportError("no Bluetooth adapter found"), "no Bluetooth adapter found"),
        (RuntimeError("org.freedesktop.DBus.Error.ServiceUnknown\nmore"), "Bluetooth could not"),
    ],
)
async def test_startup_errors_are_one_line_transport_errors(error: Exception, text: str) -> None:
    server = FakeServer(register_error=error)
    transport = GattServerTransport(NAME, lambda name: server)
    with pytest.raises(TransportError) as exc_info:
        await transport.start()
    assert text in str(exc_info.value) and "\n" not in str(exc_info.value)
    assert server.calls == ["register", "disconnect"]  # whatever was opened is let go


# --- reads, writes and notifications ----------------------------------------------------


async def test_reads_return_info() -> None:
    transport, server = await started()
    transport.set_info(b'{"v":1}')
    assert server.read(INFO) == b'{"v":1}'
    assert server.read(INFO.upper()) == b'{"v":1}'  # UUIDs compare case-insensitively
    assert server.read(SESSION) == b""
    await transport.close()


async def test_writes_reach_the_receivers_in_order() -> None:
    """bless calls the write handler synchronously; a slow receiver must not let a later
    chunk overtake an earlier one (review Q83)."""
    transport, server = await started()
    received: list[tuple[Channel, bytes]] = []

    def receiver(channel: Channel):  # noqa: ANN202
        async def cb(chunk: bytes) -> None:
            await settle()  # slow
            received.append((channel, chunk))

        return cb

    transport.on_receive(Channel.SESSION, receiver(Channel.SESSION))
    transport.on_receive(Channel.COMMAND, receiver(Channel.COMMAND))
    sent = [(Channel.SESSION if i % 3 else Channel.COMMAND, bytes([i]) * 20) for i in range(12)]
    for channel, chunk in sent:
        server.write(CHANNEL_UUIDS[channel], chunk)
    for _ in range(10_000):  # loop turns, not seconds: ends as soon as all have arrived
        if len(received) == len(sent):
            break
        await asyncio.sleep(0)
    assert received == sent
    await transport.close()


@pytest.mark.parametrize(
    ("uuid", "data"),
    [(STATUS, MARKER), (INFO, MARKER), ("0000180f-0000-1000-8000-00805f9b34fb", MARKER)],
    ids=["status", "info", "unknown"],
)
async def test_writes_elsewhere_are_dropped(
    uuid: str, data: bytes, log_stream: io.StringIO
) -> None:
    transport, server = await started()
    received: list[bytes] = []

    async def cb(chunk: bytes) -> None:
        received.append(chunk)

    transport.on_receive(Channel.SESSION, cb)
    server.write(uuid, data)
    await settle()
    assert received == []
    assert "ble_unexpected_write" in log_stream.getvalue()
    assert MARKER.decode() not in log_stream.getvalue()
    await transport.close()


async def test_a_chunk_over_20_bytes_is_dropped(log_stream: io.StringIO) -> None:
    transport, server = await started()
    received: list[bytes] = []

    async def cb(chunk: bytes) -> None:
        received.append(chunk)

    transport.on_receive(Channel.COMMAND, cb)
    server.write(COMMAND, MARKER * 2)
    server.write(COMMAND, b"ok")
    await settle()
    assert received == [b"ok"]
    assert '"length": 24' in log_stream.getvalue()
    assert MARKER.decode() not in log_stream.getvalue()
    await transport.close()


def test_the_device_does_not_receive_status() -> None:
    transport, _ = transport_with()

    async def cb(chunk: bytes) -> None:
        pass

    with pytest.raises(TransportError):
        transport.on_receive(Channel.STATUS, cb)


async def test_send_notifies() -> None:
    transport, server = await started()
    await transport.send(Channel.STATUS, b"chunk")
    await transport.send(Channel.SESSION, b"hello")
    assert server.notified == [(STATUS, b"chunk"), (SESSION, b"hello")]
    await transport.close()


async def test_send_checks_direction_and_size() -> None:
    transport, server = await started()
    with pytest.raises(TransportError, match="cannot send on command"):
        await transport.send(Channel.COMMAND, b"x")
    with pytest.raises(TransportError, match="20 bytes"):
        await transport.send(Channel.STATUS, b"x" * 21)
    assert server.notified == []
    await transport.close()
    with pytest.raises(TransportError, match="closed"):
        await transport.send(Channel.STATUS, b"x")


async def test_send_before_start_goes_nowhere() -> None:
    transport, _ = transport_with()
    await transport.send(Channel.STATUS, b"x")  # nobody can be subscribed yet


async def test_a_failed_notify_is_logged_not_raised(log_stream: io.StringIO) -> None:
    transport, server = await started("notify")
    await transport.send(Channel.STATUS, MARKER)
    assert "ble_notify_failed" in log_stream.getvalue()
    assert MARKER.decode() not in log_stream.getvalue()
    await transport.close()


# --- with the real device service --------------------------------------------------------


async def test_device_service_over_ble(tmp_path: Path) -> None:
    """[P] advertises, [Q] stops advertising and closes; info is served from the start."""
    transport, server = transport_with()
    buttons = QueueButtons()
    service = DeviceService(
        transport,
        FakeWifiBackend(sleep=instant),
        Storage(tmp_path, new_id=lambda: DEVICE_ID),
        buttons,
        sleep=instant_tick,
        status_line=StatusLine(io.StringIO()),
    )
    buttons.press(Button.PAIR)
    buttons.press(Button.POWER)
    await service.run()
    assert server.calls == [
        "register",
        "start_advertising",
        "stop_advertising",
        "unregister",
        "disconnect",
    ]
    expected = encode(Info(device_id=DEVICE_ID, state=DeviceState.ADVERTISING))
    assert transport._info == expected  # the last state published before power off


async def instant_tick(_: float) -> None:
    await settle()
