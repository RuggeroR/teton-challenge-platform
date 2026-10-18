# The transport contract, checked against every transport pair that runs without a radio:
# the in-memory pair and the simulation's Unix socket pair (review Q71). Info reads, ordered
# delivery, advertising and connection rules, BLE channel directions and chunk size, close.
# The `ends` and `make_ends` fixtures (conftest.py) run each test once per transport.

import asyncio
from collections.abc import Callable

import pytest

from teton_common.protocol import Channel
from teton_common.transport import ClientTransport, DeviceTransport, TransportError

Ends = tuple[DeviceTransport, ClientTransport]


class Collector:
    """A receive callback that stores chunks, with a way to wait for them."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def __call__(self, chunk: bytes) -> None:
        self.queue.put_nowait(chunk)

    async def get(self) -> bytes:
        return await asyncio.wait_for(self.queue.get(), timeout=1)


async def settle() -> None:
    """Let the background delivery tasks run."""
    for _ in range(10):
        await asyncio.sleep(0)


async def test_read_info_returns_what_the_device_set(ends: Ends) -> None:
    device, client = ends
    device.set_info(b'{"v":1}')
    await client.connect()
    assert await client.read_info() == b'{"v":1}'
    device.set_info(b'{"v":1,"state":"connecting"}')
    assert await client.read_info() == b'{"v":1,"state":"connecting"}'


async def test_chunks_arrive_on_the_right_channel_in_order(ends: Ends) -> None:
    device, client = ends
    session, command, status = Collector(), Collector(), Collector()
    device.on_receive(Channel.SESSION, session)
    device.on_receive(Channel.COMMAND, command)
    client.on_receive(Channel.STATUS, status)
    await client.connect()

    for i in range(5):
        await client.send(Channel.COMMAND, bytes([i]))
    await client.send(Channel.SESSION, b"s")
    await device.send(Channel.STATUS, b"reply")

    assert [await command.get() for _ in range(5)] == [bytes([i]) for i in range(5)]
    assert await session.get() == b"s"
    assert await status.get() == b"reply"


async def test_send_returns_before_delivery(make_ends: Callable[[], Ends]) -> None:
    # Like a radio: the peer's callback runs later, not inside send().
    device, client = make_ends()
    await device.start()
    await device.set_advertising(True)
    received = Collector()
    device.on_receive(Channel.SESSION, received)
    await client.connect()
    await client.send(Channel.SESSION, b"hello")
    assert received.queue.empty()
    assert await received.get() == b"hello"
    await client.close()
    await device.close()


async def test_connect_needs_advertising(make_ends: Callable[[], Ends]) -> None:
    device, client = make_ends()
    with pytest.raises(TransportError):
        await client.connect()  # device not started
    await device.start()
    with pytest.raises(TransportError):
        await client.connect()  # started, but the pairing window is closed
    await device.set_advertising(True)
    await client.connect()
    await client.close()
    await device.close()


async def test_advertising_off_keeps_the_connected_client(ends: Ends) -> None:
    device, client = ends
    received = Collector()
    client.on_receive(Channel.STATUS, received)
    await client.connect()
    await device.set_advertising(False)  # pairing window expires mid-session
    await device.send(Channel.STATUS, b"still here")
    assert await received.get() == b"still here"


async def test_reconnect_after_close_while_advertising(ends: Ends) -> None:
    _, client = ends
    await client.connect()
    await client.close()
    await client.connect()


async def test_device_send_without_client_is_dropped(ends: Ends) -> None:
    device, _ = ends
    await device.send(Channel.STATUS, b"nobody listens")  # no error


async def test_close_makes_send_fail_on_both_ends(ends: Ends) -> None:
    device, client = ends
    await client.connect()
    await client.close()
    with pytest.raises(TransportError):
        await client.send(Channel.SESSION, b"x")

    await client.connect()
    await device.close()
    with pytest.raises(TransportError):
        await device.send(Channel.STATUS, b"x")
    with pytest.raises(TransportError):
        await client.send(Channel.SESSION, b"x")  # the device went away


async def test_ble_channel_directions_are_enforced(ends: Ends) -> None:
    device, client = ends
    await client.connect()
    with pytest.raises(TransportError):
        await device.send(Channel.COMMAND, b"x")  # command is write-only
    with pytest.raises(TransportError):
        await client.send(Channel.STATUS, b"x")  # status is notify-only
    with pytest.raises(TransportError):
        await client.send(Channel.INFO, b"x")  # info is a plain read
    with pytest.raises(TransportError):
        device.on_receive(Channel.STATUS, Collector())
    with pytest.raises(TransportError):
        client.on_receive(Channel.COMMAND, Collector())


async def test_chunks_are_at_most_20_bytes(ends: Ends) -> None:
    _, client = ends
    await client.connect()
    await client.send(Channel.SESSION, bytes(20))
    with pytest.raises(TransportError):
        await client.send(Channel.SESSION, bytes(21))


async def test_failing_callback_does_not_stop_delivery(ends: Ends) -> None:
    device, client = ends
    received = Collector()
    calls = 0

    async def flaky(chunk: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("bug in a callback")
        await received(chunk)

    device.on_receive(Channel.SESSION, flaky)
    await client.connect()
    await client.send(Channel.SESSION, b"first")
    await client.send(Channel.SESSION, b"second")
    assert await received.get() == b"second"


async def test_old_chunks_do_not_reach_a_new_connection(ends: Ends) -> None:
    device, client = ends
    received = Collector()
    client.on_receive(Channel.STATUS, received)
    await client.connect()
    await client.close()
    await device.send(Channel.STATUS, b"for nobody")
    await client.connect()
    await settle()
    assert received.queue.empty()
