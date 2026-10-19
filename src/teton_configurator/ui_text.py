# Plain-language strings shown to the technician, incl. failure reason -> message + next action.
# See SPEC §7, §9.2.
#
# Every text the technician sees lives here, nowhere else (CLAUDE.md). A failure is one
# sentence saying what happened plus a next action. Exception texts are never shown: the
# flow catches typed errors and picks a text from this module (review Q28).
# Step 3.3 adds the prompts of the options it decides (menus, rescan, hidden network; Q25).

from teton_common.messages import NetworkInfo
from teton_common.protocol import (
    ErrorCode,
    FailureReason,
    InternetStatus,
    Security,
    StatusWarning,
)

# ---------------------------------------------------------------------------
# The steps of SPEC §9.2

PRESS_PAIRING_BUTTON = "Press the pairing button on the device. Its light starts blinking blue."
LOOKING_FOR_DEVICES = "Looking for devices..."
NO_DEVICE_FOUND = "No device found. Press the pairing button on the device and try again."
BLUETOOTH_UNAVAILABLE = (  # review Q99
    "Bluetooth is off or missing on this computer. Turn it on and run the configurator again."
)
ASK_SETUP_CODE = "Setup code (printed on the device label): "
CHECKING_CODE = "Checking the setup code..."
CODE_ACCEPTED = "Setup code accepted."
LOOKING_FOR_NETWORKS = "The device is looking for Wi-Fi networks..."
OTHER_NETWORK = "Other (hidden) network"
ASK_NETWORK_NAME = "Network name: "
ASK_PASSWORD = "Wi-Fi password: "
ASK_USERNAME = "Username: "
ASK_CA_FILE = "Certificate file from IT (optional, press Enter to skip): "
ASK_DOMAIN = "Server domain from IT (optional, press Enter to skip): "
STOPPED = "Setup stopped. Run the configurator again to start over."

# ---------------------------------------------------------------------------
# Menus of the guided flow (reviews Q39-Q46). Menus accept a number, or q to quit.

ASK_LOOK_AGAIN = "Press Enter to look again, or type q to quit: "
ASK_DEVICE = "Device number (q to quit): "
ASK_NETWORK = "Network number (q to quit): "
ASK_SECURITY = "Security type number (q to quit): "
ASK_NEXT = "What next? Number (q to quit): "
SCAN_AGAIN = "Scan again"
NO_NETWORKS = (
    "The device found no networks it can join. Scan again, or pick the hidden network option."
)
INVALID_CHOICE = "That is not one of the numbers shown. Type a number from the list, or q to quit."
RETRY_PASSWORD = "Try again (retype the password)"
RETRY_LOGIN = "Try again (retype the username and password)"
PICK_ANOTHER_NETWORK = "Pick another network"
QUIT = "Quit"


def connecting_text(ssid: str) -> str:
    return f"Connecting the device to {ssid}. This can take up to a minute..."


def connected_text(ip: str, internet: InternetStatus) -> str:
    """SPEC §9.2: "Connected. IP 192.168.1.23, internet OK". Anything less than `full` comes
    with the limited_internet warning, which explains it."""
    if internet == InternetStatus.FULL:
        return f"Connected. IP {ip}, internet OK."
    return f"Connected. IP {ip}, internet not confirmed."


# ---------------------------------------------------------------------------
# Failures reported by the device (SPEC §7)

_FAILURES: dict[FailureReason, str] = {
    FailureReason.WRONG_PASSWORD: "The Wi-Fi password was not accepted. Check it and try again.",
    FailureReason.SSID_NOT_FOUND: (
        "The device can't see this network. Move closer or check the name."
    ),
    FailureReason.AUTH_FAILED: (
        "The username or password was rejected by the network. Check them and try again."
    ),
    FailureReason.NO_IP: "Connected to Wi-Fi but the network gave no address. Contact IT.",
    FailureReason.CAPTIVE_PORTAL: (
        "This network needs a login page; devices can't use it. Contact IT."
    ),
    FailureReason.NO_INTERNET: "Connected, but there is no internet access. Contact IT.",
    FailureReason.TIMEOUT: "Connecting took too long. Try again.",
}


def failure_text(reason: FailureReason) -> str:
    return _FAILURES[reason]


# ---------------------------------------------------------------------------
# Error codes from the device (SPEC §6.3)

_ERRORS: dict[ErrorCode, str] = {
    # The configurator checks input before sending (Q28), so this only happens on a bug.
    ErrorCode.BAD_REQUEST: (
        "The device refused the request. Start again, and if it happens again contact support."
    ),
    ErrorCode.UNSUPPORTED_SECURITY: (
        "The device can't join this type of network. Pick another network or contact IT."
    ),
    ErrorCode.RATE_LIMITED: (
        "Too many attempts with a wrong setup code. Wait {seconds}, then type the code again."
    ),
}


def error_text(code: ErrorCode, retry_after_s: int | None = None) -> str:
    """`retry_after_s` comes with rate_limited only (review Q5)."""
    return _ERRORS[code].format(seconds=_seconds(retry_after_s or 0))


def _seconds(n: int) -> str:
    return "1 second" if n == 1 else f"{n} seconds"


# ---------------------------------------------------------------------------
# Warnings on a successful connection (SPEC §6.3)

_WARNINGS: dict[StatusWarning, str] = {
    StatusWarning.SERVER_NOT_VERIFIED: (
        "Warning: the device couldn't check that this is the real network, because no "
        "certificate file or server domain was given. Ask IT for them to make the setup safer."
    ),
    StatusWarning.LIMITED_INTERNET: (
        "Warning: the device joined the network but can't reach the internet. This may be "
        "normal on restricted networks; contact IT if the device stays offline."
    ),
}


def warning_text(warning: StatusWarning) -> str:
    return _WARNINGS[warning]


# ---------------------------------------------------------------------------
# Failures the configurator finds itself (review Q27)

WRONG_CODE = (
    "The setup code is wrong, or this is not the right device. "
    "Check the code on the device label and type it again."
)
# Q22: a hello outside the pairing window gets no reply at all.
NO_REPLY = "The device didn't answer. Press the pairing button on the device and try again."
# Not the `timeout` text: the device always finishes its attempt and may have connected.
RESULT_TIMEOUT = (
    "The device didn't report back in time. If its light is green it is connected; "
    "otherwise press the pairing button on the device and start again."
)
CONNECTION_LOST = (
    "The connection to the device was lost. "
    "Move closer, press the pairing button on the device and start again."
)
UNEXPECTED_REPLY = (
    "The device answered in a way this configurator doesn't understand. "
    "Update the configurator and start again."
)

# ---------------------------------------------------------------------------
# Input checks before anything is sent (review Q28)

INVALID_CODE = (
    "A setup code has 16 letters and digits, like 7K2M-QX9D-4HTR-B8WZ. "
    "Check the label and type it again."
)
INVALID_PASSWORD_LENGTH = "A Wi-Fi password has 8 to 63 characters. Check it and type it again."
# The limit is 32 bytes; "characters" is right for the plain-ASCII names most networks use.
INVALID_NETWORK_NAME = "A network name has 1 to 32 characters. Check it and type it again."
REQUIRED = "This can't be empty. Type it again."
CA_FILE_UNREADABLE = (
    "This file can't be opened. Check the path and type it again, or press Enter to skip."
)
CA_FILE_TOO_LARGE = (
    "This file is too large to be a certificate. "
    "Ask IT for the certificate file and type its path again."
)
CA_FILE_NOT_CERTIFICATE = (
    "This file is not a certificate. Ask IT for the certificate file and type its path again."
)

# ---------------------------------------------------------------------------
# The phone page (web/, plan_v5 D3). tests/web_data.py copies every text the page uses into
# web/index.html, so this file stays the only place for texts (review Q105).

PAGE_UNSUPPORTED = (
    "This browser can't set up the device. Open this page in Chrome on an Android phone."
)
PAGE_FIND_DEVICE = "Find the device"
PAGE_CONNECTING = "Connecting to the device..."
PAGE_CHECK_CODE = "Check the code"
PAGE_CONNECT = "Connect"
PAGE_START_OVER = "Start over"
PAGE_STOPPED = "Setup stopped. Tap Start over to begin again."
PAGE_NO_NETWORKS = "The device found no networks it can join. Move it closer and scan again."
PAGE_ENTERPRISE = (
    "This page can't join enterprise networks. Pick another network, or use the laptop "
    "configurator."
)

# ---------------------------------------------------------------------------
# The network list (review Q30)

_BARS = "▂▄▆█"

_SECURITY_LABELS: dict[Security, str] = {
    Security.WPA2_PSK: "password",
    Security.WPA3_SAE: "password",
    Security.WPA2_EAP_PEAP: "enterprise",
}  # one per entry of CAPS; open is never in a scan list (review Q11)

# The hidden-network choice, in router-label wording (review Q42).
_HIDDEN_SECURITY_LABELS: dict[Security, str] = {
    Security.WPA2_PSK: "WPA2/WPA3 Personal (password)",
    Security.WPA3_SAE: "WPA3 Personal only (password)",
    Security.WPA2_EAP_PEAP: "WPA2 Enterprise (username and password)",
}


def signal_bars(signal: int) -> str:
    """0-100 -> 1 to 4 bars, padded to the same width so the list lines up."""
    count = 4 if signal >= 75 else 3 if signal >= 50 else 2 if signal >= 25 else 1
    return _BARS[:count].ljust(len(_BARS))


def network_label(net: NetworkInfo, width: int = 0) -> str:
    """e.g. "Home-PSK  ▂▄▆█  password". `width` pads the name so a list lines up."""
    return f"{net.ssid.ljust(width)}  {signal_bars(net.signal)}  {_SECURITY_LABELS[net.security]}"


def device_label(name: str, signal: int) -> str:
    """e.g. "Teton-9A2C  ▂▄▆█"."""
    return f"{name}  {signal_bars(signal)}"


def hidden_security_label(security: Security) -> str:
    return _HIDDEN_SECURITY_LABELS[security]


def menu_line(number: int, text: str) -> str:
    return f"  {number}) {text}"
