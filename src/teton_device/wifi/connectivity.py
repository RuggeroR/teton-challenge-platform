# Post-join verification: the success rule (review Q8) that turns what the checks found on
# the Wi-Fi interface into a ConnectResult. See SPEC §7, §8.4, §8.5.
#
# Pure: no commands run here. The checks themselves (IPv4 address, `nmcli networking
# connectivity check`, gateway ping) run in wifi/nmcli.py (NmcliConnectivity), so nmcli is
# only ever called from that one module.

from teton_common.protocol import FailureReason, InternetStatus, StatusWarning
from teton_device.wifi.backend import ConnectResult, NmConnectivity, VerifyResult


def decide_result(verify: VerifyResult, warnings: tuple[StatusWarning, ...] = ()) -> ConnectResult:
    """Success = activated + IPv4 address + (full internet, or a reachable gateway)."""
    if verify.ip is None:
        return ConnectResult.failure(FailureReason.NO_IP)
    if verify.connectivity == NmConnectivity.PORTAL:
        return ConnectResult.failure(FailureReason.CAPTIVE_PORTAL)
    if verify.connectivity == NmConnectivity.FULL:
        return ConnectResult.success(verify.ip, InternetStatus.FULL, warnings)
    if not verify.gateway_reachable:
        return ConnectResult.failure(FailureReason.NO_INTERNET)
    # On the network, but the internet isn't confirmed. Hospital networks often block
    # Ubuntu's check URL, so this is a warning, not a failure (SPEC §8.5).
    internet = InternetStatus(verify.connectivity.value)  # limited, none or unknown
    return ConnectResult.success(verify.ip, internet, (*warnings, StatusWarning.LIMITED_INTERNET))
