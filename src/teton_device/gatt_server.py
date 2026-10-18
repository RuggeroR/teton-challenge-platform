# BLE transport: the GATT service of SPEC §4 (info, session, command, status) through bless
# on BlueZ (D-Bus). See DESIGN §5.
#
# bless is used as is, with no patch (plan_v5 S2): fixed 20-byte chunks and an `info` under
# 180 bytes mean we never need the MTU or the read offsets it hides. It follows the same
# rules as the memory and socket transports: channel directions and 20-byte chunks
# (transport.check_chunk), a notify with no subscriber goes nowhere, and turning advertising
# off keeps the connected client (spike check g). One configurator at a time is the radio's
# doing: the controller stops advertising when a client connects (plan_v5 S1).
#
# Two layers, so the logic is tested without a radio (review Q87):
#   GattServerTransport  implements DeviceTransport: state, checks, ordering, close steps
#   _BlessGattServer     the only code that touches bless, imported in register() (Q84);
#                        the tests inject a fake with the same methods instead
# What differs from BlessServer.start()/stop(): registering doesn't advertise (only [P]
# does, plan_v5 D2, Q82), and close goes step by step, because bless's stop() fails when
# nothing is advertised (Q85).

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from teton_common.protocol import CHANNEL_UUIDS, CHUNK_SIZE_BYTES, SERVICE_UUID, Channel
from teton_common.transport import (
    CLIENT_SENDS,
    DEVICE_SENDS,
    Inbox,
    Receiver,
    TransportError,
    check_chunk,
    check_info,
)

log = structlog.get_logger()


@dataclass(frozen=True)
class CharacteristicSpec:
    """One GATT characteristic, in plain terms (SPEC §4). No encryption flags: there is no
    OS pairing (decision Q2); the protocol encrypts above."""

    channel: Channel
    uuid: str
    read: bool
    write: bool  # write with response, so chunks arrive in order
    notify: bool


CHARACTERISTICS: tuple[CharacteristicSpec, ...] = tuple(
    CharacteristicSpec(
        channel,
        CHANNEL_UUIDS[channel],
        read=channel == Channel.INFO,
        write=channel in CLIENT_SENDS,
        notify=channel in DEVICE_SENDS,
    )
    for channel in Channel
)

OnRead = Callable[[str], bytes]  # characteristic UUID -> value
OnWrite = Callable[[str, bytes], None]  # characteristic UUID, value; synchronous, like bless


class GattServer(Protocol):
    """What the transport needs from the BLE stack. _BlessGattServer is the real one."""

    async def register(
        self, characteristics: Sequence[CharacteristicSpec], on_read: OnRead, on_write: OnWrite
    ) -> None: ...  # connect, find the adapter, register the service; no advertising

    async def start_advertising(self) -> None: ...

    async def stop_advertising(self) -> None: ...

    def notify(self, uuid: str, data: bytes) -> None: ...  # BlueZ tells a subscribed client

    async def unregister(self) -> None: ...

    async def disconnect(self) -> None: ...


MakeServer = Callable[[str], GattServer]  # local name -> server


class GattServerTransport:
    """Device end over BLE. Implements DeviceTransport."""

    def __init__(self, name: str, make_server: MakeServer | None = None) -> None:
        self.name = name  # Teton-XXXX, from the device ID (review Q81)
        self._make_server = make_server or _BlessGattServer
        self._server: GattServer | None = None
        self._info = b""
        self._inbox = Inbox(receives=CLIENT_SENDS, side="device")
        self._channels = {spec.uuid: spec.channel for spec in CHARACTERISTICS}
        self._advertising = False
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise TransportError("transport is closed")
        if self._server is not None:
            return
        server = self._make_server(self.name)
        try:
            await server.register(CHARACTERISTICS, self._on_read, self._on_write)
        except Exception as exc:
            await _quietly("disconnect", server.disconnect)
            if isinstance(exc, TransportError):
                raise
            raise TransportError(f"Bluetooth could not start: {_reason(exc)}") from None
        self._server = server
        self._inbox.start()
        log.info("ble_registered", name=self.name)

    async def set_advertising(self, on: bool) -> None:
        if self._server is None or self._closed:
            raise TransportError("transport is not started")
        if on == self._advertising:
            return  # a second registration would mean a second advertisement (Q82)
        try:
            if on:
                await self._server.start_advertising()
            else:
                await self._server.stop_advertising()  # a connected client stays connected
        except Exception as exc:
            action = "start" if on else "stop"
            raise TransportError(
                f"Bluetooth advertising did not {action}: {_reason(exc)}"
            ) from None
        self._advertising = on

    def set_info(self, data: bytes) -> None:
        check_info(data)
        self._info = bytes(data)

    def on_receive(self, channel: Channel, cb: Receiver) -> None:
        self._inbox.on_receive(channel, cb)

    async def send(self, channel: Channel, data: bytes) -> None:
        check_chunk(channel, data, DEVICE_SENDS, "device")
        if self._closed:
            raise TransportError("transport is closed")
        if self._server is None:
            return  # not started: nobody can be subscribed
        try:
            self._server.notify(CHANNEL_UUIDS[channel], data)
        except Exception as exc:
            # Like a notify that doesn't arrive; the protocol's waits handle it.
            log.warning("ble_notify_failed", channel=channel, reason=_reason(exc))

    async def close(self) -> None:
        """Never raises: power off must always finish (review Q85)."""
        if self._closed:
            return
        self._closed = True
        server, self._server = self._server, None
        if server is not None:
            if self._advertising:
                await _quietly("stop_advertising", server.stop_advertising)
            self._advertising = False
            await _quietly("unregister", server.unregister)
            await _quietly("disconnect", server.disconnect)
        await self._inbox.stop()
        log.info("ble_stopped")

    # Called by bless from its D-Bus handlers: synchronous, so they only read or queue.

    def _on_read(self, uuid: str) -> bytes:
        return self._info if self._channels.get(uuid.lower()) == Channel.INFO else b""

    def _on_write(self, uuid: str, data: bytes) -> None:
        channel = self._channels.get(uuid.lower())
        if channel not in CLIENT_SENDS:
            log.warning("ble_unexpected_write", uuid=uuid)  # BlueZ allows writes only there
            return
        if len(data) > CHUNK_SIZE_BYTES:
            # A client with a large MTU could send more; the protocol says 20 (plan_v5 S2).
            log.warning("ble_chunk_too_long", channel=channel, length=len(data))
            return
        self._inbox.put(channel, data)


class _BlessGattServer:
    """GattServer with bless: BlessServer.start() split in two, and stop() step by step."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._server: Any = None  # bless.BlessServer, once register() has imported bless

    async def register(
        self, characteristics: Sequence[CharacteristicSpec], on_read: OnRead, on_write: OnWrite
    ) -> None:
        try:
            from bless import BlessServer, GATTAttributePermissions, GATTCharacteristicProperties
        except ImportError:
            raise TransportError("the Bluetooth library (bless) is not installed") from None
        # Connects to the system bus and looks up the adapter in a task bless starts here;
        # add_new_service() waits for it, so a missing adapter fails there.
        server = BlessServer(name=self._name)
        self._server = server
        server.read_request_func = lambda char: on_read(str(char.uuid))
        server.write_request_func = lambda char, value: on_write(str(char.uuid), bytes(value))
        try:
            await server.add_new_service(SERVICE_UUID)
        except Exception as exc:
            if "Could not locate bluetooth adapter" in str(exc):
                raise TransportError("no Bluetooth adapter found") from None
            raise
        props = GATTCharacteristicProperties
        for spec in characteristics:
            flags = props(0)
            if spec.read:
                flags |= props.read
            if spec.write:
                flags |= props.write
            if spec.notify:
                flags |= props.notify
            permissions = GATTAttributePermissions.readable | GATTAttributePermissions.writeable
            await server.add_new_characteristic(SERVICE_UUID, spec.uuid, flags, None, permissions)
        # BlessServer.start() without its last line, app.start_advertising().
        server.bus.export(server.app.path, server.app)
        await server.app.register(server.adapter)

    async def start_advertising(self) -> None:
        # Also sets the adapter alias to the local name while advertising (review Q90).
        await self._server.app.start_advertising(self._server.adapter)

    async def stop_advertising(self) -> None:
        await self._server.app.stop_advertising(self._server.adapter)

    def notify(self, uuid: str, data: bytes) -> None:
        self._server.get_characteristic(uuid).value = bytearray(data)
        self._server.update_value(SERVICE_UUID, uuid)  # sets Value: BlueZ notifies

    async def unregister(self) -> None:
        await self._server.app.unregister(self._server.adapter)
        self._server.bus.unexport(self._server.app.path, self._server.app)

    async def disconnect(self) -> None:
        bus = getattr(self._server, "bus", None)  # absent if the system bus never connected
        if bus is not None:
            bus.disconnect()


async def _quietly(step: str, action: Callable[[], Awaitable[None]]) -> None:
    """One close step: a failure is logged and the next step still runs (review Q85)."""
    try:
        await action()
    except Exception as exc:
        log.warning("ble_close_step_failed", step=step, reason=_reason(exc))


def _reason(exc: BaseException) -> str:
    """One line for logs and errors. Exceptions here come from D-Bus and BlueZ, which never
    see a secret: chunks are already encrypted, and the bytes are never part of the text."""
    return (str(exc) or type(exc).__name__).splitlines()[0]
