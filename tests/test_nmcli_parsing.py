# Tests for the NetworkManager backend: parsing recorded nmcli outputs, mapping errors to
# failure reasons, the argument lists per security type, the success rule, and, with a
# fake runner, the command sequence, rollback and that no secret ever appears in argv.

import asyncio
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from teton_common.messages import EapCredentials
from teton_common.protocol import FailureReason, InternetStatus, Security, StatusWarning
from teton_device.wifi.backend import (
    ConnectResult,
    Credentials,
    Network,
    NmConnectivity,
    VerifyResult,
)
from teton_device.wifi.connectivity import decide_result
from teton_device.wifi.nmcli import (
    AUTOCONNECT_PRIORITY,
    NmcliBackend,
    NmcliConnectivity,
    RunResult,
    build_add_args,
    build_secret_script,
    map_failure,
    map_security,
    parse_connections,
    parse_device_show,
    parse_devices,
    parse_scan,
    run,
    split_terse,
)

FIXTURES = Path(__file__).parent / "fixtures" / "nmcli"
PSK = "correct-horse-battery"
EAP_PASSWORD = "teton-demo"
CA_PEM = "-----BEGIN CERTIFICATE-----\nMIIBfakeCA\n-----END CERTIFICATE-----\n"
IFACE = "wlp0s20f3"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def psk_creds(security: Security = Security.WPA2_PSK, **kw: object) -> Credentials:
    return Credentials(ssid="Home-PSK", security=security, psk=PSK, **kw)  # type: ignore[arg-type]


def peap_creds(**eap: str) -> Credentials:
    return Credentials(
        ssid="Hospital-EAP",
        security=Security.WPA2_EAP_PEAP,
        eap=EapCredentials(identity="nurse01", password=EAP_PASSWORD, **eap),
    )


def ok(stdout: str = "") -> RunResult:
    return RunResult(0, stdout, "")


def fail(stderr: str, returncode: int = 4) -> RunResult:
    return RunResult(returncode, "", stderr)


# ---------------------------------------------------------------------------
# Parsing


def test_split_terse_handles_escapes() -> None:
    assert split_terse(r"Lab\:5G:77:WPA2") == ["Lab:5G", "77", "WPA2"]
    assert split_terse(r"Back\\slash:30") == ["Back\\slash", "30"]
    assert split_terse(":65:WPA2") == ["", "65", "WPA2"]
    assert split_terse("") == [""]


@pytest.mark.parametrize(
    ("value", "security"),
    [
        ("WPA2", Security.WPA2_PSK),
        ("WPA1 WPA2", Security.WPA2_PSK),
        ("WPA2 WPA3", Security.WPA2_PSK),  # transition mode (Q11)
        ("WPA3", Security.WPA3_SAE),
        ("WPA2 802.1X", Security.WPA2_EAP_PEAP),
        ("WPA1 WPA2 802.1X", Security.WPA2_EAP_PEAP),
        ("WPA3 802.1X", None),  # WPA3-Enterprise only: not supported
        ("WPA1", None),
        ("WEP", None),
        ("OWE", None),
        ("", None),  # open: out of scope (Q11)
        ("--", None),
    ],
)
def test_map_security(value: str, security: Security | None) -> None:
    assert map_security(value) == security


def test_parse_scan_fixture() -> None:
    assert parse_scan(fixture("scan.txt")) == [
        Network("Home-PSK", 82, Security.WPA2_PSK),
        Network("Home-PSK", 41, Security.WPA2_PSK),  # a second AP: dedupe comes in scan()
        Network("Lab:5G", 77, Security.WPA2_PSK),
        Network("Back\\slash", 30, Security.WPA2_PSK),
        Network("Hospital-EAP", 64, Security.WPA2_EAP_PEAP),
        Network("Modern-SAE", 55, Security.WPA3_SAE),
    ]  # no hidden SSID, and no open, WEP, OWE, WPA3-Enterprise or WPA1 network (Q11)


def test_parse_scan_skips_broken_lines() -> None:
    assert parse_scan("garbage\nName:notanumber:WPA2\n" + "X" * 40 + ":50:WPA2\n") == []


def test_parse_devices_picks_the_managed_wifi() -> None:
    assert parse_devices(fixture("devices.txt")) == IFACE
    assert parse_devices("lo:loopback:unmanaged\nenp1s0:ethernet:connected\n") is None


def test_parse_connections_fixture() -> None:
    names = [name for name, _ in parse_connections(fixture("connections.txt"))]
    assert names == ["My-Home-Wifi", "teton-Home-PSK", "Wired connection 1", "teton-Lab:5G"]


@pytest.mark.parametrize(
    ("name", "ip", "gateway", "connectivity"),
    [
        ("device-show-full.txt", "192.168.1.23", "192.168.1.1", NmConnectivity.FULL),
        ("device-show-limited.txt", "10.20.0.57", "10.20.0.1", NmConnectivity.LIMITED),
        ("device-show-portal.txt", "172.16.4.9", "172.16.4.1", NmConnectivity.PORTAL),
        ("device-show-no-ip.txt", None, None, NmConnectivity.NONE),
    ],
)
def test_parse_device_show(
    name: str, ip: str | None, gateway: str | None, connectivity: NmConnectivity
) -> None:
    info = parse_device_show(fixture(name))
    assert (info.ip, info.gateway, info.connectivity) == (ip, gateway, connectivity)


def test_parse_device_show_unexpected_values_are_safe() -> None:
    info = parse_device_show("GENERAL.IP4-CONNECTIVITY:7 (weird)\nIP4.ADDRESS[1]:not-an-ip\n")
    assert (info.ip, info.gateway, info.connectivity) == (None, None, NmConnectivity.UNKNOWN)


# ---------------------------------------------------------------------------
# Failure reasons (SPEC §7)


@pytest.mark.parametrize(
    ("name", "creds", "reason"),
    [
        ("up-wrong-password.txt", psk_creds(), FailureReason.WRONG_PASSWORD),
        ("up-wrong-password.txt", peap_creds(), FailureReason.AUTH_FAILED),
        ("up-eap-failed.txt", peap_creds(), FailureReason.AUTH_FAILED),
        ("up-ssid-not-found.txt", psk_creds(), FailureReason.SSID_NOT_FOUND),
        ("wifi-connect-no-ssid.txt", psk_creds(), FailureReason.SSID_NOT_FOUND),
        ("up-no-ip.txt", psk_creds(), FailureReason.NO_IP),
        ("up-unknown.txt", psk_creds(), FailureReason.TIMEOUT),  # unknown -> timeout
    ],
)
def test_map_failure(name: str, creds: Credentials, reason: FailureReason) -> None:
    assert map_failure(4, fixture(name), creds) == reason


def test_nmcli_timeout_exit_code_is_timeout() -> None:
    assert map_failure(3, fixture("up-timeout.txt"), psk_creds()) == FailureReason.TIMEOUT


# ---------------------------------------------------------------------------
# Building commands


def test_add_args_for_psk() -> None:
    args = build_add_args(psk_creds(), "teton-Home-PSK", IFACE, None)
    assert args == [
        "nmcli", "connection", "add", "type", "wifi", "con-name", "teton-Home-PSK",
        "ifname", IFACE, "ssid", "Home-PSK",
        "connection.autoconnect", "no",
        "connection.autoconnect-priority", str(AUTOCONNECT_PRIORITY),
        "wifi-sec.key-mgmt", "wpa-psk",
    ]  # fmt: skip


def test_add_args_for_sae_and_hidden() -> None:
    args = build_add_args(psk_creds(Security.WPA3_SAE, hidden=True), "teton-Home-PSK", IFACE, None)
    assert args[-4:] == ["wifi.hidden", "yes", "wifi-sec.key-mgmt", "sae"]


def test_add_args_for_peap_with_everything() -> None:
    creds = peap_creds(
        anonymous_identity="anonymous", ca_cert_pem=CA_PEM, domain_suffix_match="h.example"
    )
    args = build_add_args(creds, "teton-Hospital-EAP", IFACE, Path("/state/certs/x.pem"))
    assert args[args.index("wifi-sec.key-mgmt") :] == [
        "wifi-sec.key-mgmt", "wpa-eap", "802-1x.eap", "peap",
        "802-1x.phase2-auth", "mschapv2", "802-1x.identity", "nurse01",
        "802-1x.anonymous-identity", "anonymous",
        "802-1x.ca-cert", "/state/certs/x.pem",
        "802-1x.domain-suffix-match", "h.example",
    ]  # fmt: skip


def test_add_args_for_minimal_peap_has_no_ca_or_domain() -> None:
    args = build_add_args(peap_creds(), "teton-Hospital-EAP", IFACE, None)
    assert "802-1x.ca-cert" not in args and "802-1x.domain-suffix-match" not in args


def test_add_args_reject_open() -> None:
    with pytest.raises(ValueError):
        build_add_args(Credentials(ssid="Cafe", security=Security.OPEN), "teton-Cafe", IFACE, None)


def test_secret_scripts() -> None:
    assert build_secret_script(psk_creds()) == f"set wifi-sec.psk {PSK}\nsave\nquit\n"
    assert build_secret_script(peap_creds()) == f"set 802-1x.password {EAP_PASSWORD}\nsave\nquit\n"


@pytest.mark.parametrize("secret", ["line\nbreak", "carriage\rreturn"])
def test_secret_with_a_line_break_is_refused(secret: str) -> None:
    creds = Credentials(ssid="Home-PSK", security=Security.WPA2_PSK, psk=secret + "-padding")
    with pytest.raises(ValueError) as exc:
        build_secret_script(creds)
    assert secret not in str(exc.value)


# ---------------------------------------------------------------------------
# The success rule (review Q8)


@pytest.mark.parametrize(
    ("verify", "expected"),
    [
        (
            VerifyResult("192.168.1.23", NmConnectivity.FULL, None),
            ConnectResult.success("192.168.1.23", InternetStatus.FULL),
        ),
        (
            VerifyResult("192.168.1.23", NmConnectivity.PORTAL, None),
            ConnectResult.failure(FailureReason.CAPTIVE_PORTAL),
        ),
        (
            VerifyResult("10.0.0.5", NmConnectivity.LIMITED, True),
            ConnectResult.success(
                "10.0.0.5", InternetStatus.LIMITED, (StatusWarning.LIMITED_INTERNET,)
            ),
        ),
        (
            VerifyResult("10.0.0.5", NmConnectivity.LIMITED, False),
            ConnectResult.failure(FailureReason.NO_INTERNET),
        ),
        (
            VerifyResult("10.0.0.5", NmConnectivity.NONE, True),
            ConnectResult.success(
                "10.0.0.5", InternetStatus.NONE, (StatusWarning.LIMITED_INTERNET,)
            ),
        ),
        (
            VerifyResult("10.0.0.5", NmConnectivity.UNKNOWN, True),
            ConnectResult.success(
                "10.0.0.5", InternetStatus.UNKNOWN, (StatusWarning.LIMITED_INTERNET,)
            ),
        ),
        (
            VerifyResult(None, NmConnectivity.FULL, None),
            ConnectResult.failure(FailureReason.NO_IP),
        ),
    ],
)
def test_success_rule(verify: VerifyResult, expected: ConnectResult) -> None:
    assert decide_result(verify) == expected


def test_success_rule_keeps_the_enterprise_warning() -> None:
    verify = VerifyResult("10.0.0.5", NmConnectivity.LIMITED, True)
    result = decide_result(verify, (StatusWarning.SERVER_NOT_VERIFIED,))
    assert result.warnings == (StatusWarning.SERVER_NOT_VERIFIED, StatusWarning.LIMITED_INTERNET)


# ---------------------------------------------------------------------------
# The backend with a fake runner


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]
    stdin: str | None
    timeout_s: float


class FakeRunner:
    """Records every command and answers from rules: the first rule whose words all
    appear in argv wins; otherwise the command succeeds with no output."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.rules: list[tuple[tuple[str, ...], RunResult]] = []
        self.on("DEVICE,TYPE,STATE", result=ok(fixture("devices.txt")))
        self.on("NAME,UUID", result=ok(fixture("connections.txt")))
        self.on("device", "show", result=ok(fixture("device-show-full.txt")))

    def on(self, *words: str, result: RunResult) -> None:
        self.rules.insert(0, (words, result))  # newest first, so tests can override

    async def __call__(
        self, argv: Sequence[str], timeout_s: float, stdin: str | None = None
    ) -> RunResult:
        self.calls.append(Call(tuple(argv), stdin, timeout_s))
        for words, result in self.rules:
            if all(w in argv for w in words):
                return result
        return ok()

    def commands(self) -> list[str]:
        return [" ".join(c.argv) for c in self.calls]

    def deleted(self) -> list[str]:
        return [c.argv[-1] for c in self.calls if c.argv[1:3] == ("connection", "delete")]


HOME_UUID = "9d8c7b6a-5f4e-4d3c-2b1a-0f9e8d7c6b5a"  # teton-Home-PSK in connections.txt


@pytest.fixture
def runner() -> FakeRunner:
    return FakeRunner()


@pytest.fixture
def backend(runner: FakeRunner, tmp_path: Path) -> NmcliBackend:
    return NmcliBackend(runner=runner, state_dir=tmp_path)


async def test_connect_success_runs_the_spec_sequence(
    backend: NmcliBackend, runner: FakeRunner
) -> None:
    result = await backend.connect(psk_creds(), timeout_s=45)
    assert result == ConnectResult.success("192.168.1.23", InternetStatus.FULL)
    cmds = runner.commands()
    assert cmds[0] == "nmcli -t -f DEVICE,TYPE,STATE device"
    assert cmds[1] == "nmcli -t -f NAME,UUID connection show"
    assert cmds[2] == f"nmcli connection delete uuid {HOME_UUID}"  # leftover from a crash
    assert cmds[3].startswith("nmcli connection add type wifi con-name teton-Home-PSK")
    assert cmds[4] == "nmcli connection edit id teton-Home-PSK"
    assert cmds[5] == "nmcli --wait 45 connection up id teton-Home-PSK"
    assert cmds[6] == "nmcli networking connectivity check"
    assert cmds[7].startswith("nmcli -t -f GENERAL.IP4-CONNECTIVITY")
    assert cmds[8] == "nmcli connection modify id teton-Home-PSK connection.autoconnect yes"
    assert len(cmds) == 9  # full internet: no ping needed
    assert runner.calls[5].timeout_s > 45  # nmcli's own --wait gives up first


async def test_every_call_has_a_timeout(backend: NmcliBackend, runner: FakeRunner) -> None:
    await backend.connect(peap_creds(ca_cert_pem=CA_PEM), timeout_s=45)
    await backend.scan()
    await backend.forget_all()
    assert all(0 < c.timeout_s <= 60 for c in runner.calls)


@pytest.mark.parametrize(
    ("creds", "secret"),
    [
        (psk_creds(), PSK),
        (psk_creds(Security.WPA3_SAE), PSK),
        (peap_creds(ca_cert_pem=CA_PEM, domain_suffix_match="h.example"), EAP_PASSWORD),
    ],
)
async def test_no_secret_in_argv(
    backend: NmcliBackend, runner: FakeRunner, creds: Credentials, secret: str
) -> None:
    # Also on the failure path, which runs the rollback commands too.
    await backend.connect(creds, timeout_s=45)
    runner.on("up", result=fail(fixture("up-wrong-password.txt")))
    await backend.connect(creds, timeout_s=45)

    for call in runner.calls:
        assert not any(secret in arg for arg in call.argv), call.argv
        assert not any("BEGIN CERTIFICATE" in arg for arg in call.argv)
    stdins = [c.stdin for c in runner.calls if c.stdin is not None]
    assert stdins and all(secret in s for s in stdins)  # the secret only travels on stdin


async def test_failed_activation_deletes_the_profile(
    backend: NmcliBackend, runner: FakeRunner
) -> None:
    runner.on("up", result=fail(fixture("up-wrong-password.txt")))
    result = await backend.connect(psk_creds(), timeout_s=45)
    assert result == ConnectResult.failure(FailureReason.WRONG_PASSWORD)
    assert runner.deleted() == [HOME_UUID, HOME_UUID]  # before (leftover) and after (rollback)
    assert not any("modify" in c for c in runner.commands())  # never set to autoconnect


async def test_activation_timeout_is_timeout(backend: NmcliBackend, runner: FakeRunner) -> None:
    runner.on("up", result=RunResult(-1, "", "", timed_out=True))
    assert (await backend.connect(psk_creds(), timeout_s=45)).reason == FailureReason.TIMEOUT
    assert runner.deleted()[-1] == HOME_UUID


async def test_editor_error_counts_as_failure(backend: NmcliBackend, runner: FakeRunner) -> None:
    runner.on("edit", result=ok("Error: failed to set 'psk' property: invalid"))
    result = await backend.connect(psk_creds(), timeout_s=45)
    assert not result.ok
    assert not any(" up " in c for c in runner.commands())  # stopped before activating


async def test_captive_portal_rolls_back(backend: NmcliBackend, runner: FakeRunner) -> None:
    runner.on("device", "show", result=ok(fixture("device-show-portal.txt")))
    result = await backend.connect(psk_creds(), timeout_s=45)
    assert result == ConnectResult.failure(FailureReason.CAPTIVE_PORTAL)
    assert runner.deleted()[-1] == HOME_UUID


async def test_limited_internet_pings_the_gateway_on_the_interface(
    backend: NmcliBackend, runner: FakeRunner
) -> None:
    runner.on("device", "show", result=ok(fixture("device-show-limited.txt")))
    result = await backend.connect(psk_creds(), timeout_s=45)
    assert result == ConnectResult.success(
        "10.20.0.57", InternetStatus.LIMITED, (StatusWarning.LIMITED_INTERNET,)
    )
    assert "ping -I wlp0s20f3 -c 2 -W 2 10.20.0.1" in runner.commands()


async def test_limited_internet_with_a_silent_gateway_is_no_internet(
    backend: NmcliBackend, runner: FakeRunner
) -> None:
    runner.on("device", "show", result=ok(fixture("device-show-limited.txt")))
    runner.on("ping", result=fail("", returncode=1))
    assert (await backend.connect(psk_creds(), timeout_s=45)).reason == FailureReason.NO_INTERNET


async def test_slow_verification_is_a_timeout(runner: FakeRunner, tmp_path: Path) -> None:
    class Stuck:
        async def verify(self, interface: str) -> VerifyResult:
            await asyncio.Event().wait()  # never finishes
            raise AssertionError

    backend = NmcliBackend(runner, tmp_path, connectivity=Stuck(), verify_timeout_s=0.01)
    assert (await backend.connect(psk_creds(), timeout_s=45)).reason == FailureReason.TIMEOUT


async def test_ca_certificate_goes_to_a_private_file(
    backend: NmcliBackend, runner: FakeRunner, tmp_path: Path
) -> None:
    await backend.connect(peap_creds(ca_cert_pem=CA_PEM), timeout_s=45)
    add = next(c.argv for c in runner.calls if "add" in c.argv)
    path = Path(add[add.index("802-1x.ca-cert") + 1])
    assert path.parent == tmp_path / "certs"
    assert path.read_text() == CA_PEM
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


async def test_ca_certificate_is_removed_on_failure(
    backend: NmcliBackend, runner: FakeRunner, tmp_path: Path
) -> None:
    runner.on("up", result=fail(fixture("up-eap-failed.txt")))
    result = await backend.connect(peap_creds(ca_cert_pem=CA_PEM), timeout_s=45)
    assert result.reason == FailureReason.AUTH_FAILED
    assert list((tmp_path / "certs").iterdir()) == []


async def test_peap_without_ca_connects_with_the_warning(backend: NmcliBackend) -> None:
    result = await backend.connect(peap_creds(), timeout_s=45)
    assert result.ok and result.warnings == (StatusWarning.SERVER_NOT_VERIFIED,)


async def test_line_break_in_password_is_refused_before_any_change(
    backend: NmcliBackend, runner: FakeRunner
) -> None:
    creds = Credentials(ssid="Home-PSK", security=Security.WPA2_PSK, psk="bad\npassword")
    assert (await backend.connect(creds, timeout_s=45)).reason == FailureReason.WRONG_PASSWORD
    assert not any("add" in c.argv for c in runner.calls)


async def test_no_wifi_interface(backend: NmcliBackend, runner: FakeRunner) -> None:
    runner.on("DEVICE,TYPE,STATE", result=ok("lo:loopback:unmanaged\n"))
    assert (await backend.connect(psk_creds(), timeout_s=45)).reason == FailureReason.TIMEOUT
    assert len(runner.calls) == 1


async def test_the_interface_is_detected_once(backend: NmcliBackend, runner: FakeRunner) -> None:
    await backend.connect(psk_creds(), timeout_s=45)
    await backend.connect(psk_creds(), timeout_s=45)
    assert sum("DEVICE,TYPE,STATE" in c.argv for c in runner.calls) == 1


async def test_scan_dedupes_and_sorts(backend: NmcliBackend, runner: FakeRunner) -> None:
    runner.on("wifi", "list", result=ok(fixture("scan.txt")))
    networks = await backend.scan()
    assert [n.ssid for n in networks] == [
        "Home-PSK",
        "Lab:5G",
        "Hospital-EAP",
        "Modern-SAE",
        "Back\\slash",
    ]
    assert runner.commands() == [
        "nmcli -t -f SSID,SIGNAL,SECURITY device wifi list --rescan yes"
    ]  # SPEC §8.1


async def test_scan_failure_gives_an_empty_list(backend: NmcliBackend, runner: FakeRunner) -> None:
    runner.on("wifi", "list", result=fail("Error: NetworkManager is not running."))
    assert await backend.scan() == []


async def test_forget_all_deletes_only_teton_profiles(
    backend: NmcliBackend, runner: FakeRunner, tmp_path: Path
) -> None:
    await backend.connect(peap_creds(ca_cert_pem=CA_PEM), timeout_s=45)  # leaves a CA file
    runner.calls.clear()
    assert await backend.forget_all() == ["teton-Home-PSK", "teton-Lab:5G"]
    assert runner.deleted() == [HOME_UUID, "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"]
    assert list((tmp_path / "certs").iterdir()) == []


async def test_connectivity_full_needs_no_ping(runner: FakeRunner) -> None:
    result = await NmcliConnectivity(runner).verify(IFACE)
    assert result == VerifyResult("192.168.1.23", NmConnectivity.FULL, None)
    assert not any(c.argv[0] == "ping" for c in runner.calls)


async def test_connectivity_without_address_or_gateway(runner: FakeRunner) -> None:
    runner.on("device", "show", result=ok(fixture("device-show-no-ip.txt")))
    assert (await NmcliConnectivity(runner).verify(IFACE)).ip is None
    runner.on(
        "device",
        "show",
        result=ok("GENERAL.IP4-CONNECTIVITY:3 (limited)\nIP4.ADDRESS[1]:10.0.0.5/24\n"),
    )
    result = await NmcliConnectivity(runner).verify(IFACE)
    assert result.gateway_reachable is False  # no gateway to ping
    assert not any(c.argv[0] == "ping" for c in runner.calls)


# ---------------------------------------------------------------------------
# The real runner (runs small standard commands, no nmcli)


async def test_runner_feeds_stdin_and_captures_output() -> None:
    result = await run(["cat"], timeout_s=5, stdin="set wifi-sec.psk x\n")
    assert result == RunResult(0, "set wifi-sec.psk x\n", "")


async def test_runner_forces_english_messages() -> None:
    result = await run(["sh", "-c", "echo $LC_ALL"], timeout_s=5)
    assert result.stdout.strip() == "C"


async def test_runner_kills_a_command_that_overruns() -> None:
    result = await run(["sleep", "5"], timeout_s=0.05)
    assert result.timed_out


async def test_runner_reports_a_missing_command() -> None:
    result = await run(["teton-no-such-command"], timeout_s=5)
    assert result.returncode == 127 and not result.timed_out
