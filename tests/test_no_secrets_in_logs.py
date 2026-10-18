# The enforcement of "no secrets in logs" (CLAUDE.md, SPEC §5.5): one simulated run through
# the real logging setup, then a search of everything it wrote for every secret it handled.
#
# The logs are rendered by configure_logging() itself, not captured with structlog's
# capture_logs, which replaces the processors and so would skip the redaction. Keys and
# ciphertexts are collected by spies on crypto, never by logging them (review Q58).
# Step 4.1b; decided in review Q56-Q62.

import base64
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fake_configurator import (
    CODE,
    DEVICE_ID,
    TETON,
    WAIT_S,
    WRONG_CODE,
    RecordingWifi,
    ScriptedIO,
    ca_pem,
    settle,
    start_paired,
)
from fake_nmcli import FakeRunner

from teton_common import crypto
from teton_common.log import configure_logging
from teton_common.memory_transport import MemoryClientTransport
from teton_common.messages import EapCredentials, Envelope
from teton_common.protocol import Security
from teton_common.setup_code import format_code
from teton_common.transport import ClientTransport
from teton_configurator import ui_text
from teton_configurator.client import ProvisioningClient
from teton_configurator.flow import FoundDevice, run_flow
from teton_device.wifi.backend import Credentials
from teton_device.wifi.fake import HOSPITAL_USER, FakeNetwork
from teton_device.wifi.nmcli import NmcliBackend, RunResult, run

# Unique values, so a leak can't hide behind a common word.
HOME_PSK = "home-NEEDLE-psk-7730"  # the network's real key
WRONG_PSK = "wrong-NEEDLE-psk-5521"  # what the technician types first
EAP_PASSWORD = "eap-NEEDLE-pass-8830"
NETWORKS = (
    FakeNetwork("Home-PSK", 82, Security.WPA2_PSK, psk=HOME_PSK),
    FakeNetwork(
        "Hospital-EAP",
        64,
        Security.WPA2_EAP_PEAP,
        identity=HOSPITAL_USER,
        password=EAP_PASSWORD,
        ip="10.20.0.57",
    ),
)
HOME, HOSPITAL, PICK_ANOTHER_NETWORK = "1", "2", "2"


@dataclass
class CryptoSpy:
    """Every private key, derived key and ciphertext made during the run (review Q58)."""

    keys: list[bytes] = field(default_factory=list)
    ciphertexts: list[bytes] = field(default_factory=list)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> CryptoSpy:
    # The handshakes and the cipher look these up in the module at call time, so wrapping
    # them here sees every handshake on both sides.
    found = CryptoSpy()
    new_key, derive, seal = crypto._new_key, crypto._derive, crypto.SessionCipher.seal

    def spy_new_key(randbytes: crypto.RandBytes) -> Any:
        key = new_key(randbytes)
        found.keys.append(key.private_bytes_raw())  # X25519 private key
        return key

    def spy_derive(*args: Any) -> tuple[crypto.SessionKeys, bytes]:
        keys, k_confirm = derive(*args)
        found.keys += [keys.c2d, keys.d2c, k_confirm]
        return keys, k_confirm

    def spy_seal(self: crypto.SessionCipher, plaintext: bytes) -> Envelope:
        envelope = seal(self, plaintext)
        found.ciphertexts.append(envelope.ct)
        return envelope

    monkeypatch.setattr(crypto, "_new_key", spy_new_key)
    monkeypatch.setattr(crypto, "_derive", spy_derive)
    monkeypatch.setattr(crypto.SessionCipher, "seal", spy_seal)
    return found


class EchoingRunner(FakeRunner):
    """Answers like FakeRunner, but each command also goes through the real run() with a
    stand-in that echoes its stdin to stdout and stderr, as nmcli's editor echoes
    `set wifi-sec.psk <secret>`. So run()'s own logging sees the real nmcli argv and the
    secret scripts (review Q61)."""

    async def __call__(
        self, argv: Sequence[str], timeout_s: float, stdin: str | None = None
    ) -> RunResult:
        await run(["sh", "-c", "tee /dev/stderr", *argv], timeout_s, stdin)
        return await super().__call__(argv, timeout_s, stdin)


async def provision(state_dir: Path, ca_file: Path) -> tuple[ScriptedIO, str]:
    """One technician run: a wrong code, the code typed lowercase with dashes, a wrong
    password on Home-PSK, then Hospital-EAP with a CA file, which connects. Returns what
    the technician saw and what the device's screen showed."""
    device = await start_paired(state_dir, RecordingWifi(NETWORKS))
    typed_code = format_code(CODE).lower()
    io_ = ScriptedIO(
        "", WRONG_CODE, typed_code, HOME, WRONG_PSK, PICK_ANOTHER_NETWORK,
        HOSPITAL, HOSPITAL_USER, EAP_PASSWORD, str(ca_file), "",
    )  # fmt: skip

    async def discover() -> list[FoundDevice]:
        return [TETON]

    def connect(_: FoundDevice) -> MemoryClientTransport:
        return MemoryClientTransport(device.transport)

    def make_client(transport: ClientTransport) -> ProvisioningClient:
        return ProvisioningClient(
            transport, reply_wait_s=WAIT_S, scan_wait_s=WAIT_S, result_wait_s=WAIT_S
        )

    try:
        assert await run_flow(discover, connect, io_, make_client=make_client) == 0
        await settle()
        io_.assert_all_used()
    finally:
        await device.stop()
    return io_, device.screen.getvalue()


async def nmcli_attempts(state_dir: Path, ca_text: str) -> None:
    """The same secrets through the real NetworkManager backend and runner (review Q61)."""
    backend = NmcliBackend(runner=EchoingRunner(), state_dir=state_dir)
    psk = Credentials(ssid="Home-PSK", security=Security.WPA2_PSK, psk=HOME_PSK)
    peap = Credentials(
        ssid="Hospital-EAP",
        security=Security.WPA2_EAP_PEAP,
        eap=EapCredentials(identity=HOSPITAL_USER, password=EAP_PASSWORD, ca_cert_pem=ca_text),
    )
    assert (await backend.connect(psk, timeout_s=45)).ok
    await backend.connect(peap, timeout_s=45)


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


async def test_no_secret_reaches_the_logs_the_screens_or_stdout(
    log_stream: io.StringIO,
    spy: CryptoSpy,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    configure_logging(verbose=True, stream=log_stream)  # DEBUG: the most that is ever logged
    ca_file = tmp_path / "ca.pem"
    ca_file.write_bytes(ca_pem())
    ca_text = ca_file.read_text()

    technician, device_screen = await provision(tmp_path / "device", ca_file)
    await nmcli_attempts(tmp_path / "nmcli", ca_text)

    # The spies saw the run, so a renamed crypto function can't make this pass silently.
    assert len(spy.keys) >= 4 * 4 and spy.ciphertexts  # two handshakes, both sides

    code_forms = {CODE, CODE.lower(), format_code(CODE), format_code(CODE).lower()}
    secrets = {"psk": HOME_PSK, "wrong psk": WRONG_PSK, "eap password": EAP_PASSWORD}
    for i, key in enumerate(spy.keys):
        secrets |= {f"key {i} hex": key.hex(), f"key {i} base64": b64(key)}
    for i, ct in enumerate(spy.ciphertexts):
        secrets[f"ciphertext {i}"] = b64(ct)
    # Line by line: inside JSON the PEM's newlines become "\n" (review Q59).
    for i, line in enumerate(ca_text.splitlines()):
        if line and not line.startswith("-----"):
            secrets[f"CA line {i}"] = line
    for form in code_forms:
        secrets[f"setup code {form!r}"] = form

    logs = log_stream.getvalue()
    out = capsys.readouterr()
    places = {
        "logs": (logs, secrets),
        "stdout": (out.out, secrets),
        "stderr": (out.err, secrets),
        "configurator": (technician.output, secrets),
        # SPEC §9.1: the device's own screen shows its code (demo note, [S]); nothing else.
        "device screen": (device_screen, {k: v for k, v in secrets.items() if v not in code_forms}),
    }
    leaks = [
        f"{name} in {place}"
        for place, (text, forbidden) in places.items()
        for name, value in forbidden.items()
        if value in text
    ]
    assert leaks == []
    assert technician.secret_prompts == [ui_text.ASK_PASSWORD] * 2  # never echoed

    # And the opposite: redaction must not hide what is needed to debug (review Q62).
    lines = [json.loads(line) for line in logs.splitlines()]

    def logged(**fields: object) -> bool:
        return any(all(line.get(k) == v for k, v in fields.items()) for line in lines)

    assert logged(event="connect_failed", reason="wrong_password")
    assert logged(event="connecting", ssid="Home-PSK")
    assert logged(event="connecting", ssid="Hospital-EAP")
    assert logged(event="connected", device_id=DEVICE_ID)
    assert logged(event="fake_wifi_result", level="debug")  # DEBUG lines really are captured
    nmcli_argvs = [line["argv"] for line in lines if line["event"] == "command_done"]
    assert any(HOSPITAL_USER in argv for argv in nmcli_argvs)  # identity stays (Q18)
    assert any("Home-PSK" in " ".join(argv) for argv in nmcli_argvs)
