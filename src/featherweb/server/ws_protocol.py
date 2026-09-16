"""WebSocket framing, RFC 6455.

Two layers, kept apart on purpose:

* :class:`FrameParser` turns bytes into frames. It is incremental over one
  ``bytearray`` and strict about everything the RFC calls a protocol error —
  an unmasked client frame, a reserved bit, a fragmented control frame — because
  each of those is either a broken peer or someone probing.
* :class:`WebSocketCycle` turns frames into the ASGI websocket protocol:
  ``websocket.connect``/``receive``/``disconnect`` going up, ``accept``/``send``
  /``close`` coming down. Fragments are joined into whole messages, and ping is
  answered with pong without the application hearing about it.

The handshake itself is :func:`handshake_response`, which the HTTP protocol
calls when a request asks to upgrade.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

from .._types import ASGIApp, Message, Scope
from .parser import RequestHead

__all__ = [
    "FrameParser",
    "WebSocketCycle",
    "WebSocketProtocolError",
    "is_upgrade",
]

#: RFC 6455 §1.3: the constant every accept token is derived from.
_GUID: Final = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION: Final = 0x0
OP_TEXT: Final = 0x1
OP_BINARY: Final = 0x2
OP_CLOSE: Final = 0x8
OP_PING: Final = 0x9
OP_PONG: Final = 0xA
_DATA_OPCODES: Final = frozenset({OP_CONTINUATION, OP_TEXT, OP_BINARY})
_CONTROL_OPCODES: Final = frozenset({OP_CLOSE, OP_PING, OP_PONG})

#: Close codes this module sends itself.
CLOSE_NORMAL: Final = 1000
CLOSE_GOING_AWAY: Final = 1001
CLOSE_PROTOCOL_ERROR: Final = 1002
CLOSE_INVALID_PAYLOAD: Final = 1007
CLOSE_TOO_LARGE: Final = 1009
CLOSE_INTERNAL_ERROR: Final = 1011
#: Never sent on the wire; the RFC reserves it for "no code was given".
CLOSE_NO_STATUS: Final = 1005
CLOSE_ABNORMAL: Final = 1006

#: Control frames carry at most this much, per §5.5.
_MAX_CONTROL_PAYLOAD: Final = 125
#: Default ceiling for one assembled message.
DEFAULT_MAX_MESSAGE_SIZE: Final = 16 * 1024 * 1024


class WebSocketProtocolError(Exception):
    """The peer broke the framing rules; carries the close code to answer with."""

    def __init__(self, reason: str, code: int = CLOSE_PROTOCOL_ERROR) -> None:
        self.code = code
        self.reason = reason
        super().__init__(reason)


class Frame:
    """One frame, with the mask already removed."""

    __slots__ = ("fin", "opcode", "payload")

    def __init__(self, opcode: int, payload: bytes, *, fin: bool) -> None:
        self.opcode = opcode
        self.payload = payload
        self.fin = fin

    @property
    def is_control(self) -> bool:
        return self.opcode in _CONTROL_OPCODES

    def __repr__(self) -> str:
        return f"Frame(opcode={self.opcode:#x}, {len(self.payload)} bytes, fin={self.fin})"


class FrameParser:
    """Bytes in, frames out, one buffer and no copies until a frame is whole."""

    __slots__ = ("_buffer", "_max_frame_size")

    def __init__(self, *, max_frame_size: int = DEFAULT_MAX_MESSAGE_SIZE) -> None:
        self._buffer = bytearray()
        self._max_frame_size = max_frame_size

    def feed_data(self, data: bytes) -> None:
        self._buffer += data

    def next_frame(self) -> Frame | None:
        """The next whole frame, or ``None`` while one is still arriving."""
        buffer = self._buffer
        if len(buffer) < 2:
            return None
        first, second = buffer[0], buffer[1]
        fin = bool(first & 0x80)
        if first & 0x70:
            raise WebSocketProtocolError("reserved bits must be zero")
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        offset = 2

        if length == 126:
            if len(buffer) < offset + 2:
                return None
            length = int.from_bytes(buffer[offset : offset + 2], "big")
            offset += 2
        elif length == 127:
            if len(buffer) < offset + 8:
                return None
            length = int.from_bytes(buffer[offset : offset + 8], "big")
            if length >> 63:
                raise WebSocketProtocolError("the top bit of a 64-bit length must be zero")
            offset += 8

        self._check_frame(opcode, length, masked=masked, fin=fin)

        mask = b""
        if masked:
            if len(buffer) < offset + 4:
                return None
            mask = bytes(buffer[offset : offset + 4])
            offset += 4
        if len(buffer) < offset + length:
            return None

        payload = bytes(buffer[offset : offset + length])
        del buffer[: offset + length]
        if mask:
            payload = _unmask(payload, mask)
        return Frame(opcode, payload, fin=fin)

    def _check_frame(self, opcode: int, length: int, *, masked: bool, fin: bool) -> None:
        if not masked:
            # §5.1: a client that sends an unmasked frame must be cut off.
            raise WebSocketProtocolError("frames from a client must be masked")
        if opcode not in _DATA_OPCODES and opcode not in _CONTROL_OPCODES:
            raise WebSocketProtocolError(f"unknown opcode {opcode:#x}")
        if opcode in _CONTROL_OPCODES:
            if length > _MAX_CONTROL_PAYLOAD:
                raise WebSocketProtocolError("a control frame cannot carry more than 125 bytes")
            if not fin:
                raise WebSocketProtocolError("a control frame cannot be fragmented")
        elif length > self._max_frame_size:
            raise WebSocketProtocolError("the frame is too large", CLOSE_TOO_LARGE)


def _unmask(payload: bytes, mask: bytes) -> bytes:
    """XOR the payload with the repeating mask, in one pass over ints."""
    if not payload:
        return payload
    repeated = (mask * (len(payload) // 4 + 1))[: len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(
        len(payload), "big"
    )


def build_frame(opcode: int, payload: bytes = b"", *, fin: bool = True) -> bytes:
    """One frame to send; a server never masks what it sends (§5.1)."""
    head = bytearray(2)
    head[0] = (0x80 if fin else 0x00) | opcode
    length = len(payload)
    if length < 126:
        head[1] = length
    elif length < 1 << 16:
        head[1] = 126
        head += length.to_bytes(2, "big")
    else:
        head[1] = 127
        head += length.to_bytes(8, "big")
    return bytes(head) + payload


def close_payload(code: int, reason: str = "") -> bytes:
    """The body of a close frame: the code, then the reason as UTF-8."""
    if code in (CLOSE_NO_STATUS, CLOSE_ABNORMAL):
        return b""  # these two mean "nothing was sent", so nothing is
    return code.to_bytes(2, "big") + reason.encode("utf-8")[:123]


def parse_close(payload: bytes) -> tuple[int, str]:
    """The code and reason inside a close frame."""
    if not payload:
        return CLOSE_NO_STATUS, ""
    if len(payload) == 1:
        raise WebSocketProtocolError("a close payload of one byte is malformed")
    code = int.from_bytes(payload[:2], "big")
    if not _valid_close_code(code):
        raise WebSocketProtocolError(f"close code {code} is not allowed on the wire")
    try:
        reason = payload[2:].decode("utf-8")
    except UnicodeDecodeError:
        raise WebSocketProtocolError(
            "the close reason is not UTF-8", CLOSE_INVALID_PAYLOAD
        ) from None
    return code, reason


def _valid_close_code(code: int) -> bool:
    """§7.4: which codes a peer may actually put in a close frame."""
    if 3000 <= code <= 4999:  # registered and private use
        return True
    if code in (CLOSE_NO_STATUS, CLOSE_ABNORMAL, 1015):
        return False  # reserved: they describe a state, they are never sent
    return 1000 <= code <= 1014


# -- the handshake ----------------------------------------------------------


def is_upgrade(head: RequestHead) -> bool:
    """Whether this request is asking to become a WebSocket."""
    if head.method != "GET":
        return False
    values = dict(head.headers)
    upgrade = values.get(b"upgrade", b"").lower()
    connection = values.get(b"connection", b"").lower()
    return upgrade == b"websocket" and b"upgrade" in connection


def handshake_response(head: RequestHead, subprotocol: str | None = None) -> bytes:
    """The 101 that completes the upgrade, or the 400 that refuses it."""
    values = dict(head.headers)
    key = values.get(b"sec-websocket-key", b"")
    version = values.get(b"sec-websocket-version", b"")
    if version != b"13":
        # §4.2.2: tell the client which version we do speak.
        return _refuse(b"426 Upgrade Required", b"sec-websocket-version: 13\r\n")
    if not _valid_key(key):
        return _refuse(b"400 Bad Request")
    lines = [
        b"HTTP/1.1 101 Switching Protocols\r\n",
        b"upgrade: websocket\r\n",
        b"connection: Upgrade\r\n",
        b"sec-websocket-accept: " + accept_token(key) + b"\r\n",
    ]
    if subprotocol:
        lines.append(b"sec-websocket-protocol: " + subprotocol.encode("latin-1") + b"\r\n")
    lines.append(b"\r\n")
    return b"".join(lines)


def _refuse(status: bytes, extra: bytes = b"") -> bytes:
    return (
        b"HTTP/1.1 " + status + b"\r\nconnection: close\r\ncontent-length: 0\r\n" + extra + b"\r\n"
    )


def _valid_key(key: bytes) -> bool:
    """§4.1: 16 bytes, base64-encoded, so 24 characters that decode cleanly."""
    from base64 import b64decode

    if len(key) != 24:
        return False
    try:
        return len(b64decode(key, validate=True)) == 16
    except Exception:
        return False


def accept_token(key: bytes) -> bytes:
    """``base64(sha1(key + GUID))``, the proof that we read the request."""
    from base64 import b64encode
    from hashlib import sha1

    return b64encode(sha1(key + _GUID).digest())  # sha1 is what the RFC specifies


def subprotocols_of(head: RequestHead) -> list[str]:
    for name, value in head.headers:
        if name == b"sec-websocket-protocol":
            return [item.strip() for item in value.decode("latin-1").split(",") if item.strip()]
    return []


# -- ASGI over the frames ---------------------------------------------------


class WebSocketCycle:
    """One upgraded connection, exposed to the application as ASGI."""

    def __init__(
        self,
        protocol: Any,
        head: RequestHead,
        scope: Scope,
        *,
        max_message_size: int = DEFAULT_MAX_MESSAGE_SIZE,
    ) -> None:
        self.protocol = protocol
        self.head = head
        self.scope = scope
        self.accepted = False
        self.closed = False
        self._max_message_size = max_message_size
        self._parser = FrameParser(max_frame_size=max_message_size)
        self._incoming: asyncio.Queue[Message] = asyncio.Queue()
        self._fragments = bytearray()
        self._fragment_opcode = 0
        self._handshake_sent = False
        self._connect_sent = False
        self._disconnected = False

    # -- fed by the connection ------------------------------------------

    def feed_data(self, data: bytes) -> None:
        """Parse whatever arrived, queueing the messages it completed."""
        self._parser.feed_data(data)
        try:
            while True:
                frame = self._parser.next_frame()
                if frame is None:
                    return
                self._handle_frame(frame)
                if self.closed:
                    return
        except WebSocketProtocolError as exc:
            self.fail(exc.code, exc.reason)

    def on_disconnect(self, code: int = CLOSE_ABNORMAL) -> None:
        """The transport is gone; let the application's receive() see it."""
        if self._disconnected:
            return
        self._disconnected = True
        self.closed = True
        self._incoming.put_nowait({"type": "websocket.disconnect", "code": code})

    def _handle_frame(self, frame: Frame) -> None:
        if frame.is_control:
            self._handle_control(frame)
            return
        if frame.opcode == OP_CONTINUATION:
            if not self._fragment_opcode:
                raise WebSocketProtocolError("a continuation frame with nothing to continue")
        else:
            if self._fragment_opcode:
                raise WebSocketProtocolError("a new message started before the last one finished")
            self._fragment_opcode = frame.opcode
        self._fragments += frame.payload
        if len(self._fragments) > self._max_message_size:
            raise WebSocketProtocolError("the message is too large", CLOSE_TOO_LARGE)
        if not frame.fin:
            return
        payload = bytes(self._fragments)
        opcode = self._fragment_opcode
        self._fragments.clear()
        self._fragment_opcode = 0
        self._incoming.put_nowait(self._message_for(opcode, payload))

    def _message_for(self, opcode: int, payload: bytes) -> Message:
        if opcode == OP_BINARY:
            return {"type": "websocket.receive", "bytes": payload}
        try:
            return {"type": "websocket.receive", "text": payload.decode("utf-8")}
        except UnicodeDecodeError:
            # §8.1: a text message that is not valid UTF-8 ends the connection.
            raise WebSocketProtocolError(
                "a text message must be valid UTF-8", CLOSE_INVALID_PAYLOAD
            ) from None

    def _handle_control(self, frame: Frame) -> None:
        if frame.opcode == OP_PING:
            self._write(build_frame(OP_PONG, frame.payload))
            return
        if frame.opcode == OP_PONG:
            return  # unsolicited pongs are allowed and mean nothing to us
        code, reason = parse_close(frame.payload)
        self._echo_close(code, reason)

    def _echo_close(self, code: int, reason: str) -> None:
        """Answer the peer's close with our own and let the application know."""
        if not self.closed:
            self.closed = True
            echoed = CLOSE_NORMAL if code == CLOSE_NO_STATUS else code
            self._write(build_frame(OP_CLOSE, close_payload(echoed, reason)))
        self._incoming.put_nowait({"type": "websocket.disconnect", "code": code, "reason": reason})
        self._disconnected = True
        self.protocol.close()

    def fail(self, code: int, reason: str) -> None:
        """Close because the peer broke the protocol."""
        if not self.closed:
            self.closed = True
            self._write(build_frame(OP_CLOSE, close_payload(code, reason)))
        self.on_disconnect(code)
        self.protocol.close()

    def _write(self, data: bytes) -> None:
        self.protocol.write(data)

    # -- ASGI -----------------------------------------------------------

    async def run(self, app: ASGIApp) -> None:
        import logging

        try:
            await app(self.scope, self.receive, self.send)
        except Exception:
            logging.getLogger("featherweb.server").exception("websocket application failed")
            if not self.closed:
                self.fail(CLOSE_INTERNAL_ERROR, "internal error")
        finally:
            if not self._handshake_sent:
                # The application never accepted, so the upgrade never happened.
                self._write(_refuse(b"403 Forbidden"))
            self.protocol.close()

    async def receive(self) -> Message:
        if not self._connect_sent:
            self._connect_sent = True
            return {"type": "websocket.connect"}
        return await self._incoming.get()

    async def send(self, message: Message) -> None:
        kind = message["type"]
        if kind == "websocket.accept":
            self._accept(message)
        elif kind == "websocket.send":
            self._send_payload(message)
        elif kind == "websocket.close":
            self._close(message)
        else:
            raise RuntimeError(f"unexpected websocket message {kind!r}")

    def _accept(self, message: Message) -> None:
        if self._handshake_sent:
            raise RuntimeError("this websocket has already been accepted")
        subprotocol = message.get("subprotocol")
        self._write(handshake_response(self.head, subprotocol))
        self._handshake_sent = True
        self.accepted = True

    def _send_payload(self, message: Message) -> None:
        if not self.accepted:
            raise RuntimeError("accept the websocket before sending on it")
        if self.closed:
            return  # the peer is gone; dropping it beats raising into the handler
        text = message.get("text")
        if text is not None:
            self._write(build_frame(OP_TEXT, str(text).encode("utf-8")))
            return
        data = message.get("bytes")
        if data is not None:
            self._write(build_frame(OP_BINARY, bytes(data)))

    def _close(self, message: Message) -> None:
        code = int(message.get("code", CLOSE_NORMAL))
        reason = str(message.get("reason", ""))
        if not self._handshake_sent:
            # Closing before accepting is how ASGI spells "refuse the handshake".
            self._write(_refuse(b"403 Forbidden"))
            self._handshake_sent = True
            self.closed = True
            self.protocol.close()
            return
        if self.closed:
            return
        self.closed = True
        self._write(build_frame(OP_CLOSE, close_payload(code, reason)))
        # close() flushes what is already buffered, so the close frame still goes out.
        self.protocol.close()
