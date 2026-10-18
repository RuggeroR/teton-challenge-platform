# Simulation transport: connects to the device's Unix domain socket. See SPEC §10.
#
# The configurator's half of sim_server.py, with the same rules as BLE and the memory
# transport. There is no disconnect event (review Q36): when the device goes away, the next
# send() or read_info() raises TransportError, which ProvisioningClient turns into
# ConnectionLostError. The record format is in teton_common/sim_wire.py.

import asyncio
import contextlib
from pathlib import Path

import structlog

from teton_common.messages import MessageError, decode_info
from teton_common.protocol import Channel
from teton_common.sim_wire import (
    CLIENT_RECEIVES,
    REPLY_WAIT_S,
    WireError,
    pack,
    read_record,
    socket_path,
)
from teton_common.transport import (
    CLIENT_SENDS,
    DEVICE_SENDS,
    Receiver,
    TransportError,
    check_chunk,
)
from teton_configurator.flow import FoundDevice

log = structlog.get_logger()

SIM_SIGNAL = 100  # no radio, so always full bars
CLOSE_WAIT_S = 1  # how long close() waits for the device to drop the connection (Q70)


class SimClientTransport:
    """Configurator end of the simulation. Implements ClientTransport."""

    def __init__(self, path: Path | None = None, reply_wait_s: float = REPLY_WAIT_S) -> None:
        self.path = path or socket_path()
        self._reply_wait_s = reply_wait_s
        self._receivers: dict[Channel, Receiver] = {}
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task[None] | None = None  # reads everything the device sends
        self._info_replies: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._info_lock = asyncio.Lock()  # one info request in flight at a time
        self._connected = False

    async def connect(self) -> None:
        if self._connected:
            raise TransportError("already connected")
        try:
            reader, writer = await asyncio.open_unix_connection(str(self.path))
        except OSError:
            raise TransportError("no simulated device is running") from None
        self._writer = writer
        self._info_replies = asyncio.Queue()
        self._connected = True
        self._task = asyncio.create_task(self._receive(reader, writer))
        # A device that isn't accepting closes the connection at once, so one info round
        # trip tells whether we really got in (review Q64).
        try:
            await self.read_info()
        except TransportError:
            await self.close()
            raise TransportError("the device is not accepting connections") from None

    async def read_info(self) -> bytes:
        self._require_connected()
        async with self._info_lock:
            await self._write(pack(Channel.INFO, b""))
            try:
                async with asyncio.timeout(self._reply_wait_s):
                    reply = await self._info_replies.get()
            except TimeoutError:
                raise TransportError("the device did not answer the info request") from None
        if reply is None:
            raise TransportError("the device closed the connection")
        return reply

    def on_receive(self, channel: Channel, cb: Receiver) -> None:
        if channel not in DEVICE_SENDS:
            raise TransportError(f"the configurator does not receive on {channel}")
        self._receivers[channel] = cb

    async def send(self, channel: Channel, data: bytes) -> None:
        check_chunk(channel, data, CLIENT_SENDS, "configurator")
        self._require_connected()
        await self._write(pack(channel, data))

    async def close(self) -> None:
        """Leave, and wait until the device has dropped the connection, so it has freed its
        one slot when this returns (review Q70)."""
        writer, self._writer = self._writer, None
        self._connected = False
        if writer is None:
            return
        task, self._task = self._task, None
        with contextlib.suppress(OSError):
            if not writer.is_closing():
                writer.write_eof()  # the device sees the end and closes its side
        if task is not None and task is not asyncio.current_task():
            _, pending = await asyncio.wait({task}, timeout=CLOSE_WAIT_S)
            for t in pending:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await t
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()

    async def _write(self, record: bytes) -> None:
        writer = self._writer
        if writer is None or writer.is_closing():
            raise TransportError("not connected")
        try:
            writer.write(record)
            await writer.drain()
        except OSError:
            raise TransportError("the connection to the device was lost") from None

    async def _receive(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while (record := await read_record(reader, CLIENT_RECEIVES)) is not None:
                channel, payload = record
                if channel == Channel.INFO:
                    self._info_replies.put_nowait(payload)
                    continue
                cb = self._receivers.get(channel)
                if cb is None:
                    continue  # nobody subscribed
                try:
                    await cb(payload)
                except Exception:
                    log.exception("receiver_failed", side="configurator", channel=channel)
        except WireError as exc:
            log.warning("sim_bad_record", reason=str(exc))  # never the bytes (Q67)
        except OSError:
            pass  # reset by the device: the same as a close
        finally:
            # Whatever ended it, this connection is over: later sends fail (Q36).
            self._connected = False
            self._info_replies.put_nowait(None)  # wakes a read_info() that is waiting
            writer.close()

    def _require_connected(self) -> None:
        if not self._connected:
            raise TransportError("not connected")


async def discover(path: Path | None = None) -> list[FoundDevice]:
    """The simulated device, while it accepts a configurator; otherwise nothing, as a BLE
    scan finds nothing while the device isn't advertising (review Q64, Q72)."""
    transport = SimClientTransport(path)
    try:
        await transport.connect()
        data = await transport.read_info()
    except TransportError:
        return []
    finally:
        await transport.close()
    try:
        info = decode_info(data)
    except MessageError as exc:
        log.warning("sim_bad_info", reason=str(exc))
        return []
    return [FoundDevice(name=info.name, signal=SIM_SIGNAL, address=str(transport.path))]
