# Test helpers shared by the device, client, flow and end-to-end tests: a low-level
# configurator for driving the device, a running device rig, fake time, a recording
# transport, a scripted technician and recording Wi-Fi backends.
# Not a test module (no test_ prefix); pytest puts tests/ on sys.path, so tests import it
# as `fake_configurator`. The fixtures built on these live in conftest.py (review Q48).
#
# The Configurator is built on the real client's ClientChannel (framing, inbox, cipher), so
# the protocol exists only once (review Q37). It adds what only tests need: raw replies
# instead of typed errors, invalid plaintext, and checking that nothing arrives.

import asyncio
import datetime
import io
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from teton_common.crypto import ConfiguratorHandshake, Role, SessionCipher
from teton_common.framing import Reassembler
from teton_common.memory_transport import MemoryClientTransport, MemoryDeviceTransport
from teton_common.messages import (
    HelloReply,
    Message,
    SetCredentials,
    decode_info,
    decode_session,
    encode,
)
from teton_common.protocol import Channel, DeviceState, InternetStatus, Security, StatusWarning
from teton_configurator.client import ClientChannel, DeviceMessage
from teton_configurator.flow import FoundDevice
from teton_device.buttons import Button, QueueButtons, StatusLine
from teton_device.service import DeviceService
from teton_device.storage import Storage
from teton_device.wifi.backend import ConnectResult, Credentials
from teton_device.wifi.fake import (
    DEFAULT_NETWORKS,
    HOME_PSK_PASSWORD,
    FakeNetwork,
    FakeWifiBackend,
)

DEVICE_ID = "TETON-3F9A2C"
CODE = "7K2MQX9D4HTRB8WZ"
NEW_CODE = "ZYXWVTSRQPNMKJHG"  # the code after the first [R][R]
WRONG_CODE = "0123456789ABCDEF"
HOME = SetCredentials(ssid="Home-PSK", security=Security.WPA2_PSK, psk=HOME_PSK_PASSWORD)
WRONG_PSK = SetCredentials(ssid="Home-PSK", security=Security.WPA2_PSK, psk="not-the-password")
RECEIVE_WAIT_S = 1  # real seconds; the memory transport delivers in a few loop turns
# The configurator's own waits are real seconds (they don't use the device's fake clock),
# so client and flow tests set them to fractions of a second (review Q54).
WAIT_S = 0.5  # an answer that should come arrives within a few loop turns
NO_REPLY_S = 0.05  # for the cases where nothing comes
TETON = FoundDevice(name="Teton-9A2C", signal=80, address="memory")


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

    async def led(self) -> str:
        """What the status "LED" shows now, e.g. "LED green". The controller redraws it on
        its 1 s tick, so let one simulated second pass first (review Q50)."""
        await self.time.advance(1)
        return [line for line in self.screen.getvalue().splitlines() if line.startswith("LED ")][-1]

    async def stop(self) -> None:
        if self.running():
            self.buttons.press(Button.POWER)
            assert self.task is not None
            await asyncio.wait_for(self.task, 1)


async def start_paired(state_dir: Path, wifi: FakeWifiBackend) -> Device:
    """A running device whose pairing button was pressed."""
    d = Device(state_dir, wifi=wifi)
    await d.start()
    await d.press(Button.PAIR)
    return d


async def provisioned_device(tmp_path: Path) -> Device:
    stored = Storage(tmp_path, new_code=lambda: CODE, new_id=lambda: DEVICE_ID)
    stored.load_or_create()
    stored.mark_provisioned()
    d = Device(tmp_path)
    await d.start()
    return d


class Wire(MemoryClientTransport):
    """The client end of the memory transport, recording every chunk the client sends."""

    def __init__(self, device: MemoryDeviceTransport) -> None:
        super().__init__(device)
        self.sent: list[tuple[Channel, bytes]] = []

    async def send(self, channel: Channel, data: bytes) -> None:
        self.sent.append((channel, data))
        await super().send(channel, data)

    def messages(self, channel: Channel) -> list[bytes]:
        """The whole messages sent on `channel`, reassembled from the chunks."""
        reassembler, out = Reassembler(), []
        for ch, chunk in self.sent:
            if ch == channel and (msg := reassembler.feed(chunk)) is not None:
                out.append(msg)
        return out

    def session_types(self) -> list[str]:
        return [decode_session(m).TYPE for m in self.messages(Channel.SESSION)]


# ---------------------------------------------------------------------------
# The technician and the Wi-Fi, for the flow and end-to-end tests

Answer = str | Callable[[], Awaitable[str]]


class ScriptedIO:
    """Plays the technician. An answer can be a coroutine function, to act on the device
    (e.g. let time pass) at exactly that prompt. Running out of answers is end of input."""

    def __init__(self, *answers: Answer) -> None:
        self._answers = list(answers)
        self.lines: list[str] = []
        self.secret_prompts: list[str] = []

    async def ask(self, prompt: str) -> str:
        return await self._next(prompt)

    async def ask_secret(self, prompt: str) -> str:
        self.secret_prompts.append(prompt)
        return await self._next(prompt)

    def show(self, text: str) -> None:
        self.lines.append(text)

    async def _next(self, prompt: str) -> str:
        self.lines.append(prompt)
        if not self._answers:
            raise EOFError
        answer = self._answers.pop(0)
        return answer if isinstance(answer, str) else await answer()

    @property
    def output(self) -> str:
        return "\n".join(self.lines)

    def assert_all_used(self) -> None:
        assert self._answers == [], f"unused answers: {self._answers}"


class RecordingWifi(FakeWifiBackend):
    """The fake Wi-Fi, keeping what the device was asked to join."""

    def __init__(self, networks: Iterable[FakeNetwork] = DEFAULT_NETWORKS) -> None:
        super().__init__(networks, sleep=instant)
        self.requests: list[Credentials] = []

    async def connect(self, creds: Credentials, timeout_s: float) -> ConnectResult:
        self.requests.append(creds)
        return await super().connect(creds, timeout_s)


class LimitedWifi(RecordingWifi):
    """Joins, but the network has no internet: the gateway answers, the check doesn't."""

    async def connect(self, creds: Credentials, timeout_s: float) -> ConnectResult:
        return ConnectResult.success(
            "192.168.1.23", InternetStatus.LIMITED, (StatusWarning.LIMITED_INTERNET,)
        )


def ca_pem() -> bytes:
    """A freshly made, self-signed CA certificate in PEM form."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Teton Test CA")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)
