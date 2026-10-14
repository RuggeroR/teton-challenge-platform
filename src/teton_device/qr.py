# Prints the setup code and a terminal QR (TETON:v1;id=...;code=...) with the
# 'for demo purposes the code is shown' note. See SPEC §3.
#
# This is the stand-in for the printed device label. The payload contains the setup code,
# so it goes to the screen only, never to the log (review Q7). Pure: returns text, the
# service decides where to print it.

import io

import qrcode

from teton_common import setup_code
from teton_common.messages import check_device_id
from teton_common.protocol import PROTOCOL_VERSION

DEMO_NOTE = (
    "For demo purposes the code is shown on screen; in production it is printed on the "
    "device label."
)


def qr_payload(device_id: str, code: str) -> str:
    """TETON:v1;id=<device_id>;code=<16 characters, no dashes> (review Q14)."""
    check_device_id(device_id)
    return f"TETON:v{PROTOCOL_VERSION};id={device_id};code={setup_code.normalize(code)}"


def _qr_text(payload: str) -> str:
    qr = qrcode.QRCode(border=1)  # 1 module of quiet zone is enough on a terminal
    qr.add_data(payload)
    out = io.StringIO()
    # Half blocks draw two QR rows per line. invert=True draws light modules with the
    # terminal's (light) text colour, so a phone sees dark-on-light on a dark terminal.
    qr.print_ascii(out=out, invert=True)
    # qrcode uses a no-break space for "both dark"; a plain space renders the same in every
    # terminal and font.
    return out.getvalue().replace("\xa0", " ")


def render(device_id: str, code: str) -> str:
    """The block printed on start and after every reset: ID, code, QR and the demo note."""
    return "\n".join(
        [
            f"Teton device {device_id}",
            f"Setup code: {setup_code.format_code(code)}",
            _qr_text(qr_payload(device_id, code)).rstrip("\n"),
            DEMO_NOTE,
        ]
    )
