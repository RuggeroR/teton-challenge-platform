# Protocol constants: version, GATT service and characteristic UUIDs, channel names,
# enums for Wi-Fi security types, device states and failure reasons. See SPEC §4, §6, §7.
#
# StrEnum members are their string value (Security.WPA2_PSK == "wpa2-psk"), so they go
# into JSON as plain strings with no conversion code.

from enum import StrEnum
from typing import Final

PROTOCOL_VERSION: Final = 1
# Mixed into the key derivation (SPEC §5.2) and the AES-GCM AAD (SPEC §5.3), so keys and
# ciphertexts from this protocol can never be confused with those of another one.
PROTOCOL_LABEL: Final = "teton-prov-v1"

DEVICE_ID_PREFIX: Final = "TETON-"  # + 6 uppercase hex characters (SPEC §3)
LOCAL_NAME_PREFIX: Final = "Teton-"  # + last 4 characters of the device ID (SPEC §3)


class Channel(StrEnum):
    """The four logical channels; over BLE each is one GATT characteristic (SPEC §4)."""

    INFO = "info"  # read, plaintext JSON, never secret
    SESSION = "session"  # write + notify, plaintext handshake
    COMMAND = "command"  # write, encrypted, configurator -> device
    STATUS = "status"  # notify, encrypted, device -> configurator


SERVICE_UUID: Final = "18388b82-f3e5-40bb-a9b9-fd0f9d789b62"

CHANNEL_UUIDS: Final[dict[Channel, str]] = {
    Channel.INFO: "a95e3738-82e8-4b9f-bdae-54b2ad44b843",
    Channel.SESSION: "3904babf-186b-4914-b120-559051a7400d",
    Channel.COMMAND: "8f3ed082-9bcf-4ea0-8794-785bf52deb1f",
    Channel.STATUS: "7e81670a-c5d4-459d-8e6d-0925e5eaa9da",
}


class Security(StrEnum):
    """Wi-Fi security types a configurator can ask for (SPEC §6.2)."""

    OPEN = "open"  # valid on the wire, but out of scope: unsupported_security (review Q11)
    WPA2_PSK = "wpa2-psk"
    WPA3_SAE = "wpa3-sae"
    WPA2_EAP_PEAP = "wpa2-eap-peap"


# What the device reports in `info.caps` (SPEC §6.1). A tuple keeps the JSON order stable.
CAPS: Final[tuple[Security, ...]] = (
    Security.WPA2_PSK,
    Security.WPA3_SAE,
    Security.WPA2_EAP_PEAP,
)


class DeviceState(StrEnum):
    """Device provisioning states (DESIGN §4), reported in `info.state`.

    No state for a failed attempt (it goes back to SESSION_ESTABLISHED) or for a handshake
    in progress (the device stays ADVERTISING; the session tracks it). Review Q13.
    """

    UNPROVISIONED = "unprovisioned"
    ADVERTISING = "advertising"
    SESSION_ESTABLISHED = "session_established"
    CONNECTING = "connecting"
    PROVISIONED = "provisioned"  # saved; advertising off, the device stays on (plan_v5 D2)


class StatusState(StrEnum):
    """`state` of a `{"type": "status"}` message (SPEC §6.3)."""

    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"


class FailureReason(StrEnum):
    """Why joining Wi-Fi failed (SPEC §7). Each maps to a plain-language message."""

    WRONG_PASSWORD = "wrong_password"
    SSID_NOT_FOUND = "ssid_not_found"
    AUTH_FAILED = "auth_failed"
    NO_IP = "no_ip"
    CAPTIVE_PORTAL = "captive_portal"
    NO_INTERNET = "no_internet"
    TIMEOUT = "timeout"


class ErrorCode(StrEnum):
    """Codes in `{"type": "error", "code": ...}` (SPEC §6.3)."""

    BAD_REQUEST = "bad_request"  # a decrypted command is invalid; the session stays (Q6)
    UNSUPPORTED_SECURITY = "unsupported_security"
    RATE_LIMITED = "rate_limited"  # too many handshakes; comes with retry_after_s (Q5)


class StatusWarning(StrEnum):
    """Entries of `warnings` in a `connected` status (SPEC §6.3)."""

    SERVER_NOT_VERIFIED = "server_not_verified"  # PEAP without CA certificate or domain
    LIMITED_INTERNET = "limited_internet"  # gateway reachable, internet not confirmed


class InternetStatus(StrEnum):
    """NetworkManager connectivity of the Wi-Fi interface, reported as `internet` (SPEC §6.3)."""

    FULL = "full"
    LIMITED = "limited"
    NONE = "none"
    UNKNOWN = "unknown"


# Sizes of handshake fields (SPEC §5.1) and of the envelope sequence number (SPEC §5.3).
PUBLIC_KEY_BYTES: Final = 32  # X25519 public key
HANDSHAKE_NONCE_BYTES: Final = 16
CONFIRM_BYTES: Final = 32  # HMAC-SHA256 tag
MAX_SEQ: Final = 2**64 - 1  # seq becomes the last 8 bytes of the AES-GCM nonce

# Limits. Units are part of each name.
CHUNK_SIZE_BYTES: Final = 20  # fixed, the minimum every BLE link supports (plan_v5 S2)
MAX_MESSAGE_BYTES: Final = 8192  # fits a CA certificate PEM (SPEC §4.1)
MAX_INFO_BYTES: Final = 180  # `info` must fit in a single GATT read (SPEC §4.1)
REASSEMBLY_TIMEOUT_S: Final = 10  # drop a partial message after this (SPEC §4.1)
SESSION_IDLE_TIMEOUT_S: Final = 300  # end a session without traffic (SPEC §5.4)
CONNECT_TIMEOUT_S: Final = 45  # Wi-Fi activation limit (SPEC §7, §8.3)
VERIFY_TIMEOUT_S: Final = 15  # IP, connectivity check and gateway ping, after activation (Q10)
# How long the configurator waits for connected/failed after `connecting`: both limits above
# plus 10 s for BLE and the profile setup (review Q10).
CONFIGURATOR_RESULT_WAIT_S: Final = 70
HANDSHAKE_MAX_FAILURES: Final = 5  # consecutive failures before backing off (SPEC §5.4)
HANDSHAKE_BACKOFF_INITIAL_S: Final = 30
HANDSHAKE_BACKOFF_MAX_S: Final = 600
