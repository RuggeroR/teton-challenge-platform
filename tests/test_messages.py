# Tests for message encode/decode and validation of bad input.

import json
from typing import Any

import pytest

from teton_common import messages as m
from teton_common.messages import MessageError
from teton_common.protocol import (
    DeviceState,
    ErrorCode,
    FailureReason,
    InternetStatus,
    Security,
    StatusState,
    StatusWarning,
)

PUB = bytes(range(32))
NONCE = bytes(range(16))
TAG = bytes(range(100, 132))
DEVICE_ID = "TETON-3F9A2C"
PSK = "correct-horse-battery"
EAP_PASSWORD = "teton-demo"


def raw(d: Any) -> bytes:
    return json.dumps(d).encode()


def peap(**eap_fields: Any) -> m.SetCredentials:
    return m.SetCredentials(
        ssid="Hospital-EAP",
        security=Security.WPA2_EAP_PEAP,
        eap=m.EapCredentials(identity="nurse01", password=EAP_PASSWORD, **eap_fields),
    )


# ---------------------------------------------------------------------------
# Round trips: encode, then decode on the right channel, gives the same object.

SESSION_MESSAGES = [
    m.Hello(pub=PUB, nonce=NONCE),
    m.HelloReply(device_id=DEVICE_ID, pub=PUB, nonce=NONCE, confirm=TAG),
    m.Confirm(confirm=TAG),
    m.HandshakeFailed(),
    m.Error(code=ErrorCode.BAD_REQUEST),
    m.Error(code=ErrorCode.RATE_LIMITED, retry_after_s=30),
]
COMMAND_MESSAGES = [
    m.Scan(),
    m.SetCredentials(ssid="Home-PSK", security=Security.WPA2_PSK, psk=PSK),
    m.SetCredentials(ssid="Home-SAE", security=Security.WPA3_SAE, psk=PSK, hidden=True),
    m.SetCredentials(ssid="Cafe", security=Security.OPEN),
    peap(),
    peap(
        anonymous_identity="anonymous",
        ca_cert_pem="-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
        domain_suffix_match="radius.example.org",
    ),
]
STATUS_MESSAGES = [
    m.Ready(),
    m.ScanResult(networks=()),
    m.ScanResult(
        networks=(
            m.NetworkInfo(ssid="Home-PSK", signal=82, security=Security.WPA2_PSK),
            m.NetworkInfo(ssid="Hospital-EAP", signal=40, security=Security.WPA2_EAP_PEAP),
        )
    ),
    m.Status(state=StatusState.CONNECTING),
    m.Status(
        state=StatusState.CONNECTED,
        ip="192.168.1.23",
        internet=InternetStatus.LIMITED,
        warnings=(StatusWarning.LIMITED_INTERNET, StatusWarning.SERVER_NOT_VERIFIED),
    ),
    m.Status(state=StatusState.FAILED, reason=FailureReason.WRONG_PASSWORD),
    m.Error(code=ErrorCode.UNSUPPORTED_SECURITY),
]


@pytest.mark.parametrize("msg", SESSION_MESSAGES)
def test_session_round_trip(msg: m.SessionMessage) -> None:
    assert m.decode_session(m.encode(msg)) == msg


@pytest.mark.parametrize("msg", COMMAND_MESSAGES)
def test_command_round_trip(msg: m.CommandMessage) -> None:
    assert m.decode_command(m.encode(msg)) == msg


@pytest.mark.parametrize("msg", STATUS_MESSAGES)
def test_status_round_trip(msg: m.StatusMessage) -> None:
    assert m.decode_status(m.encode(msg)) == msg


def test_envelope_round_trip() -> None:
    env = m.Envelope(seq=7, ct=b"\x00\x01ciphertext")
    assert m.decode_envelope(m.encode(env)) == env


def test_info_round_trip_and_spec_shape() -> None:
    info = m.Info(device_id=DEVICE_ID, state=DeviceState.ADVERTISING)
    assert m.decode_info(m.encode(info)) == info
    # Same JSON as the SPEC §6.1 example.
    assert json.loads(m.encode(info)) == {
        "v": 1,
        "device_id": "TETON-3F9A2C",
        "name": "Teton-9A2C",
        "state": "advertising",
        "caps": ["wpa2-psk", "wpa3-sae", "wpa2-eap-peap"],
    }


def test_encoding_is_compact_json_with_binary_as_base64() -> None:
    data = m.encode(m.Hello(pub=PUB, nonce=NONCE))
    assert b" " not in data
    d = json.loads(data)
    assert d["type"] == "hello" and d["v"] == 1
    assert d["pub"] == "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="


# ---------------------------------------------------------------------------
# Each channel accepts only its own message types.


def test_channels_reject_each_others_messages() -> None:
    with pytest.raises(MessageError):
        m.decode_status(m.encode(COMMAND_MESSAGES[1]))  # set_credentials on status
    with pytest.raises(MessageError):
        m.decode_command(m.encode(SESSION_MESSAGES[1]))  # hello_reply on command
    with pytest.raises(MessageError):
        m.decode_session(m.encode(m.Scan()))  # scan on session (would be plaintext)


@pytest.mark.parametrize(
    "data",
    [
        b"not json",
        b"\xff\xfe",  # not UTF-8
        b"[1, 2]",  # JSON, but not an object
        raw({}),  # no type
        raw({"type": "launch_rockets"}),
        raw({"type": ["hello"]}),  # type of the wrong JSON type
    ],
)
def test_garbage_is_rejected(data: bytes) -> None:
    with pytest.raises(MessageError):
        m.decode_session(data)


# ---------------------------------------------------------------------------
# Field validation.


def hello_dict(**changes: Any) -> dict[str, Any]:
    d = json.loads(m.encode(m.Hello(pub=PUB, nonce=NONCE)))
    d.update(changes)
    return d


@pytest.mark.parametrize(
    "d",
    [
        hello_dict(extra="field"),
        {k: v for k, v in hello_dict().items() if k != "nonce"},
        hello_dict(pub="not base64!"),
        hello_dict(pub="AAEC"),  # valid base64, but 3 bytes
        hello_dict(v=2),
        hello_dict(v=True),  # True == 1 in Python, still not a version number
    ],
)
def test_bad_hello_fields_are_rejected(d: dict[str, Any]) -> None:
    with pytest.raises(MessageError):
        m.decode_session(raw(d))


@pytest.mark.parametrize("device_id", ["TETON-3f9a2c", "TETON-3F9A2", "teton-3F9A2C", "X-3F9A2C"])
def test_device_id_format(device_id: str) -> None:
    with pytest.raises(MessageError):
        m.HelloReply(device_id=device_id, pub=PUB, nonce=NONCE, confirm=TAG)


@pytest.mark.parametrize("seq", [-1, 2**64, True, "3", 1.0])
def test_bad_seq_is_rejected(seq: Any) -> None:
    with pytest.raises(MessageError):
        m.decode_envelope(raw({"seq": seq, "ct": "AAAA"}))


def test_seq_range_edges_are_accepted() -> None:
    for seq in (0, 2**64 - 1):
        assert m.decode_envelope(raw({"seq": seq, "ct": "AAAA"})).seq == seq


def test_ssid_is_limited_in_bytes_not_characters() -> None:
    m.SetCredentials(ssid="A" * 32, security=Security.OPEN)
    m.SetCredentials(ssid="😀" * 8, security=Security.OPEN)  # 8 characters, 32 bytes
    with pytest.raises(MessageError):
        m.SetCredentials(ssid="😀" * 9, security=Security.OPEN)  # 9 characters, 36 bytes
    with pytest.raises(MessageError):
        m.SetCredentials(ssid="", security=Security.OPEN)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"security": Security.WPA2_PSK},  # psk missing
        {"security": Security.WPA2_PSK, "psk": "x" * 7},
        {"security": Security.WPA3_SAE, "psk": "x" * 64},
        {"security": Security.OPEN, "psk": PSK},  # psk not allowed
        {"security": Security.WPA2_EAP_PEAP},  # eap missing
        {"security": Security.WPA2_EAP_PEAP, "psk": PSK},
        {
            "security": Security.WPA2_PSK,
            "psk": PSK,
            "eap": m.EapCredentials(identity="nurse01", password=EAP_PASSWORD),
        },
    ],
)
def test_credentials_must_match_security_type(kwargs: dict[str, Any]) -> None:
    with pytest.raises(MessageError):
        m.SetCredentials(ssid="Net", **kwargs)


def test_psk_length_edges_are_accepted() -> None:
    for psk in ("x" * 8, "x" * 63):
        m.SetCredentials(ssid="Net", security=Security.WPA2_PSK, psk=psk)


def test_eap_fields_are_checked() -> None:
    with pytest.raises(MessageError):
        m.EapCredentials(identity="", password=EAP_PASSWORD)
    with pytest.raises(MessageError):
        m.EapCredentials(identity="nurse01", password=EAP_PASSWORD, domain_suffix_match="")
    d = json.loads(m.encode(peap()))
    d["eap"] = None  # null is not "absent"
    with pytest.raises(MessageError):
        m.decode_command(raw(d))
    d["eap"] = {"identity": "nurse01", "password": EAP_PASSWORD, "certificate": "x"}
    with pytest.raises(MessageError):
        m.decode_command(raw(d))


def test_hidden_must_be_a_boolean() -> None:
    d = json.loads(m.encode(m.SetCredentials(ssid="Net", security=Security.OPEN)))
    d["hidden"] = "yes"
    with pytest.raises(MessageError):
        m.decode_command(raw(d))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"state": StatusState.CONNECTED, "internet": InternetStatus.FULL},  # no ip
        {"state": StatusState.CONNECTED, "ip": "10.0.0.5"},  # no internet
        {"state": StatusState.CONNECTED, "ip": "not-an-ip", "internet": InternetStatus.FULL},
        {"state": StatusState.FAILED},  # no reason
        {"state": StatusState.CONNECTING, "reason": FailureReason.TIMEOUT},
        {"state": StatusState.CONNECTING, "warnings": (StatusWarning.LIMITED_INTERNET,)},
        {"state": StatusState.FAILED, "reason": FailureReason.NO_IP, "ip": "10.0.0.5"},
    ],
)
def test_status_fields_must_match_state(kwargs: dict[str, Any]) -> None:
    with pytest.raises(MessageError):
        m.Status(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"code": ErrorCode.RATE_LIMITED},  # needs retry_after_s
        {"code": ErrorCode.BAD_REQUEST, "retry_after_s": 30},  # only with rate_limited
        {"code": ErrorCode.RATE_LIMITED, "retry_after_s": 0},
    ],
)
def test_retry_after_goes_with_rate_limited_only(kwargs: dict[str, Any]) -> None:
    with pytest.raises(MessageError):
        m.Error(**kwargs)


def test_rate_limited_wire_shape() -> None:
    data = m.encode(m.Error(code=ErrorCode.RATE_LIMITED, retry_after_s=60))
    assert json.loads(data) == {"type": "error", "code": "rate_limited", "retry_after_s": 60}
    with pytest.raises(MessageError):
        m.decode_session(b'{"type":"error","code":"rate_limited","retry_after_s":"60"}')


def test_unknown_enum_values_are_rejected() -> None:
    with pytest.raises(MessageError):
        m.decode_status(raw({"type": "status", "state": "failed", "reason": "gremlins"}))
    with pytest.raises(MessageError):
        m.decode_status(raw({"type": "error", "code": "busy"}))  # removed by plan_v5 S1


def test_scan_result_limits() -> None:
    net = m.NetworkInfo(ssid="Net", signal=50, security=Security.WPA2_PSK)
    m.ScanResult(networks=(net,) * 30)
    with pytest.raises(MessageError):
        m.ScanResult(networks=(net,) * 31)
    for signal in (-1, 101):
        with pytest.raises(MessageError):
            m.NetworkInfo(ssid="Net", signal=signal, security=Security.WPA2_PSK)


def test_info_checks() -> None:
    d = json.loads(m.encode(m.Info(device_id=DEVICE_ID, state=DeviceState.ADVERTISING)))
    d["name"] = "Teton-0000"
    with pytest.raises(MessageError):
        m.decode_info(raw(d))
    with pytest.raises(MessageError):  # too big for one GATT read (plan_v5 S2)
        m.Info(device_id=DEVICE_ID, state=DeviceState.ADVERTISING, caps=tuple(Security) * 8)


# ---------------------------------------------------------------------------
# Secrets never show up in repr() or in error messages.


def test_secrets_are_not_in_repr() -> None:
    creds = peap(ca_cert_pem="-----BEGIN CERTIFICATE-----SECRETCA")
    psk_creds = m.SetCredentials(ssid="Home-PSK", security=Security.WPA2_PSK, psk=PSK)
    envelope = m.Envelope(seq=1, ct=b"CIPHERTEXT")
    text = repr(creds) + repr(psk_creds) + repr(envelope)
    for secret in (EAP_PASSWORD, "SECRETCA", PSK, "CIPHERTEXT"):
        assert secret not in text
    assert "Hospital-EAP" in text and "nurse01" in text  # non-secret fields still visible


def test_errors_do_not_contain_values() -> None:
    with pytest.raises(MessageError) as exc:
        m.SetCredentials(ssid="Net", security=Security.WPA2_PSK, psk="hunter2")
    assert "hunter2" not in str(exc.value)

    with pytest.raises(MessageError) as exc:
        m.decode_command(b'{"type":"set_credentials","psk":"hunter2hunter2"')  # broken JSON
    assert "hunter2" not in str(exc.value)
    # Raised "from None": no chained exception that could carry the raw input.
    assert exc.value.__cause__ is None and exc.value.__suppress_context__

    with pytest.raises(MessageError) as exc:
        m.decode_command(raw({"type": "scan", "hunter2hunter2": 1}))
    assert "hunter2" not in str(exc.value)
