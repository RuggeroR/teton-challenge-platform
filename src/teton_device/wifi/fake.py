# Simulated Wi-Fi with fixed networks (Home-PSK, Hospital-EAP, Cafe-Portal, Weak-Signal). See SPEC
# §10.
#
# Used by --simulate and by the tests. It follows the same contract as the real backend:
# a failed attempt leaves no profile behind, a successful one keeps teton-<ssid>, and the
# scan list and Enterprise warning come from the shared rules in backend.py. Waiting goes
# through an injectable `sleep`, so tests never wait for real.

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field

import structlog

from teton_common.protocol import FailureReason, InternetStatus, Security
from teton_device.wifi.backend import (
    ConnectResult,
    Credentials,
    Network,
    profile_name,
    scan_list,
    security_warnings,
)

log = structlog.get_logger()

Sleep = Callable[[float], Awaitable[None]]

# The demo credentials of SPEC §10. They are fake networks, so these are not secrets.
HOME_PSK_PASSWORD = "correct-horse-battery"
HOSPITAL_USER = "nurse01"
HOSPITAL_PASSWORD = "teton-demo"


@dataclass(frozen=True)
class FakeNetwork:
    """One simulated access point.

    `outcome` set: the attempt always ends with that failure, whatever password is typed
    (the network itself is the problem, e.g. a login page or a weak signal). Otherwise the
    credentials decide: right ones connect with `ip`, wrong ones are rejected.
    """

    ssid: str
    signal: int
    security: Security
    psk: str | None = field(default=None, repr=False)
    identity: str | None = None
    password: str | None = field(default=None, repr=False)
    outcome: FailureReason | None = None
    ip: str = "192.168.1.23"

    def as_network(self) -> Network:
        return Network(ssid=self.ssid, signal=self.signal, security=self.security)


DEFAULT_NETWORKS: tuple[FakeNetwork, ...] = (
    FakeNetwork("Home-PSK", 82, Security.WPA2_PSK, psk=HOME_PSK_PASSWORD),
    FakeNetwork(
        "Hospital-EAP",
        64,
        Security.WPA2_EAP_PEAP,
        identity=HOSPITAL_USER,
        password=HOSPITAL_PASSWORD,
        ip="10.20.0.57",
    ),
    FakeNetwork("Cafe-Portal", 48, Security.WPA2_PSK, outcome=FailureReason.CAPTIVE_PORTAL),
    FakeNetwork("Weak-Signal", 9, Security.WPA2_PSK, outcome=FailureReason.TIMEOUT),
)


def _credentials_match(net: FakeNetwork, creds: Credentials) -> bool:
    if creds.security != net.security:
        return False
    if creds.security == Security.WPA2_EAP_PEAP:
        return (
            creds.eap is not None
            and creds.eap.identity == net.identity
            and creds.eap.password == net.password
        )
    return creds.psk == net.psk


def _rejected(creds: Credentials) -> FailureReason:
    # The reason NetworkManager would give: Enterprise rejects the login, Personal the key.
    if creds.security == Security.WPA2_EAP_PEAP:
        return FailureReason.AUTH_FAILED
    return FailureReason.WRONG_PASSWORD


class FakeWifiBackend:
    """Implements WifiBackend with simulated networks. Inspect `profiles` in tests."""

    def __init__(
        self,
        networks: Iterable[FakeNetwork] = DEFAULT_NETWORKS,
        sleep: Sleep = asyncio.sleep,
        scan_delay_s: float = 1.0,
        connect_delay_s: float = 2.0,
    ) -> None:
        networks = tuple(networks)  # iterated twice below
        self._networks = {n.ssid: n for n in networks}
        self._scan_list = scan_list(n.as_network() for n in networks)
        self._sleep = sleep
        self._scan_delay_s = scan_delay_s
        self._connect_delay_s = connect_delay_s
        self.profiles: set[str] = set()  # what NetworkManager would have saved

    async def scan(self) -> list[Network]:
        await self._sleep(self._scan_delay_s)
        return list(self._scan_list)

    async def connect(self, creds: Credentials, timeout_s: float) -> ConnectResult:
        result = await self._attempt(creds, timeout_s)
        if result.ok:
            self.profiles.add(profile_name(creds.ssid))
        log.debug("fake_wifi_result", ssid=creds.ssid, ok=result.ok, reason=result.reason)
        return result

    async def forget(self, profile_name: str) -> None:
        self.profiles.discard(profile_name)

    async def _attempt(self, creds: Credentials, timeout_s: float) -> ConnectResult:
        net = self._networks.get(creds.ssid)
        if net is None:
            await self._sleep(self._connect_delay_s)
            return ConnectResult.failure(FailureReason.SSID_NOT_FOUND)
        if net.outcome == FailureReason.TIMEOUT:
            await self._sleep(timeout_s)  # nothing happens until the limit
            return ConnectResult.failure(FailureReason.TIMEOUT)
        await self._sleep(self._connect_delay_s)
        if net.outcome is not None:
            return ConnectResult.failure(net.outcome)
        if not _credentials_match(net, creds):
            return ConnectResult.failure(_rejected(creds))
        return ConnectResult.success(net.ip, InternetStatus.FULL, security_warnings(creds))
