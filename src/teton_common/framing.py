# Length-prefixed framing: split a message into 20-byte chunks and reassemble them
# per channel, with max size and reassembly timeout. See SPEC §4.1.
#
# Frame = [u16 big-endian payload length][payload], cut into chunks. The length counts the
# payload only. Framing only finds where a message ends: integrity comes from AES-GCM
# (command/status) and the confirmation tags (session), so on any error we just reset.

import time
from collections.abc import Callable

from teton_common.protocol import CHUNK_SIZE_BYTES, MAX_MESSAGE_BYTES, REASSEMBLY_TIMEOUT_S

HEADER_BYTES = 2


class FramingError(ValueError):
    """Bad framing. The text never contains the data."""


def frame(payload: bytes, chunk_size: int = CHUNK_SIZE_BYTES) -> list[bytes]:
    """Prefix the payload with its length and cut the result into chunks.

    A frame never shares a chunk with the next one: the last chunk is just shorter.
    """
    if len(payload) > MAX_MESSAGE_BYTES:
        raise FramingError(f"message is larger than {MAX_MESSAGE_BYTES} bytes")
    if chunk_size < 1:
        raise FramingError("chunk size must be at least 1 byte")
    data = len(payload).to_bytes(HEADER_BYTES, "big") + payload
    return [data[i : i + chunk_size] for i in range(0, len(data), chunk_size)]


class Reassembler:
    """Collects the chunks of one channel and returns each complete payload.

    Use one per channel, so chunks of different characteristics never mix.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._buffer = b""
        self._started_at = 0.0  # when the first chunk of the current message arrived

    def reset(self) -> None:
        """Drop any partial message (new session, error)."""
        self._buffer = b""

    def feed(self, chunk: bytes) -> bytes | None:
        """Add one chunk. Returns the payload when the message is complete, else None."""
        now = self._clock()
        # Checked lazily: a stale partial message only matters when the next chunk arrives.
        if self._buffer and now - self._started_at > REASSEMBLY_TIMEOUT_S:
            self.reset()
        if not chunk:
            self._fail("empty chunk")
        if not self._buffer:
            self._started_at = now
        self._buffer += chunk

        if len(self._buffer) < HEADER_BYTES:
            return None
        length = int.from_bytes(self._buffer[:HEADER_BYTES], "big")
        # Refuse before collecting more, so a peer can't make us buffer more than the limit.
        if length > MAX_MESSAGE_BYTES:
            self._fail(f"declared length is larger than {MAX_MESSAGE_BYTES} bytes")
        end = HEADER_BYTES + length
        if len(self._buffer) < end:
            return None
        if len(self._buffer) > end:
            self._fail("data after the end of the message")

        payload = self._buffer[HEADER_BYTES:]
        self.reset()
        return payload

    def _fail(self, reason: str) -> None:
        self.reset()
        raise FramingError(reason)
