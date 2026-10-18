# Tests for chunking/reassembly, max size, errors and the reassembly timeout.

import pytest

from teton_common.framing import FramingError, Reassembler, frame
from teton_common.protocol import CHUNK_SIZE_BYTES, MAX_MESSAGE_BYTES


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def reassemble(chunks: list[bytes], reassembler: Reassembler | None = None) -> bytes | None:
    r = reassembler or Reassembler()
    results = [r.feed(c) for c in chunks]
    assert all(result is None for result in results[:-1])  # complete only at the last chunk
    return results[-1]


@pytest.mark.parametrize("chunk_size", [CHUNK_SIZE_BYTES, 185])
@pytest.mark.parametrize("size", [0, 1, 18, 19, 150, 2000])
def test_round_trip(chunk_size: int, size: int) -> None:
    payload = bytes(i % 256 for i in range(size))
    chunks = frame(payload, chunk_size)
    assert all(1 <= len(c) <= chunk_size for c in chunks)
    assert reassemble(chunks) == payload


def test_header_is_payload_length_big_endian() -> None:
    chunks = frame(b"x" * 300)
    assert chunks[0][:2] == b"\x01\x2c"  # 300, not counting the 2-byte header


def test_150_byte_message_gives_8_chunks_of_20() -> None:
    chunks = frame(b"x" * 150)
    assert [len(c) for c in chunks] == [20, 20, 20, 20, 20, 20, 20, 12]


def test_empty_payload_is_just_the_header() -> None:
    assert frame(b"") == [b"\x00\x00"]
    assert Reassembler().feed(b"\x00\x00") == b""


def test_header_split_across_chunks() -> None:
    assert reassemble(frame(b"hello", chunk_size=1)) == b"hello"


def test_max_size() -> None:
    payload = b"x" * MAX_MESSAGE_BYTES
    assert reassemble(frame(payload)) == payload
    with pytest.raises(FramingError):
        frame(b"x" * (MAX_MESSAGE_BYTES + 1))


def test_oversized_declared_length_is_refused_at_the_header() -> None:
    r = Reassembler()
    with pytest.raises(FramingError):
        r.feed((MAX_MESSAGE_BYTES + 1).to_bytes(2, "big"))  # nothing else needed


def test_bad_chunks_raise_and_then_recover() -> None:
    r = Reassembler()
    with pytest.raises(FramingError):
        r.feed(b"")  # frame() never sends an empty chunk
    with pytest.raises(FramingError):
        r.feed(b"\x00\x02abc")  # declares 2 bytes, carries 3
    assert reassemble(frame(b"after errors"), r) == b"after errors"


def test_reset_drops_a_partial_message() -> None:
    r = Reassembler()
    first = frame(b"x" * 50)
    r.feed(first[0])
    r.reset()
    assert reassemble(frame(b"fresh"), r) == b"fresh"


def test_partial_message_times_out_after_10_seconds() -> None:
    clock = FakeClock()
    r = Reassembler(clock)

    # Still within the timeout: the message completes.
    slow = frame(b"y" * 30)
    r.feed(slow[0])
    clock.now = 9.9
    assert r.feed(slow[1]) == b"y" * 30

    # Past the timeout: the stale start is dropped and the new message decodes on its own.
    clock.now = 100.0
    r.feed(frame(b"z" * 30)[0])
    clock.now = 110.1
    assert reassemble(frame(b"new"), r) == b"new"


def test_separate_reassemblers_per_channel() -> None:
    session, status = Reassembler(), Reassembler()
    a, b = frame(b"A" * 45), frame(b"B" * 45)
    results = []
    for chunk_a, chunk_b in zip(a, b, strict=True):
        results += [session.feed(chunk_a), status.feed(chunk_b)]
    assert results[-2:] == [b"A" * 45, b"B" * 45]


def test_errors_do_not_contain_data() -> None:
    with pytest.raises(FramingError) as exc:
        Reassembler().feed(b"\x00\x02secret")
    assert "secret" not in str(exc.value)
