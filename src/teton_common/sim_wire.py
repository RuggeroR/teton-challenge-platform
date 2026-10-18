# Simulation wire format: how the four channels share one Unix stream socket. See SPEC §10.
#
# A socket is a byte stream with no message boundaries, so each chunk travels as one record:
#
#     [u8 channel][u16 big-endian length][payload]
#
# This is a small multiplexing layer *under* the normal framing (framing.py): a session,
# command or status record carries exactly one chunk of at most 20 bytes, as one BLE write
# or notify would. `info` is a request (an empty record from the configurator) and a
# response (the info JSON). There is no protocol, framing or crypto here (review Q66): the
# device's sim_server.py and the configurator's sim_client.py both use this module, and
# everything above them is the same as over BLE.

import asyncio
import os
import struct
import tempfile
from collections.abc import Mapping
from pathlib import Path

from teton_common.protocol import CHUNK_SIZE_BYTES, MAX_INFO_BYTES, Channel

SOCKET_NAME = "teton-sim.sock"
REPLY_WAIT_S = 2  # how long the configurator waits for the device to answer (review Q67)

CHANNEL_BYTES: Mapping[Channel, int] = {
    Channel.INFO: 0,
    Channel.SESSION: 1,
    Channel.COMMAND: 2,
    Channel.STATUS: 3,
}
_CHANNELS = {number: channel for channel, number in CHANNEL_BYTES.items()}
_HEADER = struct.Struct(">BH")

# What each side may receive, with the largest payload allowed on each channel. This also
# enforces the BLE directions: the device never receives `status`, the configurator never
# receives `command`. An info request is empty; an info response fits one GATT read.
DEVICE_RECEIVES: Mapping[Channel, int] = {
    Channel.INFO: 0,
    Channel.SESSION: CHUNK_SIZE_BYTES,
    Channel.COMMAND: CHUNK_SIZE_BYTES,
}
CLIENT_RECEIVES: Mapping[Channel, int] = {
    Channel.INFO: MAX_INFO_BYTES,
    Channel.SESSION: CHUNK_SIZE_BYTES,
    Channel.STATUS: CHUNK_SIZE_BYTES,
}


class WireError(Exception):
    """A malformed record. The text names the channel and length only, never the bytes, so
    it can be logged as is."""


def socket_path() -> Path:
    """`$XDG_RUNTIME_DIR/teton-sim.sock` (private to the user), else a per-user name in the
    temp directory, e.g. in CI (review Q69)."""
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / SOCKET_NAME
    return Path(tempfile.gettempdir()) / f"teton-sim-{os.getuid()}.sock"


def pack(channel: Channel, payload: bytes) -> bytes:
    return _HEADER.pack(CHANNEL_BYTES[channel], len(payload)) + payload


async def read_record(
    reader: asyncio.StreamReader, allowed: Mapping[Channel, int]
) -> tuple[Channel, bytes] | None:
    """The next record, or None when the peer closed the connection between two records.

    Raises WireError for an unknown channel, a channel not allowed in this direction, a
    length over the limit, or a connection that ends in the middle of a record.
    """
    try:
        header = await reader.readexactly(_HEADER.size)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None  # a clean end
        raise WireError("short read: the connection ended inside a header") from None
    number, length = _HEADER.unpack(header)
    channel = _CHANNELS.get(number)
    if channel is None:
        raise WireError(f"unknown channel byte {number}")
    if channel not in allowed:
        raise WireError(f"{channel} is not allowed in this direction")
    if length > allowed[channel]:
        raise WireError(f"{channel} record of {length} bytes, the limit is {allowed[channel]}")
    try:
        payload = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise WireError(
            f"short read: {channel} record of {length} bytes ended after {len(exc.partial)}"
        ) from None
    return channel, payload
