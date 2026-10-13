# structlog JSON configuration and the redaction processor that strips secrets
# (psk, password, setup_code, keys, ciphertext) from every log event. See SPEC §5.5, §11.
#
# Usage: call configure_logging() once at CLI start, then everywhere
#     log = structlog.get_logger(); log.info("scan_done", networks=12)
# Context that belongs on every line (device_id, session_id, state) is bound once with
# structlog.contextvars.bind_contextvars(...); each asyncio task keeps its own context.

import logging
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any, TextIO

import structlog

REDACTED = "[REDACTED]"

# Key names whose values never reach a log line, at any nesting depth (SPEC §5.5).
# `code` is deliberately absent: error codes such as "bad_request" must stay readable, and
# the setup code is only ever logged as `setup_code` (review Q7). The Enterprise username
# (`identity`) stays visible for debugging (review Q18).
REDACTED_KEYS = frozenset({"psk", "password", "setup_code", "confirm", "pub", "ct", "ca_cert_pem"})


def _redact(value: Any) -> Any:
    """Return a copy with the values of REDACTED_KEYS replaced; never modifies the input."""
    if isinstance(value, Mapping):
        return {k: REDACTED if k in REDACTED_KEYS else _redact(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact(v) for v in value]
    return value


def redact_secrets(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> dict[str, Any]:
    """structlog processor: redact secret fields anywhere in the event."""
    return _redact(event_dict)


def configure_logging(verbose: bool = False, stream: TextIO | None = None) -> None:
    """JSON lines to `stream` (stdout by default). INFO, or DEBUG when verbose."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,  # device_id, session_id, state
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.processors.format_exc_info,
            redact_secrets,  # last before rendering, so it also sees fields added above
            structlog.processors.JSONRenderer(),  # objects it can't serialise use repr()
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if verbose else logging.INFO
        ),
        logger_factory=structlog.PrintLoggerFactory(stream or sys.stdout),
        cache_logger_on_first_use=False,
    )
