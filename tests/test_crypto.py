# Tests for handshake and envelope: success, wrong code, tampering, replay, skipped seq,
# plus a known-answer vector that pins the key derivation of SPEC §5.2.

import dataclasses
import hashlib
import hmac
from collections.abc import Callable

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from teton_common.crypto import (
    ConfiguratorHandshake,
    DeviceHandshake,
    EnvelopeError,
    HandshakeError,
    Role,
    SessionCipher,
    SessionKeys,
)
from teton_common.messages import Confirm, Envelope, Hello

CODE = "7K2MQX9D4HTRB8WZ"
WRONG_CODE = "7K2MQX9D4HTRB8WY"
DEVICE_ID = "TETON-3F9A2C"


def handshake(code_c: str = CODE, code_d: str = CODE) -> tuple[SessionKeys, SessionKeys]:
    """Run a full handshake; returns (configurator keys, device keys)."""
    c = ConfiguratorHandshake(code_c)
    d = DeviceHandshake(DEVICE_ID, code_d)
    confirm, keys_c = c.on_hello_reply(d.on_hello(c.hello()))
    return keys_c, d.on_confirm(confirm)


def ciphers() -> tuple[SessionCipher, SessionCipher]:
    keys_c, keys_d = handshake()
    return SessionCipher(keys_c, Role.CONFIGURATOR), SessionCipher(keys_d, Role.DEVICE)


# ---------------------------------------------------------------------------
# Handshake


def test_both_sides_derive_the_same_distinct_keys() -> None:
    keys_c, keys_d = handshake()
    assert keys_c == keys_d
    assert keys_c.c2d != keys_c.d2c
    assert len(keys_c.c2d) == len(keys_c.d2c) == 32


def test_every_session_gets_fresh_keys() -> None:
    assert handshake()[0] != handshake()[0]


def test_code_typing_variants_give_the_same_keys() -> None:
    keys_c, keys_d = handshake(code_c="7k2m-qx9d-4htr-b8wz", code_d=CODE)
    assert keys_c == keys_d


def test_wrong_code_is_caught_by_the_configurator_before_it_sends_anything() -> None:
    c = ConfiguratorHandshake(WRONG_CODE)
    d = DeviceHandshake(DEVICE_ID, CODE)
    with pytest.raises(HandshakeError, match="setup code is wrong"):
        c.on_hello_reply(d.on_hello(c.hello()))  # raises instead of returning a Confirm


def test_fake_configurator_cannot_complete_the_handshake() -> None:
    # Someone without the code gets a hello_reply but can't produce a valid confirm_c.
    c = ConfiguratorHandshake(WRONG_CODE)
    d = DeviceHandshake(DEVICE_ID, CODE)
    d.on_hello(c.hello())
    with pytest.raises(HandshakeError):
        d.on_confirm(Confirm(confirm=bytes(32)))


def test_reflected_device_tag_is_rejected() -> None:
    # Sending the device's own tag back as the configurator's must fail (different labels).
    c = ConfiguratorHandshake(CODE)
    d = DeviceHandshake(DEVICE_ID, CODE)
    reply = d.on_hello(c.hello())
    with pytest.raises(HandshakeError):
        d.on_confirm(Confirm(confirm=reply.confirm))


@pytest.mark.parametrize("change", [{"device_id": "TETON-000000"}, {"nonce": bytes(16)}])
def test_tampered_hello_reply_is_rejected(change: dict[str, object]) -> None:
    # A man in the middle changing any transcript field breaks confirm_d.
    c = ConfiguratorHandshake(CODE)
    reply = DeviceHandshake(DEVICE_ID, CODE).on_hello(c.hello())
    with pytest.raises(HandshakeError):
        c.on_hello_reply(dataclasses.replace(reply, **change))


def test_all_zero_public_key_is_rejected() -> None:
    d = DeviceHandshake(DEVICE_ID, CODE)
    with pytest.raises(HandshakeError):
        d.on_hello(Hello(pub=bytes(32), nonce=bytes(16)))


def test_handshake_steps_are_single_use_and_ordered() -> None:
    c = ConfiguratorHandshake(CODE)
    d = DeviceHandshake(DEVICE_ID, CODE)
    with pytest.raises(HandshakeError):
        d.on_confirm(Confirm(confirm=bytes(32)))  # no hello yet
    reply = d.on_hello(c.hello())
    with pytest.raises(HandshakeError):
        d.on_hello(c.hello())  # second hello on the same object
    confirm, _ = c.on_hello_reply(reply)
    with pytest.raises(HandshakeError):
        c.on_hello_reply(reply)  # second reply
    d.on_confirm(confirm)
    with pytest.raises(HandshakeError):
        d.on_confirm(confirm)  # second confirm


# ---------------------------------------------------------------------------
# Envelope


def test_round_trip_both_directions_and_seq_counts_up() -> None:
    conf, dev = ciphers()
    for i in range(3):
        env = conf.seal(b'{"type":"scan"}')
        assert env.seq == i
        assert dev.open(env) == b'{"type":"scan"}'
    env = dev.seal(b'{"type":"ready"}')
    assert env.seq == 0  # each direction has its own counter
    assert conf.open(env) == b'{"type":"ready"}'


def test_tampered_ciphertext_is_rejected() -> None:
    conf, dev = ciphers()
    env = conf.seal(b"secret command")
    flipped = bytes([env.ct[0] ^ 1]) + env.ct[1:]
    with pytest.raises(EnvelopeError):
        dev.open(Envelope(seq=env.seq, ct=flipped))


def test_truncated_ciphertext_is_rejected() -> None:
    conf, dev = ciphers()
    env = conf.seal(b"secret command")
    with pytest.raises(EnvelopeError):
        dev.open(Envelope(seq=0, ct=env.ct[:10]))  # shorter than the 16-byte GCM tag


def test_replayed_message_is_rejected() -> None:
    conf, dev = ciphers()
    env = conf.seal(b"first")
    dev.open(env)
    with pytest.raises(EnvelopeError):
        dev.open(env)


def test_skipped_and_reordered_messages_are_rejected() -> None:
    conf, dev = ciphers()
    first, second = conf.seal(b"first"), conf.seal(b"second")
    with pytest.raises(EnvelopeError):
        dev.open(second)  # expected seq 0
    assert dev.open(first) == b"first"


def test_changed_seq_is_rejected_even_when_expected() -> None:
    # Re-labelling message 1 as seq 0 fails: seq is bound into the nonce and the AAD.
    conf, dev = ciphers()
    conf.seal(b"first")
    second = conf.seal(b"second")
    with pytest.raises(EnvelopeError):
        dev.open(Envelope(seq=0, ct=second.ct))


def test_message_cannot_be_reflected_to_its_sender() -> None:
    conf, _ = ciphers()
    with pytest.raises(EnvelopeError):
        conf.open(conf.seal(b"c2d message"))  # opened as if it were d2c


def test_other_session_keys_cannot_open() -> None:
    conf, _ = ciphers()
    _, other_dev = ciphers()
    with pytest.raises(EnvelopeError):
        other_dev.open(conf.seal(b"command"))


# ---------------------------------------------------------------------------
# Known-answer vector: fixed randomness, recomputed independently from the SPEC formula.

PRIV_C, NONCE_C = bytes(range(1, 33)), bytes(range(33, 49))
PRIV_D, NONCE_D = bytes(range(101, 133)), bytes(range(133, 149))


def fixed(*chunks: bytes) -> Callable[[int], bytes]:
    """A randbytes() that returns the given chunks in order."""
    queue = list(chunks)

    def randbytes(n: int) -> bytes:
        chunk = queue.pop(0)
        assert len(chunk) == n
        return chunk

    return randbytes


def test_known_answer_vector() -> None:
    c = ConfiguratorHandshake("7K2M-QX9D-4HTR-B8WZ", randbytes=fixed(PRIV_C, NONCE_C))
    d = DeviceHandshake(DEVICE_ID, CODE, randbytes=fixed(PRIV_D, NONCE_D))
    reply = d.on_hello(c.hello())
    confirm, keys = c.on_hello_reply(reply)
    assert d.on_confirm(confirm) == keys

    # SPEC §5.2, line by line, with standard-library SHA-256 and HMAC.
    key_c = X25519PrivateKey.from_private_bytes(PRIV_C)
    pub_c = key_c.public_key().public_bytes_raw()
    pub_d = X25519PrivateKey.from_private_bytes(PRIV_D).public_key().public_bytes_raw()
    shared = key_c.exchange(X25519PublicKey.from_public_bytes(pub_d))
    transcript = hashlib.sha256(
        b"teton-prov-v1" + DEVICE_ID.encode() + pub_c + pub_d + NONCE_C + NONCE_D
    ).digest()
    okm = HKDF(hashes.SHA256(), length=96, salt=CODE.encode(), info=transcript).derive(shared)
    k_c2d, k_d2c, k_confirm = okm[0:32], okm[32:64], okm[64:96]
    confirm_d = hmac.new(k_confirm, b"device" + transcript, hashlib.sha256).digest()
    confirm_c = hmac.new(k_confirm, b"configurator" + transcript, hashlib.sha256).digest()

    assert (reply.pub, c.hello().pub) == (pub_d, pub_c)
    assert (keys.c2d, keys.d2c) == (k_c2d, k_d2c)
    assert (reply.confirm, confirm.confirm) == (confirm_d, confirm_c)

    # Pinned values: any change to the derivation (or the COULD web page) must match these.
    assert confirm_d.hex() == "7deef41dc487653bb49369125e81f030313b2360a73f454ddec253fcb3124040"
    assert confirm_c.hex() == "85ad383a2d43b319d8a55b24d4855ebd89c57e65fe28a9f4ceae659f84b421b1"
    assert k_c2d.hex() == "1256659cd779ef71d411db722599c5d3d0e890ca480f9b792b453d58cf9ebe4d"
    assert k_d2c.hex() == "e7468bbb343bafb7f9f569b6254ab80c832ee00f09b1ff78b36c191277993433"

    # SPEC §5.3: first device message, seq 0.
    ready = SessionCipher(keys, Role.DEVICE).seal(b'{"type":"ready"}')
    assert ready.ct.hex() == "a77a507685a45bbf0c53339245723049e6ba84eb34f08f72176c5e78e13ad960"


# ---------------------------------------------------------------------------
# No key material in repr() or errors.


def test_keys_are_not_in_repr_or_errors() -> None:
    keys_c, _ = handshake()
    text = repr(keys_c)
    assert keys_c.c2d.hex() not in text and str(keys_c.c2d) not in text

    c = ConfiguratorHandshake(WRONG_CODE)
    d = DeviceHandshake(DEVICE_ID, CODE)
    with pytest.raises(HandshakeError) as exc:
        c.on_hello_reply(d.on_hello(c.hello()))
    assert CODE not in str(exc.value) and WRONG_CODE not in str(exc.value)
    assert exc.value.__cause__ is None

    conf, dev = ciphers()
    env = conf.seal(b"secret")
    with pytest.raises(EnvelopeError) as exc:
        dev.open(Envelope(seq=0, ct=bytes(len(env.ct))))
    assert exc.value.__cause__ is None and exc.value.__suppress_context__
