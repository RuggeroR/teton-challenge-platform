# BLE transport via bleak: discover devices by service UUID, read `info`, write `session` and
# `command`, receive `session` and `status` notifications. See SPEC §4, §9.2.
#
# The configurator's half of gatt_server.py, with the same rules as the memory and socket
# transports. Only public bleak API and fixed 20-byte chunks, so no MTU handling (plan_v5
# S2). There is no disconnect event (review Q36): when the device goes away, the next send()
# or read_info() raises TransportError, which ProvisioningClient turns into
# ConnectionLostError. bleak is imported only when scanning or connecting, so building
# these classes (the CLI, the tests, simulation) never loads it.

from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol

import structlog

from teton_common.protocol import CHANNEL_UUIDS, SERVICE_UUID, Channel
from teton_common.transport import (
    CLIENT_SENDS,
    DEVICE_SENDS,
    Inbox,
    Receiver,
    TransportError,
    check_chunk,
)
from teton_configurator.flow import FoundDevice

log = structlog.get_logger()

SCAN_S = 10  # SPEC §9.2
CONNECT_TIMEOUT_S = 10  # review Q95


class BluetoothUnavailableError(TransportError):
    """The scan couldn't run: no adapter, or Bluetooth is off (review Q99)."""


def rssi_to_signal(rssi: int) -> int:
    """dBm -> 0-100, NetworkManager's scale for Wi-Fi: -100 -> 0, -50 or stronger -> 100
    (review Q91), so device bars and network bars mean the same."""
    return max(0, min(100, 2 * (rssi + 100)))


def found_devices(results: Iterable[tuple[Any, Any]]) -> list[FoundDevice]:
    """bleak's (BLEDevice, AdvertisementData) pairs as FoundDevices, strongest first. The
    name is the advertised local name, else the name BlueZ knows (review Q92)."""
    found = [
        FoundDevice(
            name=adv.local_name or device.name or device.address,
            signal=rssi_to_signal(adv.rssi),
            address=device.address,
        )
        for device, adv in results
    ]
    return sorted(found, key=lambda d: (-d.signal, d.name))


# ---------------------------------------------------------------------------
# The client: what the transport needs from bleak.BleakClient (a fake in the tests)


class GattClient(Protocol):
    @property
    def services(self) -> Any: ...  # get_service(uuid) -> service with get_characteristic()

    async def connect(self) -> None: ...

    async def start_notify(self, uuid: str, callback: Callable[[Any, bytearray], None]) -> None: ...

    async def read_gatt_char(self, uuid: str) -> bytearray: ...

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool) -> None: ...

    async def disconnect(self) -> None: ...


# (device or address, called when the link drops) -> client
MakeClient = Callable[[Any, Callable[[Any], None]], GattClient]


def _bleak_client(device: Any, on_disconnect: Callable[[Any], None]) -> GattClient:
    from bleak import BleakClient

    return BleakClient(device, disconnected_callback=on_disconnect, timeout=CONNECT_TIMEOUT_S)


class BleClientTransport:
    """Configurator end over BLE. Implements ClientTransport."""

    def __init__(self, device: Any, make_client: MakeClient | None = None) -> None:
        self.device = device  # a bleak BLEDevice from the scan, or an address (review Q93)
        self._make_client = make_client or _bleak_client
        self._client: GattClient | None = None
        self._inbox = Inbox(receives=DEVICE_SENDS, side="configurator")
        self._connected = False

    async def connect(self) -> None:
        """Connect, check it is a Teton device, and subscribe to `session` and `status`
        before anything is written, so no reply can be missed (review Q95)."""
        if self._connected:
            raise TransportError("already connected")
        client = self._make_client(self.device, self._on_disconnect)
        self._client = client
        try:
            await client.connect()  # fails if the device isn't advertising (Q64)
            self._connected = True
            _check_service(client.services)
            self._inbox.start()
            for channel in (Channel.SESSION, Channel.STATUS):
                await client.start_notify(CHANNEL_UUIDS[channel], self._notifier(channel))
        except Exception as exc:
            await self.close()
            if isinstance(exc, TransportError):
                raise
            raise TransportError(f"could not connect to the device: {_reason(exc)}") from None
        log.info("ble_connected")

    async def read_info(self) -> bytes:
        client = self._require_connected()
        return bytes(
            await _guarded("info read", client.read_gatt_char(CHANNEL_UUIDS[Channel.INFO]))
        )

    def on_receive(self, channel: Channel, cb: Receiver) -> None:
        self._inbox.on_receive(channel, cb)

    async def send(self, channel: Channel, data: bytes) -> None:
        check_chunk(channel, data, CLIENT_SENDS, "configurator")
        client = self._require_connected()
        # With response: each chunk is confirmed before the next goes (review Q98).
        await _guarded("write", client.write_gatt_char(CHANNEL_UUIDS[channel], data, response=True))

    async def close(self) -> None:
        """Idempotent and never raises (review Q97)."""
        client, self._client = self._client, None
        self._connected = False
        if client is not None:
            try:
                await client.disconnect()
            except Exception as exc:
                log.debug("ble_disconnect_failed", reason=_reason(exc))
        await self._inbox.stop()

    # Called by bleak, synchronously: they only queue or mark (review Q96, Q97).

    def _notifier(self, channel: Channel) -> Callable[[Any, bytearray], None]:
        def on_notify(_characteristic: Any, data: bytearray) -> None:
            self._inbox.put(channel, bytes(data))

        return on_notify

    def _on_disconnect(self, _client: Any) -> None:
        if self._connected:
            log.info("ble_disconnected")
        self._connected = False  # the next send() or read_info() fails at once

    def _require_connected(self) -> GattClient:
        if not self._connected or self._client is None:
            raise TransportError("not connected")
        return self._client


def _check_service(services: Any) -> None:
    service = services.get_service(SERVICE_UUID) if services is not None else None
    if service is None or any(
        service.get_characteristic(u) is None for u in CHANNEL_UUIDS.values()
    ):
        raise TransportError("the device does not offer the Teton service")


# ---------------------------------------------------------------------------
# Discovery

Scan = Callable[[float], Awaitable[list[tuple[Any, Any]]]]  # seconds -> (device, adv) pairs


async def _bleak_scan(seconds: float) -> list[tuple[Any, Any]]:
    from bleak import BleakScanner

    found = await BleakScanner.discover(
        timeout=seconds, service_uuids=[SERVICE_UUID], return_adv=True
    )
    return list(found.values())


class BleDiscovery:
    """discover() for run_flow, and connect(found) for the devices it found: it keeps the
    scanned BLEDevices, so bleak doesn't have to scan again to connect (review Q93)."""

    def __init__(
        self,
        scan: Scan | None = None,
        make_client: MakeClient | None = None,
        scan_s: float = SCAN_S,
    ) -> None:
        self._scan = scan or _bleak_scan
        self._make_client = make_client
        self._scan_s = scan_s
        self._devices: dict[str, Any] = {}

    async def discover(self) -> list[FoundDevice]:
        try:
            results = await self._scan(self._scan_s)
        except Exception as exc:
            raise BluetoothUnavailableError(f"the Bluetooth scan failed: {_reason(exc)}") from None
        self._devices = {device.address: device for device, _ in results}
        return found_devices(results)

    def connect(self, found: FoundDevice) -> BleClientTransport:
        device = self._devices.get(found.address, found.address)
        return BleClientTransport(device, self._make_client)


async def _guarded[T](what: str, call: Awaitable[T]) -> T:
    try:
        return await call
    except Exception as exc:
        raise TransportError(f"the {what} failed: {_reason(exc)}") from None


def _reason(exc: BaseException) -> str:
    """One line. bleak and BlueZ errors never contain our data: chunks are encrypted, and
    the bytes are never part of the text."""
    return (str(exc) or type(exc).__name__).splitlines()[0]
