# Tests for the technician's texts: every reason, error code and warning has one, failures
# carry a next action, and nothing sounds like a developer wrote it (SPEC §7, §9.2,
# reviews Q26–Q30).

import re

import pytest

from teton_common.messages import NetworkInfo
from teton_common.protocol import (
    CAPS,
    ErrorCode,
    FailureReason,
    InternetStatus,
    Security,
    StatusWarning,
)
from teton_configurator import ui_text
from teton_configurator.flow import HIDDEN_SECURITY

JARGON = ("nmcli", "GATT", "BLE", "exception", "traceback", "D-Bus")
# Word boundaries, so "BLE" doesn't match "unable" (review Q29).
_JARGON_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in JARGON) + r")\b", re.IGNORECASE)

CONFIGURATOR_FAILURES = (
    ui_text.WRONG_CODE,
    ui_text.NO_REPLY,
    ui_text.RESULT_TIMEOUT,
    ui_text.CONNECTION_LOST,
    ui_text.UNEXPECTED_REPLY,
)
INPUT_CHECKS = (
    ui_text.INVALID_CODE,
    ui_text.INVALID_PASSWORD_LENGTH,
    ui_text.INVALID_NETWORK_NAME,
    ui_text.REQUIRED,
    ui_text.INVALID_CHOICE,
    ui_text.CA_FILE_UNREADABLE,
    ui_text.CA_FILE_TOO_LARGE,
    ui_text.CA_FILE_NOT_CERTIFICATE,
)


def failure_texts() -> list[str]:
    """Everything that tells the technician something went wrong."""
    return [
        *(ui_text.failure_text(r) for r in FailureReason),
        *(ui_text.error_text(c, retry_after_s=30) for c in ErrorCode),
        *(ui_text.warning_text(w) for w in StatusWarning),
        *CONFIGURATOR_FAILURES,
        *INPUT_CHECKS,
        ui_text.NO_DEVICE_FOUND,
        ui_text.NO_NETWORKS,
        ui_text.BLUETOOTH_UNAVAILABLE,
        ui_text.PAGE_UNSUPPORTED,
        ui_text.PAGE_STOPPED,
        ui_text.PAGE_NO_NETWORKS,
        ui_text.PAGE_ENTERPRISE,
    ]


def every_text() -> list[str]:
    constants = [v for k, v in vars(ui_text).items() if k.isupper() and isinstance(v, str)]
    results = [ui_text.connected_text("192.168.1.23", i) for i in InternetStatus]
    labels = [ui_text.hidden_security_label(s) for s in HIDDEN_SECURITY]
    return [*constants, *failure_texts(), *results, *labels, ui_text.connecting_text("Home-PSK")]


# --- coverage: a new enum value fails here until it gets a text ---------------------------


@pytest.mark.parametrize("reason", list(FailureReason))
def test_every_failure_reason_has_a_text(reason: FailureReason) -> None:
    assert ui_text.failure_text(reason)


@pytest.mark.parametrize("code", list(ErrorCode))
def test_every_error_code_has_a_text(code: ErrorCode) -> None:
    assert ui_text.error_text(code, retry_after_s=30)


@pytest.mark.parametrize("warning", list(StatusWarning))
def test_every_warning_has_a_text(warning: StatusWarning) -> None:
    assert ui_text.warning_text(warning)


@pytest.mark.parametrize("security", CAPS)
def test_every_listed_security_type_has_a_label(security: Security) -> None:
    net = NetworkInfo(ssid="Net", signal=50, security=security)
    assert ui_text.network_label(net).endswith(("password", "enterprise"))


@pytest.mark.parametrize("security", HIDDEN_SECURITY)
def test_every_hidden_network_security_type_has_a_label(security: Security) -> None:
    assert ui_text.hidden_security_label(security)  # review Q42


# --- plain language -------------------------------------------------------------------


def test_the_jargon_check_works() -> None:
    assert _JARGON_RE.search("GATT write failed")
    assert _JARGON_RE.search("a ble error")
    assert not _JARGON_RE.search("We were unable to connect.")  # "ble" inside a word


@pytest.mark.parametrize("text", every_text())
def test_no_text_contains_jargon(text: str) -> None:
    assert not _JARGON_RE.search(text), text


@pytest.mark.parametrize("text", failure_texts())
def test_every_failure_has_a_next_action(text: str) -> None:
    """One sentence saying what happened, then at least one more saying what to do."""
    sentences = [s for s in re.split(r"(?<=[.;])\s+", text) if s]
    assert len(sentences) >= 2, text
    assert text.endswith("."), text


# --- the decided wording --------------------------------------------------------------


def test_spec_texts() -> None:
    assert ui_text.failure_text(FailureReason.WRONG_PASSWORD) == (
        "The Wi-Fi password was not accepted. Check it and try again."
    )
    assert ui_text.failure_text(FailureReason.AUTH_FAILED).endswith(
        "Check them and try again."  # review Q26
    )
    assert ui_text.NO_DEVICE_FOUND == (
        "No device found. Press the pairing button on the device and try again."
    )
    assert "Press the pairing button on the device" in ui_text.NO_REPLY  # review Q22


def test_result_timeout_is_not_the_device_timeout_text() -> None:
    # The device always finishes and may have connected, so point at its light (Q27).
    assert ui_text.RESULT_TIMEOUT != ui_text.failure_text(FailureReason.TIMEOUT)
    assert "green" in ui_text.RESULT_TIMEOUT


def test_rate_limited_says_how_long_to_wait() -> None:
    assert "Wait 30 seconds" in ui_text.error_text(ErrorCode.RATE_LIMITED, retry_after_s=30)
    assert "Wait 1 second," in ui_text.error_text(ErrorCode.RATE_LIMITED, retry_after_s=1)


def test_connected_text() -> None:
    assert ui_text.connected_text("192.168.1.23", InternetStatus.FULL) == (
        "Connected. IP 192.168.1.23, internet OK."
    )
    limited = ui_text.connected_text("10.0.0.5", InternetStatus.LIMITED)
    assert limited.startswith("Connected. IP 10.0.0.5") and "OK" not in limited


# --- the network list (review Q30) ----------------------------------------------------


@pytest.mark.parametrize(
    ("signal", "bars"),
    [(100, "▂▄▆█"), (75, "▂▄▆█"), (74, "▂▄▆ "), (50, "▂▄▆ "), (49, "▂▄  "), (25, "▂▄  "),
     (24, "▂   "), (0, "▂   ")],
)  # fmt: skip
def test_signal_bars(signal: int, bars: str) -> None:
    assert ui_text.signal_bars(signal) == bars


def test_network_label() -> None:
    home = NetworkInfo(ssid="Home-PSK", signal=82, security=Security.WPA2_PSK)
    hospital = NetworkInfo(ssid="Hospital-EAP", signal=64, security=Security.WPA2_EAP_PEAP)
    assert ui_text.network_label(home) == "Home-PSK  ▂▄▆█  password"
    assert ui_text.network_label(hospital) == "Hospital-EAP  ▂▄▆   enterprise"
    assert "🔒" not in ui_text.network_label(home)
    assert ui_text.network_label(home, width=12) == "Home-PSK      ▂▄▆█  password"  # lines up
    assert ui_text.device_label("Teton-9A2C", 80) == "Teton-9A2C  ▂▄▆█"
