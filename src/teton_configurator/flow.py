# Guided steps: pick device, enter setup code, pick network, enter credentials,
# show progress and result, offer retry. See SPEC §9.2.
#
# The flow knows neither the radio nor the terminal: it gets `discover`, `connect` and `io`
# injected (review Q38), so tests script every answer and run it against the real device
# service. Every text comes from ui_text; exception texts are never shown (review Q28).
# Exit code: 0 when the device is connected, 1 otherwise.

import asyncio
import getpass
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cryptography import x509

from teton_common import setup_code
from teton_common.messages import (
    MAX_SSID_BYTES,
    PSK_MAX_CHARS,
    PSK_MIN_CHARS,
    EapCredentials,
    Info,
    NetworkInfo,
    SetCredentials,
    Status,
)
from teton_common.protocol import MAX_CA_PEM_BYTES, ErrorCode, Security, StatusState
from teton_common.transport import ClientTransport
from teton_configurator import ui_text
from teton_configurator.client import (
    ConnectionLostError,
    DeviceRefusedError,
    NoReplyError,
    ProtocolError,
    ProvisioningClient,
    RateLimitedError,
    WrongCodeError,
)

# What a hidden network may use, in menu order (review Q11, Q42).
HIDDEN_SECURITY = (Security.WPA2_PSK, Security.WPA3_SAE, Security.WPA2_EAP_PEAP)


@dataclass(frozen=True)
class FoundDevice:
    """A device seen while discovering (review Q38)."""

    name: str  # Teton-9A2C
    signal: int  # 0-100; the BLE transport converts RSSI, so signal_bars() fits both
    address: str  # how the transport finds it again (BLE address, socket path)


Discover = Callable[[], Awaitable[list[FoundDevice]]]
Connect = Callable[[FoundDevice], ClientTransport]  # not connected yet: the client does it
MakeClient = Callable[[ClientTransport], ProvisioningClient]


class FlowIO(Protocol):
    async def ask(self, prompt: str) -> str: ...

    async def ask_secret(self, prompt: str) -> str: ...  # not echoed

    def show(self, text: str) -> None: ...


class TerminalIO:
    """input() and getpass() run in a thread, so the event loop (and with it the BLE
    connection) keeps running while the technician types."""

    async def ask(self, prompt: str) -> str:
        return await asyncio.to_thread(input, prompt)

    async def ask_secret(self, prompt: str) -> str:
        return await asyncio.to_thread(getpass.getpass, prompt)

    def show(self, text: str) -> None:
        print(text, flush=True)


class _Quit(Exception):
    """The technician typed q, or input ended (review Q46)."""


class _End(Exception):
    """The run can't go on (review Q35, Q44). Carries the ui_text sentence to show."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


@dataclass(frozen=True)
class _Target:
    """The network the technician chose."""

    ssid: str
    security: Security
    hidden: bool = False


async def run_flow(
    discover: Discover,
    connect: Connect,
    io: FlowIO,
    make_client: MakeClient = ProvisioningClient,
) -> int:
    try:
        found = await _pick_device(discover, io)
        client = make_client(connect(found))
        try:
            return await _Session(client, io).run()
        finally:
            await client.close()  # always, whatever happened (review Q46)
    except _Quit:
        io.show(ui_text.STOPPED)
    except _End as end:
        io.show(end.text)
    return 1


# ---------------------------------------------------------------------------
# Step 1: pick a device (review Q39)


async def _pick_device(discover: Discover, io: FlowIO) -> FoundDevice:
    io.show(ui_text.PRESS_PAIRING_BUTTON)
    while True:
        io.show(ui_text.LOOKING_FOR_DEVICES)
        devices = await discover()
        if devices:
            break
        io.show(ui_text.NO_DEVICE_FOUND)
        if (await _ask(io, ui_text.ASK_LOOK_AGAIN)).strip().lower() == "q":
            raise _Quit
    for number, device in enumerate(devices, 1):
        io.show(ui_text.menu_line(number, ui_text.device_label(device.name, device.signal)))
    return devices[await _choose(io, ui_text.ASK_DEVICE, len(devices))]


# ---------------------------------------------------------------------------
# Steps 2 to 6, on one connection


class _Session:
    def __init__(self, client: ProvisioningClient, io: FlowIO) -> None:
        self._client = client
        self._io = io
        self._networks: list[NetworkInfo] = []

    async def run(self) -> int:
        try:
            await self._client.connect()
            info = await self._enter_code()
            await self._scan()
            while True:
                target = await self._pick_network(info)
                if await self._provision(target):
                    return 0
        except NoReplyError:
            raise _End(ui_text.NO_REPLY) from None  # Q22: the pairing window is closed
        except ConnectionLostError:
            raise _End(ui_text.CONNECTION_LOST) from None
        except ProtocolError:
            raise _End(ui_text.UNEXPECTED_REPLY) from None

    # --- step 2: the setup code (review Q40) ---------------------------------------------

    async def _enter_code(self) -> Info:
        while True:
            typed = await _ask(self._io, ui_text.ASK_SETUP_CODE)
            try:
                code = setup_code.normalize(typed)
            except setup_code.InvalidSetupCode:
                self._io.show(ui_text.INVALID_CODE)  # nothing sent, so not counted (Q28)
                continue
            self._io.show(ui_text.CHECKING_CODE)
            try:
                info = await self._client.handshake(code)
            except WrongCodeError:
                self._io.show(ui_text.WRONG_CODE)
                continue
            except RateLimitedError as exc:
                # No countdown here: the device enforces the wait (Q40).
                self._io.show(ui_text.error_text(ErrorCode.RATE_LIMITED, exc.retry_after_s))
                continue
            self._io.show(ui_text.CODE_ACCEPTED)
            return info

    # --- step 3: the network (review Q41, Q42) -------------------------------------------

    async def _scan(self) -> None:
        self._io.show(ui_text.LOOKING_FOR_NETWORKS)
        self._networks = await self._client.scan()

    async def _pick_network(self, info: Info) -> _Target:
        while True:
            if not self._networks:
                self._io.show(ui_text.NO_NETWORKS)
            width = max((len(n.ssid) for n in self._networks), default=0)
            options = [ui_text.network_label(n, width) for n in self._networks]
            options += [ui_text.OTHER_NETWORK, ui_text.SCAN_AGAIN]
            choice = await self._menu(ui_text.ASK_NETWORK, options)
            if choice < len(self._networks):
                net = self._networks[choice]
                return _Target(net.ssid, net.security)
            if choice == len(self._networks):
                return await self._hidden_network(info)
            await self._scan()

    async def _hidden_network(self, info: Info) -> _Target:
        while True:
            ssid = await _ask(self._io, ui_text.ASK_NETWORK_NAME)
            if 1 <= len(ssid.encode("utf-8")) <= MAX_SSID_BYTES:
                break
            self._io.show(ui_text.INVALID_NETWORK_NAME)
        choices = [s for s in HIDDEN_SECURITY if s in info.caps]
        labels = [ui_text.hidden_security_label(s) for s in choices]
        security = choices[await self._menu(ui_text.ASK_SECURITY, labels)]
        return _Target(ssid, security, hidden=True)

    # --- steps 4 to 6: credentials, progress, result (review Q43, Q44, Q47) --------------

    async def _provision(self, target: _Target) -> bool:
        """True when connected; False to go back to the network menu."""
        while True:
            creds = await self._credentials(target)
            try:
                result = await self._attempt(creds)
            except DeviceRefusedError as exc:
                self._io.show(ui_text.error_text(exc.code))
                return False
            if result.state == StatusState.CONNECTED:
                assert result.ip is not None and result.internet is not None
                self._io.show(ui_text.connected_text(result.ip, result.internet))
                for warning in result.warnings:  # Q45: the device's word, after the fact
                    self._io.show(ui_text.warning_text(warning))
                return True
            assert result.reason is not None
            self._io.show(ui_text.failure_text(result.reason))
            retry = (
                ui_text.RETRY_LOGIN
                if target.security == Security.WPA2_EAP_PEAP
                else ui_text.RETRY_PASSWORD
            )
            options = [retry, ui_text.PICK_ANOTHER_NETWORK, ui_text.QUIT]
            choice = await self._menu(ui_text.ASK_NEXT, options)
            if choice == 1:
                return False
            if choice == 2:
                raise _Quit

    async def _attempt(self, creds: SetCredentials) -> Status:
        ssid = creds.ssid
        statuses = self._client.provision(creds)
        del creds
        try:
            async for status in statuses:
                if status.state == StatusState.CONNECTING:
                    self._io.show(ui_text.connecting_text(ssid))  # one line, no spinner (Q47)
                    continue
                return status
        except NoReplyError:
            # Not "took too long": the device always finishes and may have connected.
            raise _End(ui_text.RESULT_TIMEOUT) from None
        raise ProtocolError("the status stream ended without a result")

    async def _credentials(self, target: _Target) -> SetCredentials:
        if target.security != Security.WPA2_EAP_PEAP:
            while True:
                psk = await _ask_secret(self._io, ui_text.ASK_PASSWORD)
                if PSK_MIN_CHARS <= len(psk) <= PSK_MAX_CHARS:
                    break
                self._io.show(ui_text.INVALID_PASSWORD_LENGTH)
            return SetCredentials(
                ssid=target.ssid, security=target.security, hidden=target.hidden, psk=psk
            )
        identity = await self._required(ui_text.ASK_USERNAME, secret=False)
        password = await self._required(ui_text.ASK_PASSWORD, secret=True)
        ca_cert_pem = await self._ca_file()
        domain = (await _ask(self._io, ui_text.ASK_DOMAIN)).strip() or None
        eap = EapCredentials(
            identity=identity,
            password=password,
            ca_cert_pem=ca_cert_pem,
            domain_suffix_match=domain,
        )
        return SetCredentials(
            ssid=target.ssid, security=target.security, hidden=target.hidden, eap=eap
        )

    async def _required(self, prompt: str, secret: bool) -> str:
        while True:
            value = await (_ask_secret if secret else _ask)(self._io, prompt)
            if value:
                return value
            self._io.show(ui_text.REQUIRED)

    async def _ca_file(self) -> str | None:
        while True:
            path = (await _ask(self._io, ui_text.ASK_CA_FILE)).strip()
            if not path:
                return None
            try:
                return read_ca_file(Path(path).expanduser())
            except CaFileError as problem:
                self._io.show(problem.text)

    async def _menu(self, prompt: str, options: list[str]) -> int:
        for number, text in enumerate(options, 1):
            self._io.show(ui_text.menu_line(number, text))
        return await _choose(self._io, prompt, len(options))


# ---------------------------------------------------------------------------
# Small helpers


class CaFileError(Exception):
    """The CA file can't be used. Carries the ui_text sentence to show."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


def read_ca_file(path: Path) -> str:
    """The PEM text of a CA certificate file (review Q43): at most MAX_CA_PEM_BYTES, and it
    must parse as PEM certificates, so a wrong file is caught here rather than showing up
    as a failed Enterprise login on the device."""
    try:
        if path.stat().st_size > MAX_CA_PEM_BYTES:
            raise CaFileError(ui_text.CA_FILE_TOO_LARGE)  # checked before reading it
        data = path.read_bytes()
    except OSError:  # missing, a directory, no permission
        raise CaFileError(ui_text.CA_FILE_UNREADABLE) from None
    try:
        x509.load_pem_x509_certificates(data)
        return data.decode("ascii")
    except (ValueError, UnicodeDecodeError):
        raise CaFileError(ui_text.CA_FILE_NOT_CERTIFICATE) from None


async def _choose(io: FlowIO, prompt: str, count: int) -> int:
    """A 0-based index from a 1-based number. Enter picks the only option (Q39); q quits."""
    while True:
        answer = (await _ask(io, prompt)).strip().lower()
        if answer == "q":
            raise _Quit
        if answer == "" and count == 1:
            return 0
        if answer.isdigit() and 1 <= int(answer) <= count:
            return int(answer) - 1
        io.show(ui_text.INVALID_CHOICE)


async def _ask(io: FlowIO, prompt: str) -> str:
    try:
        return await io.ask(prompt)
    except EOFError:
        raise _Quit from None  # stdin closed: like typing q (Q46)


async def _ask_secret(io: FlowIO, prompt: str) -> str:
    try:
        return await io.ask_secret(prompt)
    except EOFError:
        raise _Quit from None
