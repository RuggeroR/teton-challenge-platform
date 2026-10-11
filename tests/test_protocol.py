# Checks the protocol constants against the literal values in SPEC, so a typo in either
# place shows up here and not as a silent mismatch between device and configurator.

import json
import uuid

from teton_common import protocol as p


def test_uuids_are_valid_and_distinct() -> None:
    all_uuids = [p.SERVICE_UUID, *p.CHANNEL_UUIDS.values()]
    for value in all_uuids:
        assert str(uuid.UUID(value)) == value  # valid, lowercase, canonical form
    assert len(set(all_uuids)) == len(all_uuids)


def test_uuids_match_spec() -> None:
    assert p.SERVICE_UUID == "18388b82-f3e5-40bb-a9b9-fd0f9d789b62"
    assert p.CHANNEL_UUIDS == {
        "info": "a95e3738-82e8-4b9f-bdae-54b2ad44b843",
        "session": "3904babf-186b-4914-b120-559051a7400d",
        "command": "8f3ed082-9bcf-4ea0-8794-785bf52deb1f",
        "status": "7e81670a-c5d4-459d-8e6d-0925e5eaa9da",
    }


def test_every_channel_has_a_uuid() -> None:
    assert set(p.CHANNEL_UUIDS) == set(p.Channel)


def test_enum_values_match_spec() -> None:
    assert [c.value for c in p.Channel] == ["info", "session", "command", "status"]
    assert {s.value for s in p.Security} == {"open", "wpa2-psk", "wpa3-sae", "wpa2-eap-peap"}
    assert {r.value for r in p.FailureReason} == {
        "wrong_password",
        "ssid_not_found",
        "auth_failed",
        "no_ip",
        "captive_portal",
        "no_internet",
        "timeout",
    }
    assert {e.value for e in p.ErrorCode} == {"bad_request", "unsupported_security"}
    assert {w.value for w in p.StatusWarning} == {"server_not_verified", "limited_internet"}
    assert {i.value for i in p.InternetStatus} == {"full", "limited", "none", "unknown"}
    assert [s.value for s in p.DeviceState] == [
        "unprovisioned",
        "advertising",
        "session_established",
        "connecting",
        "connected",
        "provisioned",
    ]


def test_caps_match_spec_and_leave_out_open() -> None:
    assert list(p.CAPS) == ["wpa2-psk", "wpa3-sae", "wpa2-eap-peap"]
    assert set(p.CAPS) <= set(p.Security)
    assert p.Security.OPEN not in p.CAPS


def test_enums_serialise_as_plain_strings() -> None:
    message = {"state": p.DeviceState.ADVERTISING, "caps": list(p.CAPS)}
    assert json.dumps(message) == (
        '{"state": "advertising", "caps": ["wpa2-psk", "wpa3-sae", "wpa2-eap-peap"]}'
    )


def test_limits_match_spec() -> None:
    assert p.PROTOCOL_VERSION == 1
    assert p.PROTOCOL_LABEL == "teton-prov-v1"
    assert p.CHUNK_SIZE_BYTES == 20
    assert p.MAX_MESSAGE_BYTES == 8192
    assert p.MAX_INFO_BYTES == 180
    assert p.REASSEMBLY_TIMEOUT_S == 10
    assert p.SESSION_IDLE_TIMEOUT_S == 300
    assert p.CONNECT_TIMEOUT_S == 45
    assert (p.HANDSHAKE_MAX_FAILURES, p.HANDSHAKE_BACKOFF_INITIAL_S) == (5, 30)
    assert p.HANDSHAKE_BACKOFF_MAX_S == 600
