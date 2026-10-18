# Handshake (X25519 + HKDF salted with setup code + HMAC key confirmation) for both roles,
# and the AES-256-GCM envelope with per-direction sequence numbers. See SPEC §5.
#
# Pure and synchronous: no I/O. All primitives come from `cryptography`; tags are compared
# in constant time. Errors never contain key material.

import hmac
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import hmac as crypto_hmac
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from teton_common.messages import Confirm, Envelope, Hello, HelloReply
from teton_common.protocol import HANDSHAKE_NONCE_BYTES, MAX_SEQ, PROTOCOL_LABEL
from teton_common.setup_code import normalize

KEY_BYTES = 32  # each derived key: AES-256 and HMAC-SHA256
C2D, D2C = "c2d", "d2c"  # directions: configurator -> device, device -> configurator

RandBytes = Callable[[int], bytes]


class Role(StrEnum):
    # The values are also the labels in the confirmation tags (SPEC §5.2).
    CONFIGURATOR = "configurator"
    DEVICE = "device"


class HandshakeError(Exception):
    """The handshake failed. The text never contains key material."""


class EnvelopeError(Exception):
    """An encrypted message was rejected. The session must end (SPEC §5.3)."""


@dataclass(frozen=True)
class SessionKeys:
    """The two AES-256-GCM keys of a session, one per direction."""

    c2d: bytes = field(repr=False)
    d2c: bytes = field(repr=False)


# ---------------------------------------------------------------------------
# Key derivation (SPEC §5.2)


def _transcript(
    device_id: str, pub_c: bytes, pub_d: bytes, nonce_c: bytes, nonce_d: bytes
) -> bytes:
    """SHA-256 fingerprint of this conversation.

    Plain concatenation is unambiguous because every part has a fixed length.
    """
    digest = hashes.Hash(hashes.SHA256())
    for part in (
        PROTOCOL_LABEL.encode(),
        device_id.encode("ascii"),
        pub_c,
        pub_d,
        nonce_c,
        nonce_d,
    ):
        digest.update(part)
    return digest.finalize()


def _derive(
    own_key: X25519PrivateKey, peer_pub: bytes, setup_code: str, transcript: bytes
) -> tuple[SessionKeys, bytes]:
    """ECDH, then HKDF salted with the setup code. Returns the session keys and k_confirm."""
    try:
        shared = own_key.exchange(X25519PublicKey.from_public_bytes(peer_pub))
    except ValueError:
        # e.g. an all-zero or other low-order public key, which forces a known shared secret
        raise HandshakeError("the peer's public key is not usable") from None
    okm = HKDF(
        algorithm=hashes.SHA256(),
        length=3 * KEY_BYTES,
        salt=setup_code.encode("ascii"),
        info=transcript,
    ).derive(shared)
    keys = SessionKeys(c2d=okm[:KEY_BYTES], d2c=okm[KEY_BYTES : 2 * KEY_BYTES])
    return keys, okm[2 * KEY_BYTES :]


def _confirm_tag(k_confirm: bytes, sender: Role, transcript: bytes) -> bytes:
    """HMAC-SHA256(k_confirm, sender label || transcript). Different labels per side stop a
    tag from being reflected back as the other side's."""
    mac = crypto_hmac.HMAC(k_confirm, hashes.SHA256())
    mac.update(sender.value.encode() + transcript)
    return mac.finalize()


def _new_key(randbytes: RandBytes) -> X25519PrivateKey:
    # Any 32 random bytes are a valid X25519 private key: X25519 clamps the bits itself.
    return X25519PrivateKey.from_private_bytes(randbytes(KEY_BYTES))


def _public_bytes(key: X25519PrivateKey) -> bytes:
    return key.public_key().public_bytes_raw()


# ---------------------------------------------------------------------------
# Handshake, one object per side and per session (SPEC §5.1). Each object is single-use.


class ConfiguratorHandshake:
    def __init__(self, setup_code: str, randbytes: RandBytes = os.urandom) -> None:
        self._code = normalize(setup_code)  # same salt however the code was typed
        self._key: X25519PrivateKey | None = _new_key(randbytes)
        self._hello = Hello(pub=_public_bytes(self._key), nonce=randbytes(HANDSHAKE_NONCE_BYTES))

    def hello(self) -> Hello:
        return self._hello

    def on_hello_reply(self, reply: HelloReply) -> tuple[Confirm, SessionKeys]:
        """Check the device's tag; only then return our own tag and the session keys."""
        if self._key is None:
            raise HandshakeError("handshake already finished")
        key, self._key = self._key, None  # single use; drop the private key
        transcript = _transcript(
            reply.device_id, self._hello.pub, reply.pub, self._hello.nonce, reply.nonce
        )
        keys, k_confirm = _derive(key, reply.pub, self._code, transcript)
        if not hmac.compare_digest(reply.confirm, _confirm_tag(k_confirm, Role.DEVICE, transcript)):
            # Stop here: nothing is sent, so a fake device learns nothing to guess with.
            raise HandshakeError("setup code is wrong or this is not the right device")
        return Confirm(confirm=_confirm_tag(k_confirm, Role.CONFIGURATOR, transcript)), keys


class DeviceHandshake:
    def __init__(self, device_id: str, setup_code: str, randbytes: RandBytes = os.urandom) -> None:
        self._device_id = device_id
        self._code = normalize(setup_code)
        self._randbytes = randbytes
        self._started = False
        self._pending: tuple[bytes, SessionKeys, bytes] | None = None  # transcript, keys, k_confirm

    def on_hello(self, hello: Hello) -> HelloReply:
        """Derive the keys and reply with our public key, nonce and tag."""
        if self._started:
            raise HandshakeError("handshake already started")
        self._started = True
        key = _new_key(self._randbytes)
        pub_d, nonce_d = _public_bytes(key), self._randbytes(HANDSHAKE_NONCE_BYTES)
        transcript = _transcript(self._device_id, hello.pub, pub_d, hello.nonce, nonce_d)
        keys, k_confirm = _derive(key, hello.pub, self._code, transcript)
        self._pending = (transcript, keys, k_confirm)
        return HelloReply(
            device_id=self._device_id,
            pub=pub_d,
            nonce=nonce_d,
            confirm=_confirm_tag(k_confirm, Role.DEVICE, transcript),
        )

    def on_confirm(self, confirm: Confirm) -> SessionKeys:
        """Check the configurator's tag. Only then is the session established."""
        if self._pending is None:
            raise HandshakeError("not waiting for a confirm")
        transcript, keys, k_confirm = self._pending
        self._pending = None
        expected = _confirm_tag(k_confirm, Role.CONFIGURATOR, transcript)
        if not hmac.compare_digest(confirm.confirm, expected):
            raise HandshakeError("the configurator did not prove it knows the setup code")
        return keys


# ---------------------------------------------------------------------------
# Encrypted envelope (SPEC §5.3)


def _nonce(seq: int) -> bytes:
    # 12-byte GCM nonce. Never repeats for a key: seq only counts up, keys differ per
    # direction, and every session derives fresh keys.
    return bytes(4) + seq.to_bytes(8, "big")


def _aad(direction: str, seq: int) -> bytes:
    return f"{PROTOCOL_LABEL}|{direction}|{seq}".encode()


class SessionCipher:
    """Seals our messages and opens the peer's, each direction with its own key and counter."""

    def __init__(self, keys: SessionKeys, role: Role) -> None:
        if role == Role.CONFIGURATOR:
            send_key, self._send_dir, recv_key, self._recv_dir = keys.c2d, C2D, keys.d2c, D2C
        else:
            send_key, self._send_dir, recv_key, self._recv_dir = keys.d2c, D2C, keys.c2d, C2D
        self._send, self._recv = AESGCM(send_key), AESGCM(recv_key)
        self._send_seq = 0
        self._recv_seq = 0

    def seal(self, plaintext: bytes) -> Envelope:
        seq = self._send_seq
        if seq > MAX_SEQ:
            raise EnvelopeError("sequence numbers exhausted")
        ct = self._send.encrypt(_nonce(seq), plaintext, _aad(self._send_dir, seq))
        self._send_seq += 1
        return Envelope(seq=seq, ct=ct)

    def open(self, envelope: Envelope) -> bytes:
        """Only the next expected seq is accepted: replay, reordering and gaps all fail."""
        seq = envelope.seq
        if seq != self._recv_seq:
            raise EnvelopeError("message rejected: unexpected sequence number")
        try:
            plaintext = self._recv.decrypt(_nonce(seq), envelope.ct, _aad(self._recv_dir, seq))
        except InvalidTag:
            raise EnvelopeError("message rejected: authentication failed") from None
        self._recv_seq += 1
        return plaintext
