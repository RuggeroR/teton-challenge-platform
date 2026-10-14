# Tests for the fake Wi-Fi backend (the SPEC §10 networks) and the rules shared with the
# real backend: scan list shape, Enterprise warning, profile name.

import pytest

from teton_common.messages import EapCredentials
from teton_common.protocol import FailureReason, InternetStatus, Security, StatusWarning
from teton_device.wifi.backend import (
    ConnectResult,
    Credentials,
    Network,
    profile_name,
    scan_list,
    security_warnings,
)
from teton_device.wifi.fake import (
    HOME_PSK_PASSWORD,
    HOSPITAL_PASSWORD,
    HOSPITAL_USER,
    FakeNetwork,
    FakeWifiBackend,
)

CA_PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


class FakeSleep:
    """Records the waits instead of waiting."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleep() -> FakeSleep:
    return FakeSleep()


@pytest.fixture
def wifi(sleep: FakeSleep) -> FakeWifiBackend:
    return FakeWifiBackend(sleep=sleep)


def psk(ssid: str, password: str) -> Credentials:
    return Credentials(ssid=ssid, security=Security.WPA2_PSK, psk=password)


def peap(user: str = HOSPITAL_USER, password: str = HOSPITAL_PASSWORD, **eap: str) -> Credentials:
    return Credentials(
        ssid="Hospital-EAP",
        security=Security.WPA2_EAP_PEAP,
        eap=EapCredentials(identity=user, password=password, **eap),
    )


async def test_scan_lists_the_spec_networks_strongest_first(wifi: FakeWifiBackend) -> None:
    networks = await wifi.scan()
    assert [n.ssid for n in networks] == ["Home-PSK", "Hospital-EAP", "Cafe-Portal", "Weak-Signal"]
    assert networks[1].security == Security.WPA2_EAP_PEAP


async def test_right_password_connects_and_keeps_the_profile(wifi: FakeWifiBackend) -> None:
    result = await wifi.connect(psk("Home-PSK", HOME_PSK_PASSWORD), timeout_s=45)
    assert result == ConnectResult.success("192.168.1.23", InternetStatus.FULL)
    assert wifi.profiles == {"teton-Home-PSK"}


async def test_wrong_password_gives_wrong_password_and_no_profile(wifi: FakeWifiBackend) -> None:
    result = await wifi.connect(psk("Home-PSK", "not-the-password"), timeout_s=45)
    assert result == ConnectResult.failure(FailureReason.WRONG_PASSWORD)
    assert wifi.profiles == set()  # rolled back (SPEC §8.6)


async def test_retry_after_wrong_password_works(wifi: FakeWifiBackend) -> None:
    await wifi.connect(psk("Home-PSK", "not-the-password"), timeout_s=45)
    assert (await wifi.connect(psk("Home-PSK", HOME_PSK_PASSWORD), timeout_s=45)).ok


async def test_unknown_ssid_gives_ssid_not_found(wifi: FakeWifiBackend) -> None:
    result = await wifi.connect(psk("Hidden-Lab", HOME_PSK_PASSWORD), timeout_s=45)
    assert result.reason == FailureReason.SSID_NOT_FOUND


async def test_cafe_portal_gives_captive_portal_whatever_the_password(
    wifi: FakeWifiBackend,
) -> None:
    result = await wifi.connect(psk("Cafe-Portal", "anything-at-all"), timeout_s=45)
    assert result == ConnectResult.failure(FailureReason.CAPTIVE_PORTAL)
    assert wifi.profiles == set()


async def test_weak_signal_waits_the_whole_timeout(wifi: FakeWifiBackend, sleep: FakeSleep) -> None:
    result = await wifi.connect(psk("Weak-Signal", "anything-at-all"), timeout_s=45)
    assert result == ConnectResult.failure(FailureReason.TIMEOUT)
    assert sleep.calls == [45]  # the injected sleep, so the test didn't wait


async def test_enterprise_with_the_right_login_connects(wifi: FakeWifiBackend) -> None:
    result = await wifi.connect(peap(ca_cert_pem=CA_PEM), timeout_s=45)
    assert result == ConnectResult.success("10.20.0.57", InternetStatus.FULL)
    assert wifi.profiles == {"teton-Hospital-EAP"}


@pytest.mark.parametrize(
    ("user", "password"), [("nurse99", HOSPITAL_PASSWORD), (HOSPITAL_USER, "wrong")]
)
async def test_enterprise_bad_login_gives_auth_failed(
    wifi: FakeWifiBackend, user: str, password: str
) -> None:
    result = await wifi.connect(peap(user, password), timeout_s=45)
    assert result == ConnectResult.failure(FailureReason.AUTH_FAILED)


async def test_enterprise_without_ca_or_domain_connects_with_a_warning(
    wifi: FakeWifiBackend,
) -> None:
    result = await wifi.connect(peap(), timeout_s=45)
    assert result.ok
    assert result.warnings == (StatusWarning.SERVER_NOT_VERIFIED,)


async def test_wrong_security_type_is_rejected(wifi: FakeWifiBackend) -> None:
    result = await wifi.connect(psk("Hospital-EAP", HOSPITAL_PASSWORD), timeout_s=45)
    assert result.reason == FailureReason.WRONG_PASSWORD


async def test_forget_removes_the_profile_and_ignores_unknown_names(
    wifi: FakeWifiBackend,
) -> None:
    await wifi.connect(psk("Home-PSK", HOME_PSK_PASSWORD), timeout_s=45)
    await wifi.forget("teton-Home-PSK")
    await wifi.forget("teton-never-existed")
    assert wifi.profiles == set()


async def test_delays_are_injectable(sleep: FakeSleep) -> None:
    wifi = FakeWifiBackend(sleep=sleep, scan_delay_s=0.5, connect_delay_s=0.25)
    await wifi.scan()
    await wifi.connect(psk("Home-PSK", HOME_PSK_PASSWORD), timeout_s=45)
    assert sleep.calls == [0.5, 0.25]


async def test_custom_networks_go_through_the_scan_rules(sleep: FakeSleep) -> None:
    many = [FakeNetwork(f"Net-{i:02}", i, Security.WPA2_PSK) for i in range(40)]
    twin = FakeNetwork("Net-05", 99, Security.WPA2_PSK)  # a second AP, much stronger
    networks = await FakeWifiBackend([*many, twin], sleep=sleep).scan()
    assert len(networks) == 30
    assert networks[0] == Network("Net-05", 99, Security.WPA2_PSK)
    assert [n.ssid for n in networks].count("Net-05") == 1


# --- the shared rules -------------------------------------------------------------------


def test_scan_list_dedupes_keeping_the_strongest_sorts_and_caps() -> None:
    raw = [
        Network("B", 40, Security.WPA2_PSK),
        Network("A", 70, Security.WPA2_PSK),
        Network("B", 90, Security.WPA2_PSK),
        Network("C", 70, Security.WPA3_SAE),
    ]
    assert scan_list(raw) == [
        Network("B", 90, Security.WPA2_PSK),
        Network("A", 70, Security.WPA2_PSK),  # equal signal: by name, so the order is stable
        Network("C", 70, Security.WPA3_SAE),
    ]
    assert len(scan_list(Network(f"N{i}", 50, Security.WPA2_PSK) for i in range(31))) == 30
    assert scan_list([]) == []


@pytest.mark.parametrize(
    ("eap", "warned"),
    [
        ({}, True),
        ({"ca_cert_pem": CA_PEM}, False),
        ({"domain_suffix_match": "radius.hospital.example"}, False),
        ({"ca_cert_pem": CA_PEM, "domain_suffix_match": "radius.hospital.example"}, False),
    ],
)
def test_server_not_verified_only_without_ca_and_domain(eap: dict[str, str], warned: bool) -> None:
    expected = (StatusWarning.SERVER_NOT_VERIFIED,) if warned else ()
    assert security_warnings(peap(**eap)) == expected


def test_personal_networks_never_get_the_warning() -> None:
    assert security_warnings(psk("Home-PSK", HOME_PSK_PASSWORD)) == ()


def test_profile_name() -> None:
    assert profile_name("Home-PSK") == "teton-Home-PSK"


def test_credentials_repr_hides_secrets() -> None:
    text = repr(psk("Home-PSK", HOME_PSK_PASSWORD)) + repr(peap(ca_cert_pem=CA_PEM))
    assert HOME_PSK_PASSWORD not in text
    assert HOSPITAL_PASSWORD not in text
    assert "MIIB" not in text
    assert "SECRET" not in repr(FakeNetwork("X", 1, Security.WPA2_PSK, psk="SECRET-123"))
