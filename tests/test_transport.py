# Tests for FramedTransport: whole messages over a chunk transport, per-channel reassembly,
# reset and recovery from framing errors.

import asyncio
from collections.abc import AsyncIterator

import pytest

from teton_common.memory_transport import MemoryClientTransport, MemoryDeviceTransport, pair
from teton_common.protocol import CHUNK_SIZE_BYTES, Channel
from teton_common.transport import FramedTransport, Receiver

Ends = tuple[MemoryDeviceTransport, MemoryClientTransport]


class Messages:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def __call__(self, message: bytes) -> None:
        self.queue.put_nowait(message)

    async def get(self) -> bytes:
        return await asyncio.wait_for(self.queue.get(), timeout=1)


class ChunkSpy:
    """Wraps a transport to record the chunks sent through it."""

    def __init__(self, inner: MemoryClientTransport) -> None:
        self.inner = inner
        self.sent: list[bytes] = []

    def on_receive(self, channel: Channel, cb: Receiver) -> None:
        self.inner.on_receive(channel, cb)

    async def send(self, channel: Channel, data: bytes) -> None:
        self.sent.append(data)
        await self.inner.send(channel, data)


@pytest.fixture
async def ends() -> AsyncIterator[Ends]:
    device, client = pair()
    await device.start()
    await device.set_advertising(True)
    yield device, client
    await client.close()
    await device.close()


async def test_message_is_chunked_and_reassembled(ends: Ends) -> None:
    device, client = ends
    received = Messages()
    FramedTransport(device).on_message(Channel.COMMAND, received)
    spy = ChunkSpy(client)
    await client.connect()

    await FramedTransport(spy).send_message(Channel.COMMAND, b"x" * 150)
    assert await received.get() == b"x" * 150
    assert len(spy.sent) == 8
    assert all(len(chunk) <= CHUNK_SIZE_BYTES for chunk in spy.sent)


async def test_channels_are_reassembled_separately(ends: Ends) -> None:
    device, client = ends
    session, command = Messages(), Messages()
    framed_device = FramedTransport(device)
    framed_device.on_message(Channel.SESSION, session)
    framed_device.on_message(Channel.COMMAND, command)
    await client.connect()

    framed_client = FramedTransport(client)
    await asyncio.gather(
        framed_client.send_message(Channel.SESSION, b"S" * 60),
        framed_client.send_message(Channel.COMMAND, b"C" * 60),
    )
    assert await session.get() == b"S" * 60
    assert await command.get() == b"C" * 60


async def test_reset_drops_a_partial_message(ends: Ends) -> None:
    device, client = ends
    received = Messages()
    framed_device = FramedTransport(device)
    framed_device.on_message(Channel.SESSION, received)
    await client.connect()

    await client.send(Channel.SESSION, b"\x00\x32" + b"p" * 18)  # first chunk of 50 bytes
    await asyncio.sleep(0.01)
    framed_device.reset()  # new session: the half message is gone
    await FramedTransport(client).send_message(Channel.SESSION, b"hello")
    assert await received.get() == b"hello"


async def test_framing_error_is_dropped_and_next_message_arrives(ends: Ends) -> None:
    device, client = ends
    received = Messages()
    FramedTransport(device).on_message(Channel.SESSION, received)
    await client.connect()

    await client.send(Channel.SESSION, b"\x00\x02abc")  # declares 2 bytes, carries 3
    await FramedTransport(client).send_message(Channel.SESSION, b"after the error")
    assert await received.get() == b"after the error"
