# Simulation transport: a Unix domain socket server carrying the same four channels. See
# SPEC §10.
#
# Only the radio is simulated. It follows the same rules as BLE and the memory transport:
# channel directions and 20-byte chunks (transport.check_chunk), a send with no client goes
# nowhere, and turning advertising off keeps the connected client. Like the radio, it serves
# one configurator at a time: a connection that arrives while the pairing window is closed,
# or while a configurator is connected, is closed at once (review Q64), so a second
# configurator can neither find nor connect to the device.
#
# The record format is in teton_common/sim_wire.py; framing and crypto stay above.

import asyncio
import contextlib
import os
import stat
from pathlib import Path

import structlog

from teton_common.protocol import Channel
from teton_common.sim_wire import DEVICE_RECEIVES, WireError, pack, read_record, socket_path
from teton_common.transport import (
    CLIENT_SENDS,
    DEVICE_SENDS,
    Receiver,
    TransportError,
    check_chunk,
)

log = structlog.get_logger()


class SimServerTransport:
    """Device end of the simulation. Implements DeviceTransport."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or socket_path()
        self._info = b""
        self._receivers: dict[Channel, Receiver] = {}
        self._server: asyncio.Server | None = None
        self._advertising = False
        self._closed = False
        self._client: asyncio.StreamWriter | None = None  # the one configurator
        self._client_task: asyncio.Task[None] | None = None  # the task serving it

    async def start(self) -> None:
        if self._closed:
            raise TransportError("transport is closed")
        if self._server is not None:
            return
        await _clear_stale_socket(self.path)
        try:
            self._server = await asyncio.start_unix_server(self._serve, path=str(self.path))
        except OSError as exc:
            raise TransportError(f"cannot listen on {self.path}: {exc.strerror}") from None
        os.chmod(self.path, 0o600)  # only this user can connect (review Q69)
        log.info("sim_listening", path=str(self.path))

    async def set_advertising(self, on: bool) -> None:
        if self._server is None or self._closed:
            raise TransportError("transport is not started")
        self._advertising = on  # a connected client stays connected

    def set_info(self, data: bytes) -> None:
        self._info = bytes(data)

    def on_receive(self, channel: Channel, cb: Receiver) -> None:
        if channel not in CLIENT_SENDS:
            raise TransportError(f"the device does not receive on {channel}")
        self._receivers[channel] = cb

    async def send(self, channel: Channel, data: bytes) -> None:
        check_chunk(channel, data, DEVICE_SENDS, "device")
        if self._closed:
            raise TransportError("transport is closed")
        writer = self._client
        if writer is None or writer.is_closing():
            return  # no client: dropped, like a BLE notify with no subscriber
        try:
            writer.write(pack(channel, data))
            await writer.drain()
        except OSError:
            # The configurator has just gone; its task notices and frees the slot (Q70).
            log.debug("sim_send_dropped", channel=channel)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._advertising = False
        task, self._client_task = self._client_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()  # its `finally` closes the connection, as BLE drops it on power off
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            # Only a socket we created: after a failed start the path may be another
            # device's live socket. (Python 3.13+ already removes it in Server.close().)
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
            log.info("sim_stopped")

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Runs once per incoming connection, for as long as it lasts."""
        if self._closed or not self._advertising or self._client is not None:
            reason = "a configurator is connected" if self._client else "not advertising"
            log.info("sim_connection_refused", reason=reason)
            writer.close()  # the client's connect() sees the connection end (Q64)
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            return
        self._client = writer
        self._client_task = asyncio.current_task()
        log.info("sim_client_connected")
        try:
            while (record := await read_record(reader, DEVICE_RECEIVES)) is not None:
                channel, payload = record
                if channel == Channel.INFO:
                    writer.write(pack(Channel.INFO, self._info))
                    await writer.drain()
                    continue
                cb = self._receivers.get(channel)
                if cb is None:
                    continue  # nobody listening, like a write to an unused characteristic
                try:
                    await cb(payload)
                except Exception:
                    # One failing callback must not drop the configurator.
                    log.exception("receiver_failed", side="device", channel=channel)
        except WireError as exc:
            log.warning("sim_bad_record", reason=str(exc))  # never the bytes (Q67)
        except OSError as exc:
            log.info("sim_connection_error", reason=exc.strerror or type(exc).__name__)
        finally:
            # Free the slot before closing, so a configurator that waits for our EOF can
            # connect again right after (review Q70).
            if self._client is writer:
                self._client = None
                self._client_task = None
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            log.info("sim_client_left")


async def _clear_stale_socket(path: Path) -> None:
    """Remove a socket file left by a device that didn't shut down cleanly, but never a live
    device's socket or a file that isn't a socket (review Q68)."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise TransportError(f"{path} exists and is not a socket; not touching it")
    try:
        _, writer = await asyncio.open_unix_connection(str(path))
    except ConnectionRefusedError:
        path.unlink()  # nobody listens: stale
        log.info("sim_stale_socket_removed", path=str(path))
        return
    except OSError as exc:
        raise TransportError(f"cannot check {path}: {exc.strerror}") from None
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    raise TransportError("another simulated device is already running")
