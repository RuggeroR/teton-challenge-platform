# NetworkManager implementation via nmcli: scan, add profile (PSK/SAE/PEAP), activate,
# map errors to failure reasons, roll back on failure. See SPEC §7, §8.
#
# Two layers:
#   pure functions   parse nmcli output, build argument lists, map errors. Tested against
#                    the recorded outputs in tests/fixtures/nmcli/.
#   I/O classes      NmcliBackend (WifiBackend) and NmcliConnectivity (Connectivity). Every
#                    command, nmcli or ping, goes through one injectable runner with a timeout.
#
# Secrets never go on a command line, where any local user can read them (`ps`,
# /proc/<pid>/cmdline). The profile is created without them, and the secret is then set by
# piping editor commands into `nmcli connection edit` (SPEC §5.5, review Q9).

import asyncio
import ipaddress
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import structlog

from teton_common.messages import MessageError
from teton_common.protocol import VERIFY_TIMEOUT_S, FailureReason, Security
from teton_device.storage import DEFAULT_STATE_DIR
from teton_device.wifi.backend import (
    PROFILE_PREFIX,
    Connectivity,
    ConnectResult,
    Credentials,
    Network,
    NmConnectivity,
    VerifyResult,
    profile_name,
    rejected_reason,
    scan_list,
    security_warnings,
)
from teton_device.wifi.connectivity import decide_result

log = structlog.get_logger()

AUTOCONNECT_PRIORITY = 10  # above the default 0, so a known home network doesn't win (Q17)
SCAN_TIMEOUT_S = 30  # `--rescan yes` waits for a fresh scan
QUICK_TIMEOUT_S = 10  # every other nmcli call
PING_TIMEOUT_S = 6  # ping -c 2 -W 2 takes at most about 4 s
EXIT_TIMEOUT = 3  # nmcli's exit code for "Timeout expired" (--wait)


# ---------------------------------------------------------------------------
# The runner: the only place that starts a process.


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class Runner(Protocol):
    async def __call__(
        self, argv: Sequence[str], timeout_s: float, stdin: str | None = None
    ) -> RunResult: ...


async def run(argv: Sequence[str], timeout_s: float, stdin: str | None = None) -> RunResult:
    """Run one command with a time limit, and kill it if it overruns.

    LC_ALL=C makes nmcli print English messages, which map_failure() matches, whatever the
    desktop language is. stdin may carry a secret, so it is never logged; argv never does.
    """
    env = {**os.environ, "LC_ALL": "C"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:  # e.g. nmcli or ping not installed
        return RunResult(127, "", f"cannot run {argv[0]}: {exc.strerror}")

    out, err, timed_out = b"", b"", False
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None), timeout_s
        )
    except TimeoutError:
        timed_out = True
    finally:
        if proc.returncode is None:  # timed out or cancelled: never leave it running
            proc.kill()
            await proc.wait()
    result = RunResult(
        proc.returncode if proc.returncode is not None else -1,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
        timed_out,
    )
    # Raw output goes to the debug log only, never to the configurator (DESIGN §6).
    log.debug(
        "command_done",
        argv=list(argv),
        returncode=result.returncode,
        timed_out=timed_out,
        stderr=result.stderr.strip(),
    )
    return result


# ---------------------------------------------------------------------------
# Parsing nmcli output (pure).


def split_terse(line: str) -> list[str]:
    """Split one line of `nmcli -t` output on ':'. nmcli escapes ':' and '\\' in values
    with a backslash, so an SSID like `Lab:5G` arrives as `Lab\\:5G`."""
    fields: list[str] = []
    current: list[str] = []
    chars = iter(line)
    for ch in chars:
        if ch == "\\":
            current.append(next(chars, ""))
        elif ch == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
    fields.append("".join(current))
    return fields


def map_security(value: str) -> Security | None:
    """nmcli's SECURITY column -> what we can join, or None to leave it out (review Q11)."""
    tokens = set(value.split())
    if "802.1X" in tokens:
        # PEAP needs WPA2-Enterprise; WPA3-Enterprise-only and WPA1 are not supported.
        return Security.WPA2_EAP_PEAP if "WPA2" in tokens else None
    if "WPA2" in tokens:
        return Security.WPA2_PSK  # also "WPA2 WPA3" (transition mode, most phone hotspots)
    if "WPA3" in tokens:
        return Security.WPA3_SAE
    return None  # open, WEP, OWE, WPA1 only


def parse_scan(stdout: str) -> list[Network]:
    """`nmcli -t -f SSID,SIGNAL,SECURITY device wifi list`, one entry per access point.

    Hidden networks (empty SSID) and networks we can't join are left out; the technician
    can still type a hidden one by hand.
    """
    networks: list[Network] = []
    for line in stdout.splitlines():
        fields = split_terse(line)
        if len(fields) != 3:
            continue
        ssid, signal, security_text = fields
        security = map_security(security_text)
        if not ssid or not signal.isdigit():
            continue
        if security is None:
            log.debug("network_not_supported", ssid=ssid, security=security_text)
            continue
        try:
            networks.append(Network(ssid=ssid, signal=int(signal), security=security))
        except MessageError:
            continue  # e.g. an SSID over 32 bytes after nmcli's escaping of odd bytes
    return networks


def parse_devices(stdout: str) -> str | None:
    """`nmcli -t -f DEVICE,TYPE,STATE device`: the first Wi-Fi interface NM manages."""
    for line in stdout.splitlines():
        fields = split_terse(line)
        if len(fields) == 3 and fields[1] == "wifi" and fields[2] != "unmanaged":
            return fields[0]
    return None


def parse_connections(stdout: str) -> list[tuple[str, str]]:
    """`nmcli -t -f NAME,UUID connection show` -> (name, uuid) pairs."""
    pairs = []
    for line in stdout.splitlines():
        fields = split_terse(line)
        if len(fields) == 2 and fields[0] and fields[1]:
            pairs.append((fields[0], fields[1]))
    return pairs


@dataclass(frozen=True)
class DeviceShow:
    ip: str | None
    gateway: str | None
    connectivity: NmConnectivity


_CONNECTIVITY_RE = re.compile(r"\((\w+)\)")  # "4 (full)" -> "full"


def _ipv4_or_none(text: str) -> str | None:
    try:
        return str(ipaddress.IPv4Address(text))
    except ValueError:
        return None


def parse_device_show(stdout: str) -> DeviceShow:
    """`nmcli -t -f GENERAL.IP4-CONNECTIVITY,IP4.ADDRESS,IP4.GATEWAY device show <iface>`."""
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        fields = split_terse(line)
        if len(fields) == 2:
            values.setdefault(fields[0], fields[1])
    address = values.get("IP4.ADDRESS[1]", "").split("/")[0]  # "192.168.1.23/24"
    match = _CONNECTIVITY_RE.search(values.get("GENERAL.IP4-CONNECTIVITY", ""))
    try:
        connectivity = NmConnectivity(match.group(1)) if match else NmConnectivity.UNKNOWN
    except ValueError:
        connectivity = NmConnectivity.UNKNOWN
    return DeviceShow(
        ip=_ipv4_or_none(address),
        gateway=_ipv4_or_none(values.get("IP4.GATEWAY", "")),
        connectivity=connectivity,
    )


# Substrings of nmcli / NetworkManager 1.46 messages (lowercase), checked in this order.
_REJECTED = ("secrets were required", "supplicant")  # wrong PSK, or 802.1X login refused
_NOT_FOUND = ("could not be found", "no network with ssid")
_NO_IP = ("ip configuration could not be reserved", "dhcp")


def map_failure(returncode: int, output: str, creds: Credentials) -> FailureReason:
    """nmcli's error -> a SPEC §7 reason. A heuristic: anything we don't recognise is a
    `timeout`, and the raw text is only in the debug log (written by run())."""
    text = output.lower()
    if returncode == EXIT_TIMEOUT:
        return FailureReason.TIMEOUT
    if any(s in text for s in _REJECTED):
        return rejected_reason(creds)
    if any(s in text for s in _NOT_FOUND):
        return FailureReason.SSID_NOT_FOUND
    if any(s in text for s in _NO_IP):
        return FailureReason.NO_IP
    return FailureReason.TIMEOUT


# ---------------------------------------------------------------------------
# Building commands (pure).


def build_add_args(
    creds: Credentials, profile: str, interface: str, ca_path: Path | None
) -> list[str]:
    """`nmcli connection add` for one network, **without the secret** (review Q9)."""
    args = ["nmcli", "connection", "add", "type", "wifi", "con-name", profile]
    args += ["ifname", interface, "ssid", creds.ssid]
    # Autoconnect only after success (SPEC §8.5), so a failed profile never comes back by
    # itself before we delete it.
    args += ["connection.autoconnect", "no"]
    args += ["connection.autoconnect-priority", str(AUTOCONNECT_PRIORITY)]
    if creds.hidden:
        args += ["wifi.hidden", "yes"]
    if creds.security == Security.WPA2_PSK:
        args += ["wifi-sec.key-mgmt", "wpa-psk"]
    elif creds.security == Security.WPA3_SAE:
        args += ["wifi-sec.key-mgmt", "sae"]
    elif creds.security == Security.WPA2_EAP_PEAP and creds.eap is not None:
        eap = creds.eap
        args += ["wifi-sec.key-mgmt", "wpa-eap", "802-1x.eap", "peap"]
        args += ["802-1x.phase2-auth", "mschapv2", "802-1x.identity", eap.identity]
        if eap.anonymous_identity is not None:
            args += ["802-1x.anonymous-identity", eap.anonymous_identity]
        if ca_path is not None:
            args += ["802-1x.ca-cert", str(ca_path)]
        if eap.domain_suffix_match is not None:
            args += ["802-1x.domain-suffix-match", eap.domain_suffix_match]
    else:
        raise ValueError(f"security {creds.security} is not supported")
    return args


def build_secret_script(creds: Credentials) -> str:
    """The editor commands piped into `nmcli connection edit` to set the secret."""
    if creds.security == Security.WPA2_EAP_PEAP and creds.eap is not None:
        setting, secret = "802-1x.password", creds.eap.password
    elif creds.psk is not None:
        setting, secret = "wifi-sec.psk", creds.psk
    else:
        raise ValueError("these credentials have no secret")
    if "\n" in secret or "\r" in secret:
        # A line break would end the `set` line and start a new editor command.
        raise ValueError("the secret contains a line break")
    return f"set {setting} {secret}\nsave\nquit\n"


# ---------------------------------------------------------------------------
# I/O: the checks after activation, and the backend itself.


class NmcliConnectivity:
    """Implements Connectivity: what the Wi-Fi interface has after activation (SPEC §8.4)."""

    def __init__(self, runner: Runner = run) -> None:
        self._run = runner

    async def verify(self, interface: str) -> VerifyResult:
        # Ask NM to check now; otherwise it reports the result of its last periodic check.
        await self._run(["nmcli", "networking", "connectivity", "check"], QUICK_TIMEOUT_S)
        fields = "GENERAL.IP4-CONNECTIVITY,IP4.ADDRESS,IP4.GATEWAY"
        show = await self._run(
            ["nmcli", "-t", "-f", fields, "device", "show", interface], QUICK_TIMEOUT_S
        )
        info = parse_device_show(show.stdout)
        reachable: bool | None = None
        if info.ip is not None and info.connectivity not in (
            NmConnectivity.FULL,
            NmConnectivity.PORTAL,
        ):
            reachable = info.gateway is not None and await self._ping(interface, info.gateway)
        return VerifyResult(ip=info.ip, connectivity=info.connectivity, gateway_reachable=reachable)

    async def _ping(self, interface: str, gateway: str) -> bool:
        # -I: on the Wi-Fi interface, not over Ethernet or another route (review Q8).
        argv = ["ping", "-I", interface, "-c", "2", "-W", "2", gateway]
        return (await self._run(argv, PING_TIMEOUT_S)).returncode == 0


class NmcliBackend:
    """Implements WifiBackend with NetworkManager (SPEC §8)."""

    def __init__(
        self,
        runner: Runner = run,
        state_dir: Path = DEFAULT_STATE_DIR,
        connectivity: Connectivity | None = None,
        interface: str | None = None,
        verify_timeout_s: float = VERIFY_TIMEOUT_S,
    ) -> None:
        self._run = runner
        self._certs = state_dir / "certs"
        self._connectivity = connectivity or NmcliConnectivity(runner)
        self._interface = interface  # None: the first Wi-Fi device NM manages
        self._verify_timeout_s = verify_timeout_s

    async def scan(self) -> list[Network]:
        argv = ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"]
        result = await self._run([*argv, "--rescan", "yes"], SCAN_TIMEOUT_S)
        if result.timed_out or result.returncode != 0:
            log.warning("scan_failed", returncode=result.returncode, timed_out=result.timed_out)
            return []
        return scan_list(parse_scan(result.stdout))

    async def connect(self, creds: Credentials, timeout_s: float) -> ConnectResult:
        interface = await self._wifi_interface()
        if interface is None:
            log.error("no_wifi_interface")
            return ConnectResult.failure(FailureReason.TIMEOUT)
        try:
            script = build_secret_script(creds)
        except ValueError:
            return ConnectResult.failure(rejected_reason(creds))

        profile = profile_name(creds.ssid)
        await self._delete_profiles(lambda name: name == profile)  # a leftover from a crash
        result = await self._activate(creds, profile, interface, script, timeout_s)
        if result.ok:
            argv = ["nmcli", "connection", "modify", "id", profile, "connection.autoconnect"]
            await self._run([*argv, "yes"], QUICK_TIMEOUT_S)
        else:
            # Roll back (SPEC §8.6): NetworkManager then returns to the previous network.
            await self._delete_profiles(lambda name: name == profile)
            self._cert_path(profile).unlink(missing_ok=True)
        return result

    async def forget_all(self) -> list[str]:
        removed = await self._delete_profiles(lambda name: name.startswith(PROFILE_PREFIX))
        for pem in self._certs.glob("*.pem"):
            pem.unlink(missing_ok=True)
        return removed

    async def _activate(
        self, creds: Credentials, profile: str, interface: str, script: str, timeout_s: float
    ) -> ConnectResult:
        ca_path = None
        if creds.eap is not None and creds.eap.ca_cert_pem is not None:
            ca_path = self._write_ca(profile, creds.eap.ca_cert_pem)
        steps: list[tuple[list[str], str | None, float]] = [
            (build_add_args(creds, profile, interface, ca_path), None, QUICK_TIMEOUT_S),
            (["nmcli", "connection", "edit", "id", profile], script, QUICK_TIMEOUT_S),
            (
                ["nmcli", "--wait", str(int(timeout_s)), "connection", "up", "id", profile],
                None,
                timeout_s + 5,  # nmcli gives up first, with its own message
            ),
        ]
        for argv, stdin, limit in steps:
            r = await self._run(argv, limit, stdin)
            if r.timed_out:
                return ConnectResult.failure(FailureReason.TIMEOUT)
            # nmcli's editor exits 0 even when a command in the script failed.
            editor_error = stdin is not None and "Error" in r.stdout + r.stderr
            if r.returncode != 0 or editor_error:
                return ConnectResult.failure(map_failure(r.returncode, r.stdout + r.stderr, creds))
        try:
            async with asyncio.timeout(self._verify_timeout_s):  # review Q10
                verify = await self._connectivity.verify(interface)
        except TimeoutError:
            return ConnectResult.failure(FailureReason.TIMEOUT)
        return decide_result(verify, security_warnings(creds))

    async def _wifi_interface(self) -> str | None:
        if self._interface is None:
            r = await self._run(
                ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"], QUICK_TIMEOUT_S
            )
            self._interface = parse_devices(r.stdout)
            if self._interface is not None:
                log.info("wifi_interface", interface=self._interface)
        return self._interface

    async def _delete_profiles(self, match: Callable[[str], bool]) -> list[str]:
        """Delete the profiles whose name matches, by UUID, so two profiles with the same
        name can't make the command ambiguous."""
        listing = await self._run(
            ["nmcli", "-t", "-f", "NAME,UUID", "connection", "show"], QUICK_TIMEOUT_S
        )
        removed = []
        for name, uuid in parse_connections(listing.stdout):
            if not match(name):
                continue
            r = await self._run(["nmcli", "connection", "delete", "uuid", uuid], QUICK_TIMEOUT_S)
            if r.returncode == 0:
                removed.append(name)
            else:
                log.warning("profile_delete_failed", profile=name)
        return removed

    def _cert_path(self, profile: str) -> Path:
        # Hex keeps any SSID (slashes, spaces, emoji) a safe file name.
        return self._certs / f"{profile.encode().hex()}.pem"

    def _write_ca(self, profile: str, pem: str) -> Path:
        """NetworkManager needs the CA as a file. Only we (and root) may read it."""
        if not self._certs.exists():
            self._certs.mkdir(parents=True)
            self._certs.chmod(0o700)
        path = self._cert_path(profile)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(pem)
        path.chmod(0o600)  # O_CREAT's mode doesn't apply to a file that already existed
        return path
