# Tests for setup code generation, formatting, normalisation and validation.

import re

import pytest

from teton_common import setup_code as sc
from teton_common.setup_code import InvalidSetupCode


def test_alphabet_is_crockford_base32() -> None:
    assert len(sc.ALPHABET) == 32
    assert len(set(sc.ALPHABET)) == 32
    assert not set("ILOU") & set(sc.ALPHABET)


def test_generated_codes_have_the_right_shape() -> None:
    for _ in range(200):
        code = sc.generate()
        assert len(code) == 16
        assert set(code) <= set(sc.ALPHABET)


def test_generated_codes_are_different() -> None:
    codes = {sc.generate() for _ in range(1000)}
    assert len(codes) == 1000


def test_format_groups_of_four() -> None:
    assert sc.format_code("7K2MQX9D4HTRB8WZ") == "7K2M-QX9D-4HTR-B8WZ"
    assert re.fullmatch(r"([0-9A-Z]{4}-){3}[0-9A-Z]{4}", sc.format_code(sc.generate()))


def test_format_then_normalize_round_trip() -> None:
    code = sc.generate()
    assert sc.normalize(sc.format_code(code)) == code


@pytest.mark.parametrize(
    "typed",
    [
        "7K2MQX9D4HTRB8WZ",
        "7k2m-qx9d-4htr-b8wz",
        " 7K2M QX9D 4HTR B8WZ ",
        "7K2M-QX9D\t4HTR-B8WZ\n",
    ],
)
def test_normalize_accepts_common_typing_variants(typed: str) -> None:
    assert sc.normalize(typed) == "7K2MQX9D4HTRB8WZ"


def test_normalize_maps_look_alike_letters() -> None:
    # O -> 0, I -> 1, L -> 1 (in either case)
    assert sc.normalize("OOOO-IIII-LLLL-oil0") == "0000111111110110"


@pytest.mark.parametrize(
    "typed",
    [
        "",
        "7K2M-QX9D-4HTR-B8W",  # 15 characters
        "7K2M-QX9D-4HTR-B8WZZ",  # 17 characters
        "7K2M-QX9D-4HTR-B8WU",  # U is not in the alphabet
        "7K2M-QX9D-4HTR-B8W*",  # symbol
        "7K2M_QX9D_4HTR_B8WZ",  # underscores are not separators
    ],
)
def test_normalize_rejects_invalid_input(typed: str) -> None:
    with pytest.raises(InvalidSetupCode):
        sc.normalize(typed)


@pytest.mark.parametrize("typed", ["7K2M-QX9D-4HTR-B8W", "7K2M-QX9D-4HTR-B8WU"])
def test_error_message_never_contains_the_input(typed: str) -> None:
    with pytest.raises(InvalidSetupCode) as exc:
        sc.normalize(typed)
    message = str(exc.value)
    assert typed not in message
    assert typed.replace("-", "") not in message
    assert "7K2M" not in message


def test_format_rejects_invalid_code() -> None:
    with pytest.raises(InvalidSetupCode):
        sc.format_code("too-short")
