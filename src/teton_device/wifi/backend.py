# WifiBackend interface and result types (Network, Credentials, ConnectResult). See DESIGN §5.
#
# The only door to Wi-Fi: the session and the service call these interfaces, never nmcli.
# The rules both backends must agree on (scan list shape, Enterprise warning, profile name)
# live here once, so the fake used in tests and simulation can't drift from the real one.

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, Self

from teton_common.messages import MAX_SCAN_NETWORKS, NetworkInfo, SetCredentials
from teton_common.protocol import FailureReason, InternetStatus, Security, StatusWarning

# One scanned network has exactly the shape of a scan_result entry (SPEC §6.3), so the
# session sends what the backend returns, with no conversion.
Network = NetworkInfo

# What the configurator sent, already validated by messages.py, and with secrets hidden
# from repr(). The backend takes it as is rather than copying it into a second type that
# would need the same validation and the same repr rules.
Credentials = SetCredentials

PROFILE_PREFIX = "teton-"  # every NetworkManager profile we create starts with this


def profile_name(ssid: str) -> str:
    """The dedicated NetworkManager profile for one network: teton-<ssid> (SPEC §8)."""
    return PROFILE_PREFIX + ssid


def scan_list(networks: Iterable[Network]) -> list[Network]:
    """SPEC §6.3: one entry per SSID (the strongest), strongest first, at most 30."""
    best: dict[str, Network] = {}
    for net in networks:
        if net.ssid not in best or net.signal > best[net.ssid].signal:
            best[net.ssid] = net
    ordered = sorted(best.values(), key=lambda n: (-n.signal, n.ssid))  # ssid: stable ties
    return ordered[:MAX_SCAN_NETWORKS]


def security_warnings(creds: Credentials) -> tuple[StatusWarning, ...]:
    """PEAP without a CA certificate and without a domain can't verify the RADIUS server,
    so an evil twin could collect the login (decision Q3). We connect, but say so."""
    if creds.security == Security.WPA2_EAP_PEAP and creds.eap is not None:
        if creds.eap.ca_cert_pem is None and creds.eap.domain_suffix_match is None:
            return (StatusWarning.SERVER_NOT_VERIFIED,)
    return ()


def rejected_reason(creds: Credentials) -> FailureReason:
    """The network refused the secret: Enterprise rejects the login, Personal the key."""
    if creds.security == Security.WPA2_EAP_PEAP:
        return FailureReason.AUTH_FAILED
    return FailureReason.WRONG_PASSWORD


@dataclass(frozen=True)
class ConnectResult:
    """The outcome of one attempt. Build it with success() or failure(), so a result is
    always either connected (ip + internet) or failed (reason), never a mix."""

    ok: bool
    reason: FailureReason | None = None
    ip: str | None = None
    internet: InternetStatus | None = None  # needed by the `connected` status (SPEC §6.3)
    warnings: tuple[StatusWarning, ...] = ()

    @classmethod
    def success(
        cls, ip: str, internet: InternetStatus, warnings: tuple[StatusWarning, ...] = ()
    ) -> Self:
        return cls(ok=True, ip=ip, internet=internet, warnings=warnings)

    @classmethod
    def failure(cls, reason: FailureReason) -> Self:
        return cls(ok=False, reason=reason)


class NmConnectivity(StrEnum):
    """NetworkManager's connectivity of one interface. Unlike InternetStatus it has
    `portal`, which is a failure (captive_portal), so it is never sent as `internet`."""

    FULL = "full"
    LIMITED = "limited"
    PORTAL = "portal"
    NONE = "none"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class VerifyResult:
    """What the checks after activation found on the Wi-Fi interface (SPEC §8.4)."""

    ip: str | None  # IPv4 address, None if there is none
    connectivity: NmConnectivity
    gateway_reachable: bool | None  # ping on the interface; None = not needed (full/portal)


class WifiBackend(Protocol):
    async def scan(self) -> list[Network]:
        """Nearby networks, already passed through scan_list()."""
        ...

    async def connect(self, creds: Credentials, timeout_s: float) -> ConnectResult:
        """Create teton-<ssid>, activate it and verify it. On failure the new profile is
        deleted again (SPEC §8.6). Wi-Fi problems are a result, never an exception."""
        ...

    async def forget_all(self) -> list[str]:
        """[R][R]: delete every profile we created (name starts with teton-) and return
        their names. Found by prefix, so it works after a restart too (review Q24)."""
        ...


class Connectivity(Protocol):
    async def verify(self, interface: str) -> VerifyResult: ...
