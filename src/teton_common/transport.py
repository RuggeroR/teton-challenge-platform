# Transport interface: send/receive bytes on the four logical channels
# (info, session, command, status) independent of BLE, socket or memory. See DESIGN §5.
#
# Layers, bottom to top:
#   DeviceTransport / ClientTransport   move chunks of at most 20 bytes (BLE, socket, memory)
#   FramedTransport                     turns chunks into whole messages (framing.py)
#   session.py / client.py              speak the protocol with whole messages

import time
from collections.abc import Awaitable, Callable
from typing import Protocol

import structlog

from teton_common.framing import FramingError, Reassembler, frame
from teton_common.protocol import CHUNK_SIZE_BYTES, Channel

log = structlog.get_logger()

Receiver = Callable[[bytes], Awaitable[None]]

# Who may send on which channel, from the GATT properties in SPEC §4. `info` is never sent
# as chunks: it is a plain read (set_info / read_info).
DEVICE_SENDS = frozenset({Channel.SESSION, Channel.STATUS})  # notify
CLIENT_SENDS = frozenset({Channel.SESSION, Channel.COMMAND})  # write


class TransportError(Exception):
    """The transport can't do what was asked (closed, not connected, wrong channel)."""


def check_chunk(channel: Channel, data: bytes, allowed: frozenset[Channel], side: str) -> None:
    """The BLE rules every transport enforces, so a bug fails in tests and simulation instead
    of only on hardware: who may send on which channel, and chunks of at most 20 bytes."""
    if channel not in allowed:
        raise TransportError(f"the {side} cannot send on {channel}")
    if len(data) > CHUNK_SIZE_BYTES:
        raise TransportError(f"chunks are at most {CHUNK_SIZE_BYTES} bytes")


class DeviceTransport(Protocol):
    """The device side: serves the GATT service and advertises while the window is open."""

    async def start(self) -> None:
        """Register the service. Does not advertise yet."""

    async def set_advertising(self, on: bool) -> None:
        """Pairing window open/closed (plan_v5 D2). Turning it off keeps a connected client."""

    def set_info(self, data: bytes) -> None:
        """The raw JSON served on `info` reads."""

    def on_receive(self, channel: Channel, cb: Receiver) -> None:
        """Call `cb` with every chunk the configurator writes on `channel`."""

    async def send(self, channel: Channel, data: bytes) -> None:
        """Notify one chunk. With no client connected it goes nowhere, like a BLE notify."""

    async def close(self) -> None:
        """Stop serving and advertising (power off)."""


class ClientTransport(Protocol):
    """The configurator side: connects to one device."""

    async def connect(self) -> None:
        """Connect. Only works while the device is advertising."""

    async def read_info(self) -> bytes: ...

    def on_receive(self, channel: Channel, cb: Receiver) -> None:
        """Call `cb` with every chunk the device notifies on `channel`."""

    async def send(self, channel: Channel, data: bytes) -> None:
        """Write one chunk. Raises TransportError when not connected."""

    async def close(self) -> None: ...


class ChunkTransport(Protocol):
    """What FramedTransport needs; both DeviceTransport and ClientTransport provide it."""

    def on_receive(self, channel: Channel, cb: Receiver) -> None: ...

    async def send(self, channel: Channel, data: bytes) -> None: ...


class FramedTransport:
    """Sends and receives whole messages over a chunk transport, one Reassembler per channel."""

    def __init__(self, transport: ChunkTransport, clock: Callable[[], float] = time.monotonic):
        self._transport = transport
        self._clock = clock
        self._reassemblers: dict[Channel, Reassembler] = {}

    async def send_message(self, channel: Channel, payload: bytes) -> None:
        for chunk in frame(payload):
            await self._transport.send(channel, chunk)

    def on_message(self, channel: Channel, cb: Receiver) -> None:
        """Call `cb` with every complete message on `channel`."""
        reassembler = Reassembler(self._clock)
        self._reassemblers[channel] = reassembler

        async def on_chunk(chunk: bytes) -> None:
            try:
                message = reassembler.feed(chunk)
            except FramingError as exc:
                # The reassembler has already reset; the next message can still arrive.
                log.warning("framing_error", channel=channel, reason=str(exc))
                return
            if message is not None:
                await cb(message)

        self._transport.on_receive(channel, on_chunk)

    def reset(self) -> None:
        """Drop partial messages on every channel (a new session starts, SPEC §4.1)."""
        for reassembler in self._reassemblers.values():
            reassembler.reset()
