# Protocol client: handshake with setup code, scan, set_credentials, stream status updates.
# See SPEC §5.1, §6 and DESIGN §5.
#
# Two layers:
#   ClientChannel        whole messages on the configurator's channels: framing, one inbox
#                        for session + status, the cipher. Also used by the device tests'
#                        fake configurator, so the protocol exists only once (review Q37).
#   ProvisioningClient   the steps: handshake, scan, provision. A thin client: the device
#                        decides everything, this side only checks what it is told.
# Neither knows the transport (BLE, socket or memory). Errors are typed and never carry a
# secret; the flow turns them into ui_text sentences.

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import cast

import structlog

from teton_common.crypto import (
    ConfiguratorHandshake,
    EnvelopeError,
    HandshakeError,
    Role,
    SessionCipher,
)
from teton_common.messages import (
    CommandMessage,
    Error,
    HandshakeFailed,
    HelloReply,
    Info,
    MessageError,
    NetworkInfo,
    Ready,
    Scan,
    ScanResult,
    SessionMessage,
    SetCredentials,
    Status,
    StatusMessage,
    decode_envelope,
    decode_info,
    decode_session,
    decode_status,
    encode,
)
from teton_common.protocol import (
    CONFIGURATOR_REPLY_WAIT_S,
    CONFIGURATOR_RESULT_WAIT_S,
    CONFIGURATOR_SCAN_WAIT_S,
    Channel,
    ErrorCode,
    StatusState,
)
from teton_common.transport import ClientTransport, FramedTransport, Receiver, TransportError

log = structlog.get_logger()

DeviceMessage = SessionMessage | StatusMessage


# ---------------------------------------------------------------------------
# Typed errors (review Q33). After NoReplyError, ConnectionLostError or ProtocolError the
# client is finished; after WrongCodeError or RateLimitedError it may try again (Q35).


class ClientError(Exception):
    """Base class. The text is for logs and developers, never shown to the technician."""


class WrongCodeError(ClientError):
    """The device's tag didn't match: wrong setup code, or not the right device. Nothing
    was sent after the hello, so a fake device learns nothing to guess with."""


class RateLimitedError(ClientError):
    """The device refuses handshakes for a while (review Q5)."""

    def __init__(self, retry_after_s: int) -> None:
        super().__init__(f"rate limited, retry after {retry_after_s} s")
        self.retry_after_s = retry_after_s


class NoReplyError(ClientError):
    """A wait ran out. During the handshake: the pairing window is closed (Q22)."""


class DeviceRefusedError(ClientError):
    """The device answered a command with an error code (bad_request, unsupported_security)."""

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(f"device refused: {code}")
        self.code = code


class ProtocolError(ClientError):
    """The device sent something we can't accept (bad message, wrong type, wrong device_id)."""


class ConnectionLostError(ClientError):
    """The transport failed. No disconnect event exists (Q36): only send/read/connect fail."""


# ---------------------------------------------------------------------------


class ClientChannel:
    """Whole messages to and from the device.

    `session` and `status` share one inbox, in arrival order, because some steps wait for
    either (after confirm: `ready` on status or `handshake_failed` on session).
    """

    def __init__(self, transport: ClientTransport) -> None:
        self.transport = transport
        self._framed = FramedTransport(transport)
        self._inbox: asyncio.Queue[tuple[Channel, bytes]] = asyncio.Queue()
        self.cipher: SessionCipher | None = None  # set once the handshake succeeds
        # Registered before connect(): over BLE we must subscribe before writing hello.
        for channel in (Channel.SESSION, Channel.STATUS):
            self._framed.on_message(channel, self._receiver(channel))

    def _receiver(self, channel: Channel) -> Receiver:
        async def put(data: bytes) -> None:
            self._inbox.put_nowait((channel, data))

        return put

    def new_session(self) -> None:
        """Before a new hello: forget the old cipher and anything still in flight."""
        self.cipher = None
        self._framed.reset()
        while not self._inbox.empty():
            self._inbox.get_nowait()

    def pending(self) -> bool:
        return not self._inbox.empty()

    async def send_bytes(self, channel: Channel, data: bytes) -> None:
        try:
            await self._framed.send_message(channel, data)
        except TransportError as exc:
            raise ConnectionLostError(f"send failed: {exc}") from None

    async def send_session(self, msg: SessionMessage) -> None:
        await self.send_bytes(Channel.SESSION, encode(msg))

    async def send_sealed(self, plaintext: bytes) -> None:
        if self.cipher is None:
            raise ProtocolError("no session established")
        await self.send_bytes(Channel.COMMAND, encode(self.cipher.seal(plaintext)))

    async def send_command(self, msg: CommandMessage) -> None:
        await self.send_sealed(encode(msg))

    async def receive(self, timeout_s: float) -> tuple[Channel, DeviceMessage]:
        """The next message from the device, decoded (and decrypted, on status)."""
        try:
            async with asyncio.timeout(timeout_s):
                channel, data = await self._inbox.get()
        except TimeoutError:
            raise NoReplyError(f"no reply within {timeout_s} s") from None
        try:
            if channel == Channel.SESSION:
                return channel, decode_session(data)
            if self.cipher is None:
                raise ProtocolError("encrypted message before the handshake")
            return channel, decode_status(self.cipher.open(decode_envelope(data)))
        except (MessageError, EnvelopeError) as exc:
            raise ProtocolError(f"bad message on {channel}: {exc}") from None


class ProvisioningClient:
    """The configurator's half of the protocol, one connection to one device."""

    def __init__(
        self,
        transport: ClientTransport,
        reply_wait_s: float = CONFIGURATOR_REPLY_WAIT_S,
        scan_wait_s: float = CONFIGURATOR_SCAN_WAIT_S,
        result_wait_s: float = CONFIGURATOR_RESULT_WAIT_S,
    ) -> None:
        self._channel = ClientChannel(transport)
        self._transport = transport
        self._reply_wait_s = reply_wait_s
        self._scan_wait_s = scan_wait_s
        self._result_wait_s = result_wait_s
        self.info: Info | None = None

    async def connect(self) -> None:
        try:
            await self._transport.connect()
        except TransportError as exc:
            raise ConnectionLostError(f"connect failed: {exc}") from None

    async def read_info(self) -> Info:
        try:
            data = await self._transport.read_info()
        except TransportError as exc:
            raise ConnectionLostError(f"info read failed: {exc}") from None
        try:
            self.info = decode_info(data)
        except MessageError as exc:
            raise ProtocolError(f"bad info: {exc}") from None
        return self.info

    async def handshake(self, setup_code: str) -> Info:
        """SPEC §5.1, configurator side. May be called again after WrongCodeError or
        RateLimitedError: a new hello on the same connection (review Q4).

        The code is expected to be valid already (the flow checks it first, Q28);
        ConfiguratorHandshake raises InvalidSetupCode otherwise, before anything is sent.
        """
        info = self.info or await self.read_info()
        handshake = ConfiguratorHandshake(setup_code)
        self._channel.new_session()
        await self._channel.send_session(handshake.hello())

        _, reply = await self._channel.receive(self._reply_wait_s)  # none: window closed (Q22)
        if isinstance(reply, Error) and reply.code == ErrorCode.RATE_LIMITED:
            assert reply.retry_after_s is not None  # messages.py guarantees it
            raise RateLimitedError(reply.retry_after_s)
        if not isinstance(reply, HelloReply):
            raise ProtocolError(f"expected hello_reply, got {_type(reply)}")
        if reply.device_id != info.device_id:
            # Checked before the tag; nothing is sent. Retyping can't fix it (review Q34).
            raise ProtocolError("hello_reply comes from a different device_id than info")
        try:
            confirm, keys = handshake.on_hello_reply(reply)  # constant-time check in crypto.py
        except HandshakeError:
            raise WrongCodeError("the device's confirmation tag did not match") from None

        self._channel.cipher = SessionCipher(keys, Role.CONFIGURATOR)
        await self._channel.send_session(confirm)
        _, answer = await self._channel.receive(self._reply_wait_s)
        if isinstance(answer, HandshakeFailed):
            raise WrongCodeError("the device rejected our confirmation tag")
        if not isinstance(answer, Ready):
            raise ProtocolError(f"expected ready, got {_type(answer)}")
        log.info("handshake_ok", device_id=info.device_id)
        return info

    async def scan(self) -> list[NetworkInfo]:
        await self._channel.send_command(Scan())
        msg = await self._receive_status(self._scan_wait_s)
        if not isinstance(msg, ScanResult):
            raise ProtocolError(f"expected scan_result, got {_type(msg)}")
        return list(msg.networks)

    async def provision(self, creds: SetCredentials) -> AsyncIterator[Status]:
        """Yields `connecting`, then `connected` or `failed`. After `failed` the session
        stays open for a retry; after `connected` the device ends it."""
        await self._channel.send_command(creds)
        del creds  # the secrets are encrypted and sent; keep no reference (best effort)
        first = await self._receive_status(self._reply_wait_s)
        if not (isinstance(first, Status) and first.state == StatusState.CONNECTING):
            raise ProtocolError(f"expected connecting, got {_type(first)}")
        yield first
        result = await self._receive_status(self._result_wait_s)  # Q10: 70 s
        if not (isinstance(result, Status) and result.state != StatusState.CONNECTING):
            raise ProtocolError(f"expected connected or failed, got {_type(result)}")
        yield result

    async def close(self) -> None:
        with contextlib.suppress(TransportError):
            await self._transport.close()

    async def _receive_status(self, timeout_s: float) -> StatusMessage:
        channel, msg = await self._channel.receive(timeout_s)
        if channel != Channel.STATUS:
            raise ProtocolError(f"expected a status message, got {_type(msg)} on {channel}")
        if isinstance(msg, Error):
            raise DeviceRefusedError(msg.code)
        return cast(StatusMessage, msg)  # the status channel decodes to StatusMessage only


def _type(msg: DeviceMessage) -> str:
    return msg.TYPE
