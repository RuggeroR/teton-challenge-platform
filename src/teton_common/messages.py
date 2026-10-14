# Message dataclasses (hello, hello_reply, confirm, scan, set_credentials, scan_result,
# status, error, ready) with JSON encode/decode and field validation. See SPEC §5.1, §6.
#
# Two layers of checks:
#   - from_dict() checks the JSON shape: required/unknown fields and value types.
#   - __post_init__() checks the rules (lengths, ranges, which fields go together), so an
#     invalid message can't be built either, whether it came off the wire or from our code.
# Error texts name the field, never its value, so a secret can't leak through an exception.

import base64
import binascii
import ipaddress
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Self

from teton_common.protocol import (
    CAPS,
    CONFIRM_BYTES,
    DEVICE_ID_PREFIX,
    HANDSHAKE_NONCE_BYTES,
    LOCAL_NAME_PREFIX,
    MAX_INFO_BYTES,
    MAX_SEQ,
    PROTOCOL_VERSION,
    PUBLIC_KEY_BYTES,
    DeviceState,
    ErrorCode,
    FailureReason,
    InternetStatus,
    Security,
    StatusState,
    StatusWarning,
)

MAX_SSID_BYTES = 32  # Wi-Fi limits the SSID in bytes, not characters
PSK_MIN_CHARS, PSK_MAX_CHARS = 8, 63  # WPA passphrase length (SPEC §6.2)
MAX_SCAN_NETWORKS = 30
_DEVICE_ID_RE = re.compile(re.escape(DEVICE_ID_PREFIX) + r"[0-9A-F]{6}")


class MessageError(ValueError):
    """A message is malformed. The text names the field, never its value."""


# ---------------------------------------------------------------------------
# Small helpers: type checks for from_dict() and rule checks for __post_init__().


def _fields(d: dict[str, Any], required: tuple[str, ...], optional: tuple[str, ...] = ()) -> None:
    for key in required:
        if key not in d:
            raise MessageError(f"missing field: {key}")
    for key in d:
        if key not in required and key not in optional:
            raise MessageError("unexpected field")  # the name comes from the peer: not echoed


def _as_str(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise MessageError(f"{name} must be a string")
    return value


def _as_int(value: Any, name: str) -> int:
    # bool is a subclass of int in Python (True == 1), so reject it explicitly.
    if not isinstance(value, int) or isinstance(value, bool):
        raise MessageError(f"{name} must be an integer")
    return value


def _as_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise MessageError(f"{name} must be true or false")
    return value


def _as_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise MessageError(f"{name} must be a list")
    return value


def _as_dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MessageError(f"{name} must be an object")
    return value


def _as_bytes(value: Any, name: str) -> bytes:
    try:
        return base64.b64decode(_as_str(value, name), validate=True)
    except binascii.Error:
        raise MessageError(f"{name} is not valid base64") from None


def _as_enum[E: StrEnum](value: Any, enum_cls: type[E], name: str) -> E:
    try:
        return enum_cls(_as_str(value, name))
    except ValueError:
        raise MessageError(f"{name} has an unknown value") from None


def _check_version(value: Any) -> None:
    if _as_int(value, "v") != PROTOCOL_VERSION:
        raise MessageError("unsupported protocol version")


def _check_len(value: bytes, length: int, name: str) -> None:
    if len(value) != length:
        raise MessageError(f"{name} must be {length} bytes")


def _check_ssid(ssid: str) -> None:
    if not 1 <= len(ssid.encode("utf-8")) <= MAX_SSID_BYTES:
        raise MessageError(f"ssid must be 1-{MAX_SSID_BYTES} bytes of UTF-8")


def check_device_id(device_id: str) -> None:
    if not _DEVICE_ID_RE.fullmatch(device_id):
        raise MessageError(f"device_id must be {DEVICE_ID_PREFIX} + 6 uppercase hex characters")


def _check_non_empty(value: str | None, name: str) -> None:
    if value is not None and not value:
        raise MessageError(f"{name} must not be empty")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------------------
# Handshake messages, plaintext on the `session` channel (SPEC §5.1).


@dataclass(frozen=True)
class Hello:
    TYPE: ClassVar[str] = "hello"
    pub: bytes
    nonce: bytes

    def __post_init__(self) -> None:
        _check_len(self.pub, PUBLIC_KEY_BYTES, "pub")
        _check_len(self.nonce, HANDSHAKE_NONCE_BYTES, "nonce")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.TYPE,
            "v": PROTOCOL_VERSION,
            "pub": _b64(self.pub),
            "nonce": _b64(self.nonce),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        _fields(d, ("v", "pub", "nonce"))
        _check_version(d["v"])
        return cls(pub=_as_bytes(d["pub"], "pub"), nonce=_as_bytes(d["nonce"], "nonce"))


@dataclass(frozen=True)
class HelloReply:
    TYPE: ClassVar[str] = "hello_reply"
    device_id: str
    pub: bytes
    nonce: bytes
    confirm: bytes

    def __post_init__(self) -> None:
        check_device_id(self.device_id)
        _check_len(self.pub, PUBLIC_KEY_BYTES, "pub")
        _check_len(self.nonce, HANDSHAKE_NONCE_BYTES, "nonce")
        _check_len(self.confirm, CONFIRM_BYTES, "confirm")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.TYPE,
            "v": PROTOCOL_VERSION,
            "device_id": self.device_id,
            "pub": _b64(self.pub),
            "nonce": _b64(self.nonce),
            "confirm": _b64(self.confirm),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        _fields(d, ("v", "device_id", "pub", "nonce", "confirm"))
        _check_version(d["v"])
        return cls(
            device_id=_as_str(d["device_id"], "device_id"),
            pub=_as_bytes(d["pub"], "pub"),
            nonce=_as_bytes(d["nonce"], "nonce"),
            confirm=_as_bytes(d["confirm"], "confirm"),
        )


@dataclass(frozen=True)
class Confirm:
    TYPE: ClassVar[str] = "confirm"
    confirm: bytes

    def __post_init__(self) -> None:
        _check_len(self.confirm, CONFIRM_BYTES, "confirm")

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.TYPE, "confirm": _b64(self.confirm)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        _fields(d, ("confirm",))
        return cls(confirm=_as_bytes(d["confirm"], "confirm"))


@dataclass(frozen=True)
class HandshakeFailed:
    TYPE: ClassVar[str] = "handshake_failed"

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.TYPE}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        _fields(d, ())
        return cls()


@dataclass(frozen=True)
class Error:
    """Sent on `session` (plaintext) or inside a `status` envelope (SPEC §6.3)."""

    TYPE: ClassVar[str] = "error"
    code: ErrorCode
    retry_after_s: int | None = None  # with rate_limited only: seconds until a new hello (Q5)

    def __post_init__(self) -> None:
        if (self.code == ErrorCode.RATE_LIMITED) != (self.retry_after_s is not None):
            raise MessageError("retry_after_s goes with code rate_limited, and only with it")
        if self.retry_after_s is not None and self.retry_after_s < 1:
            raise MessageError("retry_after_s must be at least 1")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.TYPE, "code": self.code}
        if self.retry_after_s is not None:
            d["retry_after_s"] = self.retry_after_s
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        _fields(d, ("code",), ("retry_after_s",))
        return cls(
            code=_as_enum(d["code"], ErrorCode, "code"),
            retry_after_s=_as_int(d["retry_after_s"], "retry_after_s")
            if "retry_after_s" in d
            else None,
        )


# ---------------------------------------------------------------------------
# Outer layer of the `command` and `status` channels (SPEC §5.3). No `type` field: the
# message type is inside the ciphertext.


@dataclass(frozen=True)
class Envelope:
    seq: int
    ct: bytes = field(repr=False)  # ciphertext never shows up in logs or tracebacks

    def __post_init__(self) -> None:
        if not 0 <= self.seq <= MAX_SEQ:
            raise MessageError("seq is out of range")

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "ct": _b64(self.ct)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        _fields(d, ("seq", "ct"))
        return cls(seq=_as_int(d["seq"], "seq"), ct=_as_bytes(d["ct"], "ct"))


# ---------------------------------------------------------------------------
# The `info` characteristic, a plain read with no `type` field (SPEC §6.1).


@dataclass(frozen=True)
class Info:
    device_id: str
    state: DeviceState
    caps: tuple[Security, ...] = CAPS

    @property
    def name(self) -> str:
        """BLE local name: prefix + last 4 characters of the device ID (SPEC §3)."""
        return LOCAL_NAME_PREFIX + self.device_id[-4:]

    def __post_init__(self) -> None:
        check_device_id(self.device_id)
        # Plan_v5 S2: `info` must fit in one GATT read.
        if len(encode(self)) > MAX_INFO_BYTES:
            raise MessageError(f"info must encode to at most {MAX_INFO_BYTES} bytes")

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": PROTOCOL_VERSION,
            "device_id": self.device_id,
            "name": self.name,
            "state": self.state,
            "caps": list(self.caps),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        _fields(d, ("v", "device_id", "name", "state", "caps"))
        _check_version(d["v"])
        info = cls(
            device_id=_as_str(d["device_id"], "device_id"),
            state=_as_enum(d["state"], DeviceState, "state"),
            caps=tuple(_as_enum(c, Security, "caps") for c in _as_list(d["caps"], "caps")),
        )
        if d["name"] != info.name:
            raise MessageError("name does not match device_id")
        return info


# ---------------------------------------------------------------------------
# Commands, configurator -> device, inside a `command` envelope (SPEC §6.2).


@dataclass(frozen=True)
class Scan:
    TYPE: ClassVar[str] = "scan"

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.TYPE}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        _fields(d, ())
        return cls()


@dataclass(frozen=True)
class EapCredentials:
    """WPA2-Enterprise PEAP login (SPEC §6.2). CA certificate and domain are optional."""

    identity: str
    password: str = field(repr=False)
    anonymous_identity: str | None = None
    ca_cert_pem: str | None = field(default=None, repr=False)
    domain_suffix_match: str | None = None

    _OPTIONAL: ClassVar[tuple[str, ...]] = (
        "anonymous_identity",
        "ca_cert_pem",
        "domain_suffix_match",
    )

    def __post_init__(self) -> None:
        _check_non_empty(self.identity, "identity")
        _check_non_empty(self.password, "password")
        for name in self._OPTIONAL:
            _check_non_empty(getattr(self, name), name)

    def to_dict(self) -> dict[str, Any]:
        d = {"identity": self.identity, "password": self.password}
        for name in self._OPTIONAL:
            if getattr(self, name) is not None:
                d[name] = getattr(self, name)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        _fields(d, ("identity", "password"), cls._OPTIONAL)
        optional = {name: _as_str(d[name], name) for name in cls._OPTIONAL if name in d}
        return cls(
            identity=_as_str(d["identity"], "identity"),
            password=_as_str(d["password"], "password"),
            **optional,
        )


@dataclass(frozen=True)
class SetCredentials:
    TYPE: ClassVar[str] = "set_credentials"
    ssid: str
    security: Security
    hidden: bool = False
    psk: str | None = field(default=None, repr=False)  # PSK/SAE passphrase
    eap: EapCredentials | None = None  # PEAP login

    def __post_init__(self) -> None:
        _check_ssid(self.ssid)
        if self.security in (Security.WPA2_PSK, Security.WPA3_SAE):
            if self.psk is None or not PSK_MIN_CHARS <= len(self.psk) <= PSK_MAX_CHARS:
                raise MessageError(f"psk must be {PSK_MIN_CHARS}-{PSK_MAX_CHARS} characters")
        elif self.psk is not None:
            raise MessageError("psk is only allowed with wpa2-psk or wpa3-sae")
        if self.security == Security.WPA2_EAP_PEAP:
            if self.eap is None:
                raise MessageError("eap is required with wpa2-eap-peap")
        elif self.eap is not None:
            raise MessageError("eap is only allowed with wpa2-eap-peap")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "type": self.TYPE,
            "ssid": self.ssid,
            "hidden": self.hidden,
            "security": self.security,
        }
        if self.psk is not None:
            d["psk"] = self.psk
        if self.eap is not None:
            d["eap"] = self.eap.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        _fields(d, ("ssid", "hidden", "security"), ("psk", "eap"))
        return cls(
            ssid=_as_str(d["ssid"], "ssid"),
            hidden=_as_bool(d["hidden"], "hidden"),
            security=_as_enum(d["security"], Security, "security"),
            psk=_as_str(d["psk"], "psk") if "psk" in d else None,
            eap=EapCredentials.from_dict(_as_dict(d["eap"], "eap")) if "eap" in d else None,
        )


# ---------------------------------------------------------------------------
# Device -> configurator, inside a `status` envelope (SPEC §6.3).


@dataclass(frozen=True)
class Ready:
    TYPE: ClassVar[str] = "ready"

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.TYPE}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        _fields(d, ())
        return cls()


@dataclass(frozen=True)
class NetworkInfo:
    """One network in a scan result."""

    ssid: str
    signal: int  # 0-100, as nmcli reports it
    security: Security

    def __post_init__(self) -> None:
        _check_ssid(self.ssid)
        if not 0 <= self.signal <= 100:
            raise MessageError("signal must be 0-100")

    def to_dict(self) -> dict[str, Any]:
        return {"ssid": self.ssid, "signal": self.signal, "security": self.security}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        _fields(d, ("ssid", "signal", "security"))
        return cls(
            ssid=_as_str(d["ssid"], "ssid"),
            signal=_as_int(d["signal"], "signal"),
            security=_as_enum(d["security"], Security, "security"),
        )


@dataclass(frozen=True)
class ScanResult:
    TYPE: ClassVar[str] = "scan_result"
    networks: tuple[NetworkInfo, ...]

    def __post_init__(self) -> None:
        if len(self.networks) > MAX_SCAN_NETWORKS:
            raise MessageError(f"at most {MAX_SCAN_NETWORKS} networks")

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.TYPE, "networks": [n.to_dict() for n in self.networks]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        _fields(d, ("networks",))
        items = _as_list(d["networks"], "networks")
        return cls(networks=tuple(NetworkInfo.from_dict(_as_dict(n, "network")) for n in items))


@dataclass(frozen=True)
class Status:
    """Progress of a Wi-Fi attempt. Which fields are set depends on `state`:

    connecting: none; connected: ip, internet, warnings; failed: reason.
    """

    TYPE: ClassVar[str] = "status"
    state: StatusState
    ip: str | None = None
    internet: InternetStatus | None = None
    warnings: tuple[StatusWarning, ...] = ()
    reason: FailureReason | None = None

    def __post_init__(self) -> None:
        connected = self.state == StatusState.CONNECTED
        failed = self.state == StatusState.FAILED
        if connected != (self.ip is not None) or connected != (self.internet is not None):
            raise MessageError("ip and internet go with state connected, and only with it")
        if self.warnings and not connected:
            raise MessageError("warnings go with state connected only")
        if failed != (self.reason is not None):
            raise MessageError("reason goes with state failed, and only with it")
        if self.ip is not None:
            try:
                ipaddress.IPv4Address(self.ip)
            except ValueError:
                raise MessageError("ip must be an IPv4 address") from None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.TYPE, "state": self.state}
        if self.state == StatusState.CONNECTED:
            d |= {"ip": self.ip, "internet": self.internet, "warnings": list(self.warnings)}
        if self.reason is not None:
            d["reason"] = self.reason
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Self:
        _fields(d, ("state",), ("ip", "internet", "warnings", "reason"))
        warnings = _as_list(d.get("warnings", []), "warnings")
        return cls(
            state=_as_enum(d["state"], StatusState, "state"),
            ip=_as_str(d["ip"], "ip") if "ip" in d else None,
            internet=_as_enum(d["internet"], InternetStatus, "internet")
            if "internet" in d
            else None,
            warnings=tuple(_as_enum(w, StatusWarning, "warnings") for w in warnings),
            reason=_as_enum(d["reason"], FailureReason, "reason") if "reason" in d else None,
        )


# ---------------------------------------------------------------------------
# Encoding and per-channel decoding. Each channel accepts only its own message types.

SessionMessage = Hello | HelloReply | Confirm | HandshakeFailed | Error
CommandMessage = Scan | SetCredentials
StatusMessage = Ready | ScanResult | Status | Error
Message = SessionMessage | CommandMessage | StatusMessage | Envelope | Info

_SESSION_TYPES = {m.TYPE: m for m in (Hello, HelloReply, Confirm, HandshakeFailed, Error)}
_COMMAND_TYPES = {m.TYPE: m for m in (Scan, SetCredentials)}
_STATUS_TYPES = {m.TYPE: m for m in (Ready, ScanResult, Status, Error)}


def encode(msg: Message) -> bytes:
    """Compact UTF-8 JSON. Non-ASCII stays as UTF-8, so SSIDs keep their byte size."""
    return json.dumps(msg.to_dict(), separators=(",", ":"), ensure_ascii=False).encode()


def _load(data: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # "from None": the original exception would carry the raw input, which may be secret.
        raise MessageError("message is not valid UTF-8 JSON") from None
    return _as_dict(obj, "message")


def _decode_typed(data: bytes, allowed: dict[str, Any]) -> Any:
    d = _load(data)
    msg_type = d.pop("type", None)
    if not isinstance(msg_type, str) or msg_type not in allowed:
        raise MessageError("message type is not allowed on this channel")
    return allowed[msg_type].from_dict(d)


def decode_session(data: bytes) -> SessionMessage:
    return _decode_typed(data, _SESSION_TYPES)


def decode_command(plaintext: bytes) -> CommandMessage:
    return _decode_typed(plaintext, _COMMAND_TYPES)


def decode_status(plaintext: bytes) -> StatusMessage:
    return _decode_typed(plaintext, _STATUS_TYPES)


def decode_envelope(data: bytes) -> Envelope:
    return Envelope.from_dict(_load(data))


def decode_info(data: bytes) -> Info:
    return Info.from_dict(_load(data))
