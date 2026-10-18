# Test helpers shared by test_session.py and test_service.py: a minimal configurator built
# from teton_common.crypto, fake time, and a way to let the memory transport deliver.
# Not a test module (no test_ prefix); pytest puts tests/ on sys.path, so tests import it
# as `fake_configurator`.

import asyncio

from teton_common.crypto import ConfiguratorHandshake, Role, SessionCipher
from teton_common.memory_transport import MemoryClientTransport
from teton_common.messages import (
    HelloReply,
    Message,
    SessionMessage,
    SetCredentials,
    StatusMessage,
    decode_envelope,
    decode_session,
    decode_status,
    encode,
)
from teton_common.protocol import Channel, Security
from teton_common.transport import FramedTransport, Receiver
from teton_device.wifi.fake import HOME_PSK_PASSWORD

DEVICE_ID = "TETON-3F9A2C"
CODE = "7K2MQX9D4HTRB8WZ"
WRONG_CODE = "0123456789ABCDEF"
HOME = SetCredentials(ssid="Home-PSK", security=Security.WPA2_PSK, psk=HOME_PSK_PASSWORD)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def instant(_: float) -> None:
    pass


async def settle() -> None:
    """Let the memory transport deliver everything that is in flight."""
    for _ in range(200):
        await asyncio.sleep(0)


async def receive(queue: asyncio.Queue[bytes]) -> bytes:
    return await asyncio.wait_for(queue.get(), timeout=1)


class Configurator:
    """Just enough of the configurator's half of the protocol to drive the device."""

    def __init__(self, transport: MemoryClientTransport) -> None:
        self.transport = transport
        self.framed = FramedTransport(transport)
        self.session_inbox: asyncio.Queue[bytes] = asyncio.Queue()
        self.status_inbox: asyncio.Queue[bytes] = asyncio.Queue()
        self.framed.on_message(Channel.SESSION, self._put(self.session_inbox))
        self.framed.on_message(Channel.STATUS, self._put(self.status_inbox))
        self.handshake: ConfiguratorHandshake | None = None
        self.cipher: SessionCipher | None = None

    @staticmethod
    def _put(queue: asyncio.Queue[bytes]) -> Receiver:
        async def put(data: bytes) -> None:
            queue.put_nowait(data)

        return put

    async def send_session(self, msg: Message) -> None:
        await self.framed.send_message(Channel.SESSION, encode(msg))

    async def hello(self, code: str = CODE) -> SessionMessage:
        self.handshake = ConfiguratorHandshake(code)
        await self.send_session(self.handshake.hello())
        return decode_session(await receive(self.session_inbox))

    async def establish(self, code: str = CODE) -> StatusMessage:
        """hello, check the device's tag, confirm; returns the first status (ready)."""
        reply = await self.hello(code)
        assert isinstance(reply, HelloReply) and self.handshake is not None
        confirm, keys = self.handshake.on_hello_reply(reply)
        self.cipher = SessionCipher(keys, Role.CONFIGURATOR)
        await self.send_session(confirm)
        return await self.status()

    async def send_plaintext_command(self, plaintext: bytes) -> None:
        assert self.cipher is not None
        envelope = self.cipher.seal(plaintext)
        await self.framed.send_message(Channel.COMMAND, encode(envelope))

    async def command(self, msg: Message) -> None:
        await self.send_plaintext_command(encode(msg))

    async def status(self) -> StatusMessage:
        assert self.cipher is not None
        envelope = decode_envelope(await receive(self.status_inbox))
        return decode_status(self.cipher.open(envelope))

    async def assert_silent(self) -> None:
        await settle()
        assert self.session_inbox.empty() and self.status_inbox.empty()


class FakeTime:
    """A clock plus a sleep() that really suspends until advance() passes its deadline, so
    a loop like the 1 s button tick runs once per simulated second and never spins."""

    def __init__(self) -> None:
        self.now = 1000.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + seconds, future))
        await future

    async def advance(self, seconds: int) -> None:
        """One simulated second at a time, letting everything that wakes up run."""
        for _ in range(seconds):
            self.now += 1
            due = [f for at, f in self._sleepers if at <= self.now]
            self._sleepers = [(at, f) for at, f in self._sleepers if at > self.now]
            for future in due:
                if not future.done():
                    future.set_result(None)
            await settle()
