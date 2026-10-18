# Test helpers shared by test_session.py, test_service.py and test_client.py: a low-level
# configurator for driving the device, a running device rig, and fake time.
# Not a test module (no test_ prefix); pytest puts tests/ on sys.path, so tests import it
# as `fake_configurator`.
#
# The Configurator is built on the real client's ClientChannel (framing, inbox, cipher), so
# the protocol exists only once (review Q37). It adds what only tests need: raw replies
# instead of typed errors, invalid plaintext, and checking that nothing arrives.

import asyncio
import io
from pathlib import Path

from teton_common.crypto import ConfiguratorHandshake, Role, SessionCipher
from teton_common.memory_transport import MemoryClientTransport, MemoryDeviceTransport
from teton_common.messages import HelloReply, Message, SetCredentials, decode_info, encode
from teton_common.protocol import Channel, DeviceState, Security
from teton_configurator.client import ClientChannel, DeviceMessage
from teton_device.buttons import Button, QueueButtons, StatusLine
from teton_device.service import DeviceService
from teton_device.storage import Storage
from teton_device.wifi.fake import HOME_PSK_PASSWORD, FakeWifiBackend

DEVICE_ID = "TETON-3F9A2C"
CODE = "7K2MQX9D4HTRB8WZ"
NEW_CODE = "ZYXWVTSRQPNMKJHG"  # the code after the first [R][R]
WRONG_CODE = "0123456789ABCDEF"
HOME = SetCredentials(ssid="Home-PSK", security=Security.WPA2_PSK, psk=HOME_PSK_PASSWORD)
WRONG_PSK = SetCredentials(ssid="Home-PSK", security=Security.WPA2_PSK, psk="not-the-password")
RECEIVE_WAIT_S = 1  # real seconds; the memory transport delivers in a few loop turns


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


class Configurator:
    """Just enough of the configurator's half of the protocol to drive the device, step by
    step, with every reply returned as it came."""

    def __init__(self, transport: MemoryClientTransport) -> None:
        self.transport = transport
        self.channel = ClientChannel(transport)
        self.handshake: ConfiguratorHandshake | None = None

    @property
    def cipher(self) -> SessionCipher | None:
        return self.channel.cipher

    async def send_session(self, msg: Message) -> None:
        await self.channel.send_bytes(Channel.SESSION, encode(msg))

    async def hello(self, code: str = CODE) -> DeviceMessage:
        self.handshake = ConfiguratorHandshake(code)
        await self.send_session(self.handshake.hello())
        return await self.session_reply()

    async def establish(self, code: str = CODE) -> DeviceMessage:
        """hello, check the device's tag, confirm; returns the first status (ready)."""
        reply = await self.hello(code)
        assert isinstance(reply, HelloReply) and self.handshake is not None
        confirm, keys = self.handshake.on_hello_reply(reply)
        self.channel.cipher = SessionCipher(keys, Role.CONFIGURATOR)
        await self.send_session(confirm)
        return await self.status()

    async def send_plaintext_command(self, plaintext: bytes) -> None:
        await self.channel.send_sealed(plaintext)

    async def command(self, msg: Message) -> None:
        await self.send_plaintext_command(encode(msg))

    async def session_reply(self) -> DeviceMessage:
        return await self._receive(Channel.SESSION)

    async def status(self) -> DeviceMessage:
        return await self._receive(Channel.STATUS)

    async def assert_silent(self) -> None:
        await settle()
        assert not self.channel.pending()

    async def _receive(self, expected: Channel) -> DeviceMessage:
        channel, msg = await self.channel.receive(RECEIVE_WAIT_S)
        assert channel == expected, f"{msg.TYPE} arrived on {channel}, expected {expected}"
        return msg


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


class Device:
    """A running DeviceService plus everything it talks to: memory transport, fake Wi-Fi,
    injected buttons, fake time. `configurator` is a low-level client on that transport."""

    def __init__(
        self,
        tmp_path: Path,
        wifi: FakeWifiBackend | None = None,
        transport: MemoryDeviceTransport | None = None,
    ) -> None:
        self.time = FakeTime()
        self.transport = transport or MemoryDeviceTransport()
        self.configurator = Configurator(MemoryClientTransport(self.transport))
        codes = iter([CODE, NEW_CODE])
        self.storage = Storage(tmp_path, new_code=lambda: next(codes), new_id=lambda: DEVICE_ID)
        self.wifi = wifi or FakeWifiBackend(sleep=instant)
        self.buttons = QueueButtons()
        self.screen = io.StringIO()  # not a terminal: plain lines
        self.service = DeviceService(
            self.transport,
            self.wifi,
            self.storage,
            self.buttons,
            clock=self.time,
            sleep=self.time.sleep,
            status_line=StatusLine(self.screen),
        )
        self.task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self.service.run())
        await settle()

    async def press(self, *buttons: Button) -> None:
        self.buttons.press(*buttons)
        await settle()

    async def connect(self) -> None:
        await self.configurator.transport.connect()

    @property
    def state(self) -> DeviceState:
        return self.service.state()

    @property
    def advertising(self) -> bool:
        return self.transport._advertising  # what the radio is doing right now

    def info_state(self) -> DeviceState:
        return decode_info(self.transport._info).state

    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def stop(self) -> None:
        if self.running():
            self.buttons.press(Button.POWER)
            assert self.task is not None
            await asyncio.wait_for(self.task, 1)


async def provisioned_device(tmp_path: Path) -> Device:
    stored = Storage(tmp_path, new_code=lambda: CODE, new_id=lambda: DEVICE_ID)
    stored.load_or_create()
    stored.mark_provisioned()
    d = Device(tmp_path)
    await d.start()
    return d
