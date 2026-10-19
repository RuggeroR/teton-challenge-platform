# Tests for the BLE configurator transport without a radio (review Q100): RSSI to signal,
# the device list from scan results, and BleClientTransport against a fake shaped like
# bleak's BleakClient (plain UUID strings and bytes, a synchronous notify callback, a
# disconnected_callback). The real radio is checked by hand in Step 6.2's Verify. Nothing
# here imports bleak.

import asyncio
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from fake_configurator import settle

from teton_common.protocol import CHANNEL_UUIDS, SERVICE_UUID, Channel
from teton_common.transport import TransportError
from teton_configurator.ble_client import (
    BleClientTransport,
    BleDiscovery,
    BluetoothUnavailableError,
    found_devices,
    rssi_to_signal,
)
from teton_configurator.flow import FoundDevice

INFO, SESSION = CHANNEL_UUIDS[Channel.INFO], CHANNEL_UUIDS[Channel.SESSION]
COMMAND, STATUS = CHANNEL_UUIDS[Channel.COMMAND], CHANNEL_UUIDS[Channel.STATUS]
MARKER = b"NEEDLE-bytes"


@dataclass
class Device:
    """Stands in for bleak's BLEDevice."""

    address: str
    name: str | None = None


@dataclass
class Adv:
    """Stands in for bleak's AdvertisementData."""

    local_name: str | None
    rssi: int


class Service:
    def __init__(self, uuids: list[str]) -> None:
        self._uuids = uuids

    def get_characteristic(self, uuid: str) -> object | None:
        return object() if uuid in self._uuids else None


class Services:
    def __init__(self, service: Service | None) -> None:
        self._service = service

    def get_service(self, uuid: str) -> Service | None:
        return self._service if uuid == SERVICE_UUID else None


@dataclass
class FakeClient:
    """Shaped like bleak.BleakClient, as far as the transport uses it."""

    device: Any = None
    on_disconnect: Callable[[Any], None] | None = None
    uuids: list[str] = field(default_factory=lambda: list(CHANNEL_UUIDS.values()))
    fail: set[str] = field(default_factory=set)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    callbacks: dict[str, Callable[[Any, bytearray], None]] = field(default_factory=dict)
    info: bytes = b'{"v":1}'

    @property
    def services(self) -> Services:
        return Services(Service(self.uuids) if self.uuids else None)

    async def connect(self) -> None:
        self._step("connect")

    async def start_notify(self, uuid: str, callback: Callable[[Any, bytearray], None]) -> None:
        self._step("start_notify", uuid)
        self.callbacks[uuid] = callback

    async def read_gatt_char(self, uuid: str) -> bytearray:
        self._step("read", uuid)
        return bytearray(self.info)

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool) -> None:
        self._step("write", uuid, data.hex(), str(response))

    async def disconnect(self) -> None:
        self._step("disconnect")

    # What the device and the radio do.
    def notify(self, uuid: str, data: bytes) -> None:
        self.callbacks[uuid](object(), bytearray(data))

    def drop(self) -> None:
        assert self.on_disconnect is not None
        self.on_disconnect(self)

    def _step(self, name: str, *args: str) -> None:
        self.calls.append((name, *args))
        if name in self.fail:
            raise OSError(f"org.bluez.Error.Failed: {name} failed")


def transport_with(client: FakeClient | None = None) -> tuple[BleClientTransport, FakeClient]:
    client = client or FakeClient()

    def make_client(device: Any, on_disconnect: Callable[[Any], None]) -> FakeClient:
        client.device, client.on_disconnect = device, on_disconnect
        return client

    return BleClientTransport(Device("AA:BB"), make_client), client


async def connected(client: FakeClient | None = None) -> tuple[BleClientTransport, FakeClient]:
    transport, client = transport_with(client)
    await transport.connect()
    return transport, client


async def until(condition: Callable[[], bool]) -> None:
    """Loop turns, not seconds: ends as soon as the condition holds."""
    for _ in range(10_000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


# --- pure helpers ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rssi", "signal"), [(-120, 0), (-100, 0), (-90, 20), (-75, 50), (-50, 100), (-30, 100)]
)
def test_rssi_to_signal(rssi: int, signal: int) -> None:
    """NetworkManager's scale: 2 x (dBm + 100), clamped (review Q91)."""
    assert rssi_to_signal(rssi) == signal


def test_found_devices_strongest_first_with_the_advertised_name() -> None:
    results = [
        (Device("11:11", name="laptop-alias"), Adv("Teton-9A2C", -80)),
        (Device("22:22"), Adv("Teton-1B3D", -55)),
        (Device("33:33", name="Teton-4C5E"), Adv(None, -70)),  # no name in this advert
        (Device("44:44"), Adv(None, -95)),
    ]
    assert found_devices(results) == [
        FoundDevice("Teton-1B3D", 90, "22:22"),
        FoundDevice("Teton-4C5E", 60, "33:33"),
        FoundDevice("Teton-9A2C", 40, "11:11"),
        FoundDevice("44:44", 10, "44:44"),
    ]


def test_building_the_transport_does_not_import_bleak() -> None:
    script = (
        "import sys\n"
        "from teton_configurator.ble_client import BleClientTransport, BleDiscovery\n"
        "from teton_configurator import cli\n"
        "BleClientTransport('AA:BB'); BleDiscovery(); cli._radio(simulate=False)\n"
        "print('bleak' in sys.modules)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"


# --- discovery ---------------------------------------------------------------------------


async def test_discovery_keeps_the_scanned_devices_for_connect() -> None:
    """connect(found) gets the BLEDevice itself, so bleak doesn't scan again (review Q93)."""
    seen = Device("22:22")
    scans: list[float] = []

    async def scan(seconds: float) -> list[tuple[Any, Any]]:
        scans.append(seconds)
        return [(seen, Adv("Teton-1B3D", -55))]

    discovery = BleDiscovery(scan)
    (found,) = await discovery.discover()
    assert scans == [10]  # SPEC §9.2
    assert discovery.connect(found).device is seen
    assert discovery.connect(FoundDevice("Teton-X", 50, "99:99")).device == "99:99"


async def test_a_failed_scan_means_bluetooth_is_unavailable() -> None:
    async def scan(seconds: float) -> list[tuple[Any, Any]]:
        raise OSError("No Bluetooth adapters found.")

    with pytest.raises(BluetoothUnavailableError, match="No Bluetooth adapters found"):
        await BleDiscovery(scan).discover()


# --- connect, subscribe, read, write -----------------------------------------------------


async def test_connect_subscribes_before_anything_is_written() -> None:
    """A reply to `hello` can't be missed (review Q95)."""
    transport, client = await connected()
    assert client.device == Device("AA:BB")
    assert client.calls == [("connect",), ("start_notify", SESSION), ("start_notify", STATUS)]
    await transport.send(Channel.SESSION, b"hello")
    assert client.calls[-1] == ("write", SESSION, b"hello".hex(), "True")  # with response
    await transport.close()


async def test_read_info() -> None:
    transport, client = await connected()
    assert await transport.read_info() == b'{"v":1}'
    assert client.calls[-1] == ("read", INFO)
    await transport.close()


@pytest.mark.parametrize("uuids", [[], [INFO, SESSION, STATUS]], ids=["no service", "no command"])
async def test_a_device_without_the_teton_service_is_refused(uuids: list[str]) -> None:
    transport, client = transport_with(FakeClient(uuids=uuids))
    with pytest.raises(TransportError, match="does not offer the Teton service"):
        await transport.connect()
    assert client.calls[-1] == ("disconnect",)
    with pytest.raises(TransportError, match="not connected"):
        await transport.send(Channel.SESSION, b"x")


@pytest.mark.parametrize("step", ["connect", "start_notify"])
async def test_connect_errors_are_transport_errors(step: str) -> None:
    transport, client = transport_with(FakeClient(fail={step}))
    with pytest.raises(TransportError, match=f"could not connect to the device: .*{step}"):
        await transport.connect()
    assert client.calls[-1] == ("disconnect",)


async def test_notifications_reach_the_receivers_in_order() -> None:
    """bleak calls the callback synchronously; Inbox delivers in order (review Q96)."""
    transport, client = await connected()
    received: list[tuple[Channel, bytes]] = []

    def receiver(channel: Channel) -> Callable[[bytes], Any]:
        async def cb(chunk: bytes) -> None:
            await settle()  # slow
            received.append((channel, chunk))

        return cb

    transport.on_receive(Channel.SESSION, receiver(Channel.SESSION))
    transport.on_receive(Channel.STATUS, receiver(Channel.STATUS))
    sent = [(Channel.STATUS if i % 3 else Channel.SESSION, bytes([i]) * 20) for i in range(12)]
    for channel, chunk in sent:
        client.notify(CHANNEL_UUIDS[channel], chunk)
    await until(lambda: len(received) == len(sent))
    assert received == sent
    await transport.close()


def test_the_configurator_does_not_receive_command() -> None:
    transport, _ = transport_with()

    async def cb(chunk: bytes) -> None:
        pass

    with pytest.raises(TransportError):
        transport.on_receive(Channel.COMMAND, cb)


async def test_send_checks_direction_and_size() -> None:
    transport, client = await connected()
    with pytest.raises(TransportError, match="cannot send on status"):
        await transport.send(Channel.STATUS, b"x")
    with pytest.raises(TransportError, match="20 bytes"):
        await transport.send(Channel.COMMAND, b"x" * 21)
    assert not [c for c in client.calls if c[0] == "write"]
    await transport.close()


async def test_a_failed_write_or_read_is_a_transport_error() -> None:
    transport, client = await connected()
    client.fail = {"write", "read"}
    with pytest.raises(TransportError, match="write failed") as exc_info:
        await transport.send(Channel.COMMAND, MARKER)
    assert MARKER.decode() not in str(exc_info.value)
    with pytest.raises(TransportError, match="info read failed"):
        await transport.read_info()
    await transport.close()


# --- disconnects and close ---------------------------------------------------------------


async def test_after_a_disconnect_send_fails_at_once() -> None:
    """No event goes up (Q36); the next send raises, so the client sees the loss (Q97)."""
    transport, client = await connected()
    client.drop()
    writes_before = len(client.calls)
    with pytest.raises(TransportError, match="not connected"):
        await transport.send(Channel.SESSION, b"x")
    with pytest.raises(TransportError, match="not connected"):
        await transport.read_info()
    assert len(client.calls) == writes_before  # bleak wasn't even asked
    await transport.close()


async def test_close_is_idempotent_and_never_raises() -> None:
    transport, client = await connected()
    client.fail = {"disconnect"}
    await transport.close()
    await transport.close()
    assert client.calls.count(("disconnect",)) == 1
    with pytest.raises(TransportError, match="not connected"):
        await transport.send(Channel.SESSION, b"x")


async def test_connect_twice_is_refused() -> None:
    transport, _ = await connected()
    with pytest.raises(TransportError, match="already connected"):
        await transport.connect()
    await transport.close()
