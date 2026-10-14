# Tests for the setup code display: QR payload format, the rendered block and that the
# drawn QR really encodes the payload.

import pytest
import qrcode

from teton_common.messages import MessageError
from teton_common.setup_code import InvalidSetupCode
from teton_device.qr import DEMO_NOTE, qr_payload, render

DEVICE_ID = "TETON-3F9A2C"
CODE = "7K2MQX9D4HTRB8WZ"
PAYLOAD = "TETON:v1;id=TETON-3F9A2C;code=7K2MQX9D4HTRB8WZ"

# What print_ascii(invert=True) draws for (top module, bottom module); True = dark module.
_CELLS = {"█": (False, False), "▄": (True, False), "▀": (False, True), " ": (True, True)}


def qr_lines(text: str) -> list[str]:
    lines = text.splitlines()
    return lines[2:-1]  # between the code line and the note


def test_payload_format() -> None:
    assert qr_payload(DEVICE_ID, CODE) == PAYLOAD


@pytest.mark.parametrize("typed", ["7K2M-QX9D-4HTR-B8WZ", "7k2m qx9d 4htr b8wz"])
def test_payload_has_the_normalised_code_without_dashes(typed: str) -> None:
    assert qr_payload(DEVICE_ID, typed) == PAYLOAD  # review Q14


def test_payload_rejects_a_bad_code_without_echoing_it() -> None:
    with pytest.raises(InvalidSetupCode) as exc:
        qr_payload(DEVICE_ID, "7K2M-QX9D-4HTR")
    assert "7K2M" not in str(exc.value)


def test_payload_rejects_a_bad_device_id() -> None:
    with pytest.raises(MessageError):
        qr_payload("teton-3f9a2c", CODE)


def test_render_shows_id_formatted_code_and_note() -> None:
    lines = render(DEVICE_ID, CODE).splitlines()
    assert lines[0] == "Teton device TETON-3F9A2C"
    assert lines[1] == "Setup code: 7K2M-QX9D-4HTR-B8WZ"
    assert lines[-1] == DEMO_NOTE
    assert DEMO_NOTE == (
        "For demo purposes the code is shown on screen; in production it is printed on the "
        "device label."
    )  # exact text from SPEC §3


def test_qr_is_plain_text_without_escape_codes() -> None:
    lines = qr_lines(render(DEVICE_ID, CODE))
    assert lines
    assert len({len(line) for line in lines}) == 1  # a rectangle
    assert set("".join(lines)) <= set(_CELLS)  # works on a pipe or in a file, too


def test_drawn_qr_encodes_the_payload() -> None:
    # Read the half blocks back into modules and compare with a QR built from the expected
    # payload, so a phone scanning the screen gets exactly PAYLOAD.
    rows: list[list[bool]] = []
    for line in qr_lines(render(DEVICE_ID, CODE)):
        cells = [_CELLS[ch] for ch in line]
        rows.append([top for top, _ in cells])
        rows.append([bottom for _, bottom in cells])

    expected = qrcode.QRCode(border=1)
    expected.add_data(PAYLOAD)
    matrix = expected.get_matrix()
    assert rows[: len(matrix)] == matrix  # an odd row count leaves one padding row
