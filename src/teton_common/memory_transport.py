# In-memory paired transport connecting a device and a configurator in one process (tests).
#
# It behaves like the radio, not like a function call: send() returns at once and each end
# delivers incoming chunks in order from its own background task. Calling the peer's
# callback directly would let replies arrive while the sender is still mid-way through
# handling a message, which BLE never does. It also enforces the BLE rules (who may send on
# which channel, 20-byte chunks), so a bug fails here instead of only on hardware.

import asyncio
import contextlib

import structlog

from teton_common.protocol import Channel
from teton_common.transport import (
    CLIENT_SENDS,
    DEVICE_SENDS,
    Receiver,
    TransportError,
    check_chunk,
)

log = structlog.get_logger()


class _Inbox:
    """Incoming chunks for one end, delivered in order by a background task."""

    def __init__(self, receives: frozenset[Channel], side: str) -> None:
        self._receives = receives
        self._side = side
        self._receivers: dict[Channel, Receiver] = {}
        self._queue: asyncio.Queue[tuple[Channel, bytes]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    def on_receive(self, channel: Channel, cb: Receiver) -> None:
        if channel not in self._receives:
            raise TransportError(f"the {self._side} does not receive on {channel}")
        self._receivers[channel] = cb

    def put(self, channel: Channel, chunk: bytes) -> None:
        self._queue.put_nowait((channel, chunk))

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._deliver())

    async def stop(self) -> None:
        task, self._task = self._task, None
        self._queue = asyncio.Queue()  # chunks of an old connection never reach a new one
        if task is None:
            return
        task.cancel()
        if task is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _deliver(self) -> None:
        while True:
            channel, chunk = await self._queue.get()
            cb = self._receivers.get(channel)
            if cb is None:
                continue  # nobody listening, like a notify nobody subscribed to
            try:
                await cb(chunk)
            except Exception:
                # One failing callback must not silently stop all later deliveries.
                log.exception("receiver_failed", side=self._side, channel=channel)


class MemoryDeviceTransport:
    """Device end of an in-memory pair. Implements DeviceTransport."""

    def __init__(self) -> None:
        self._inbox = _Inbox(receives=CLIENT_SENDS, side="device")
        self._info = b""
        self._started = False
        self._advertising = False
        self._closed = False
        self._client: MemoryClientTransport | None = None

    async def start(self) -> None:
        if self._closed:
            raise TransportError("transport is closed")
        self._started = True
        self._inbox.start()

    async def set_advertising(self, on: bool) -> None:
        if not self._started or self._closed:
            raise TransportError("transport is not started")
        self._advertising = on  # a connected client stays connected

    def set_info(self, data: bytes) -> None:
        self._info = bytes(data)

    def on_receive(self, channel: Channel, cb: Receiver) -> None:
        self._inbox.on_receive(channel, cb)

    async def send(self, channel: Channel, data: bytes) -> None:
        check_chunk(channel, data, DEVICE_SENDS, "device")
        if self._closed:
            raise TransportError("transport is closed")
        if self._client is not None:
            self._client._inbox.put(channel, data)
        # No client: dropped, like a BLE notify with no subscriber.

    async def close(self) -> None:
        self._closed = True
        self._advertising = False
        if self._client is not None:
            await self._client._lost()
        await self._inbox.stop()

    # Used by the client end.
    def _accept(self, client: "MemoryClientTransport") -> None:
        if self._closed or not self._advertising:
            raise TransportError("device is not advertising")
        if self._client is not None:
            raise TransportError("device already has a client")
        self._client = client

    def _drop(self, client: "MemoryClientTransport") -> None:
        if self._client is client:
            self._client = None


class MemoryClientTransport:
    """Configurator end of an in-memory pair. Implements ClientTransport."""

    def __init__(self, device: MemoryDeviceTransport) -> None:
        self._device = device
        self._inbox = _Inbox(receives=DEVICE_SENDS, side="configurator")
        self._connected = False

    async def connect(self) -> None:
        if self._connected:
            raise TransportError("already connected")
        self._device._accept(self)
        self._connected = True
        self._inbox.start()

    async def read_info(self) -> bytes:
        self._require_connected()
        return self._device._info

    def on_receive(self, channel: Channel, cb: Receiver) -> None:
        self._inbox.on_receive(channel, cb)

    async def send(self, channel: Channel, data: bytes) -> None:
        check_chunk(channel, data, CLIENT_SENDS, "configurator")
        self._require_connected()
        self._device._inbox.put(channel, data)

    async def close(self) -> None:
        self._device._drop(self)
        await self._lost()

    async def _lost(self) -> None:
        self._connected = False
        await self._inbox.stop()

    def _require_connected(self) -> None:
        if not self._connected:
            raise TransportError("not connected")


def pair() -> tuple[MemoryDeviceTransport, MemoryClientTransport]:
    """A device end and a configurator end wired to each other."""
    device = MemoryDeviceTransport()
    return device, MemoryClientTransport(device)
