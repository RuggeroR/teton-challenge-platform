# Setup code: generate 80-bit Crockford base32 codes, format as XXXX-XXXX-XXXX-XXXX,
# normalise user input, validate. See SPEC §3.
#
# Both sides must normalise identically: the normalised code is the HKDF salt (SPEC §5.2),
# so any difference would make a correctly typed code fail the handshake.

import secrets

# Crockford base32: no I, L, O (look like 1 and 0) and no U. 32 symbols = 5 bits each.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LENGTH = 16  # 16 x 5 bits = 80 bits
GROUP_SIZE = 4

# Letters a technician may type by mistake, mapped to the digit they look like.
_LOOK_ALIKES = str.maketrans({"O": "0", "I": "1", "L": "1"})


class InvalidSetupCode(ValueError):
    """The input is not a valid setup code. The message never contains the input."""


def generate() -> str:
    """Return a new random code: 16 symbols from the OS cryptographic random source."""
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def format_code(code: str) -> str:
    """Return the display form, e.g. 7K2M-QX9D-4HTR-B8WZ."""
    code = normalize(code)
    groups = [code[i : i + GROUP_SIZE] for i in range(0, CODE_LENGTH, GROUP_SIZE)]
    return "-".join(groups)


def normalize(text: str) -> str:
    """Turn what the technician typed into the canonical 16-symbol code.

    Uppercases, removes dashes and all whitespace, maps O->0 and I/L->1, then checks
    length and alphabet. Raises InvalidSetupCode otherwise.
    """
    code = "".join(text.upper().split()).replace("-", "").translate(_LOOK_ALIKES)
    if len(code) != CODE_LENGTH:
        raise InvalidSetupCode(f"A setup code has {CODE_LENGTH} characters.")
    if any(ch not in ALPHABET for ch in code):
        raise InvalidSetupCode("The setup code contains a character that is not allowed.")
    return code
