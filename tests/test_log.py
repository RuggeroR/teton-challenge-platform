# Tests for the JSON log configuration and the secret redaction processor.

import io
import json
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from teton_common.log import REDACTED, REDACTED_KEYS, configure_logging
from teton_common.messages import SetCredentials
from teton_common.protocol import Security

SECRET = "correct-horse-battery"


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    out = io.StringIO()
    configure_logging(verbose=False, stream=out)
    yield out
    structlog.contextvars.clear_contextvars()
    structlog.reset_defaults()


def lines(out: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_spec_keys_are_in_the_list() -> None:
    assert REDACTED_KEYS == {"psk", "password", "setup_code", "confirm", "pub", "ct", "ca_cert_pem"}
    assert "code" not in REDACTED_KEYS  # review Q7
    assert "identity" not in REDACTED_KEYS  # review Q18: the username stays visible


@pytest.mark.parametrize("key", sorted(REDACTED_KEYS))
def test_each_key_is_redacted_at_any_depth(stream: io.StringIO, key: str) -> None:
    log = structlog.get_logger()
    log.info("top", **{key: SECRET})
    log.info("nested", outer={"inner": {key: SECRET}})
    log.info("in_list", items=[{key: SECRET}, {"other": 1}])
    assert SECRET not in stream.getvalue()
    top, nested, in_list = lines(stream)
    assert top[key] == REDACTED
    assert nested["outer"]["inner"][key] == REDACTED
    assert in_list["items"] == [{key: REDACTED}, {"other": 1}]


def test_non_secret_fields_stay_visible(stream: io.StringIO) -> None:
    structlog.get_logger().warning(
        "handshake_failed", code="bad_request", setup_code="7K2MQX9D4HTRB8WZ", ssid="Home-PSK"
    )
    (line,) = lines(stream)
    assert line["code"] == "bad_request"  # error codes stay readable
    assert line["setup_code"] == REDACTED
    assert line["ssid"] == "Home-PSK"


def test_caller_data_is_not_modified(stream: io.StringIO) -> None:
    creds = {"ssid": "Home-PSK", "psk": SECRET}
    structlog.get_logger().info("apply", creds=creds)
    assert creds["psk"] == SECRET


def test_lines_are_json_with_level_timestamp_and_context(stream: io.StringIO) -> None:
    structlog.contextvars.bind_contextvars(device_id="TETON-3F9A2C", session_id="s1")
    structlog.get_logger().info("scan_done", networks=12)
    (line,) = lines(stream)
    assert line["event"] == "scan_done" and line["networks"] == 12
    assert line["level"] == "info"
    assert line["ts"].endswith("Z")  # ISO-8601, UTC
    assert line["device_id"] == "TETON-3F9A2C" and line["session_id"] == "s1"


def test_debug_only_when_verbose(stream: io.StringIO) -> None:
    log = structlog.get_logger()
    log.debug("hidden")
    assert stream.getvalue() == ""
    configure_logging(verbose=True, stream=stream)
    log.debug("shown")
    assert [line["event"] for line in lines(stream)] == ["shown"]


def test_message_objects_are_logged_without_secrets(stream: io.StringIO) -> None:
    creds = SetCredentials(ssid="Home-PSK", security=Security.WPA2_PSK, psk=SECRET)
    structlog.get_logger().info("received", msg=creds)
    assert SECRET not in stream.getvalue()
    assert "Home-PSK" in stream.getvalue()


def test_exceptions_are_rendered_as_text(stream: io.StringIO) -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        structlog.get_logger().exception("failed")
    (line,) = lines(stream)
    assert "ValueError: boom" in line["exception"]
