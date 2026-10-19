"""Check a btmon capture for secrets (SPEC §12 MUST #5, review Q63, Q116).

`btmon -r capture | grep secret` proves nothing: btmon prints packet data as hex, and our
messages travel in 20-byte chunks, so a secret could be split across packets. This script
reads the capture itself, puts the ATT values of each characteristic back together in
order, and searches those streams (and the raw file) for each secret, as text and in any
letter case. It also reports how many ATT writes and notifications it saw, so an empty or
wrong capture can't pass by accident.

Usage:
    python scripts/check_capture.py docs/evidence/run-psk.btsnoop SECRET [SECRET ...]
Exit code 0 when no secret is found, 1 when one is (or the capture has no ATT traffic).
Only the standard library; reads btmon's own format (btsnoop, link type 2001).
"""

import struct
import sys
from collections import defaultdict
from pathlib import Path

MONITOR_LINK_TYPE = 2001  # what `btmon -w` writes
ACL_TX, ACL_RX = 4, 5  # btmon monitor opcodes
ATT_CID = 0x0004
# ATT opcodes that carry a characteristic value: handle (2 bytes) then the value.
ATT_VALUE_OPCODES = {
    0x12: "write request",
    0x52: "write command",
    0x1B: "notification",
    0x1D: "indication",
}
ATT_READ_RESPONSE = 0x0B  # value only (the handle is in the request)


def records(data: bytes):  # noqa: ANN201
    if data[:8] != b"btsnoop\0":
        raise SystemExit("not a btsnoop file")
    _, link_type = struct.unpack(">II", data[8:16])
    if link_type != MONITOR_LINK_TYPE:
        raise SystemExit(f"link type {link_type}: expected {MONITOR_LINK_TYPE}, from `btmon -w`")
    offset = 16
    while offset + 24 <= len(data):
        _, included, flags, _, _ = struct.unpack(">IIIIq", data[offset : offset + 24])
        yield flags & 0xFFFF, data[offset + 24 : offset + 24 + included]
        offset += 24 + included


def att_pdus(data: bytes):  # noqa: ANN201
    """(direction, ATT PDU) for every ACL packet on the ATT channel, fragments joined."""
    partial: dict[tuple[int, int], bytes] = {}
    for opcode, packet in records(data):
        if opcode not in (ACL_TX, ACL_RX) or len(packet) < 4:
            continue
        handle_flags, length = struct.unpack("<HH", packet[:4])
        link, boundary = handle_flags & 0x0FFF, (handle_flags >> 12) & 0x3
        payload = packet[4 : 4 + length]
        key = (opcode, link)
        if boundary == 0x1:  # continuing fragment
            partial[key] = partial.get(key, b"") + payload
        else:  # first fragment of an L2CAP frame
            partial[key] = payload
        frame = partial[key]
        if len(frame) < 4:
            continue
        l2cap_length, cid = struct.unpack("<HH", frame[:4])
        if len(frame) < 4 + l2cap_length:
            continue  # wait for the rest
        del partial[key]
        if cid == ATT_CID:
            yield ("sent" if opcode == ACL_TX else "received"), frame[4 : 4 + l2cap_length]


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    capture, secrets = Path(sys.argv[1]), sys.argv[2:]
    raw = capture.read_bytes()
    streams: dict[str, bytes] = defaultdict(bytes)
    counts: dict[str, int] = defaultdict(int)
    for direction, pdu in att_pdus(raw):
        op = pdu[0]
        if op in ATT_VALUE_OPCODES and len(pdu) >= 3:
            handle = struct.unpack("<H", pdu[1:3])[0]
            streams[f"{direction} {ATT_VALUE_OPCODES[op]}s, handle 0x{handle:04x}"] += pdu[3:]
            counts[ATT_VALUE_OPCODES[op]] += 1
        elif op == ATT_READ_RESPONSE:
            streams[f"{direction} read responses"] += pdu[1:]
            counts["read response"] += 1

    print(f"{capture}: " + ", ".join(f"{n} {kind}s" for kind, n in sorted(counts.items())))
    if not counts.get("write request") or not counts.get("notification"):
        print("FAIL: no ATT writes or notifications: is this the capture of the run?")
        return 1
    for name, stream in sorted(streams.items()):
        print(f"  {name}: {len(stream)} bytes")

    found = False
    for i, secret in enumerate(secrets, 1):
        needle = secret.lower().encode()
        hits = [name for name, s in streams.items() if needle in s.lower()]
        if needle in raw.lower():
            hits.append("the raw file")
        # Never print the secret itself: this output may end up in the evidence.
        result = f"FOUND in {hits}" if hits else "not found"
        print(f"secret {i} ({len(secret)} characters): {result}")
        found = found or bool(hits)
    print("FAIL: a secret is in the capture" if found else "OK: no secret in the capture")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
