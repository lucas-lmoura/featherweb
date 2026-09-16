"""The WebSocket a handler is given.

It speaks ASGI, so the same handler runs on the bundled server and on uvicorn:
what arrives is ``websocket.connect``, ``websocket.receive`` and
``websocket.disconnect``, and what goes out is ``websocket.accept``,
``websocket.send`` and ``websocket.close``. Framing, masking and ping/pong
happen a layer below, in whichever server is running.

A connection has to be accepted before anything can be sent over it, and the
state is tracked rather than assumed: sending before ``accept()``, or after a
close, is a mistake in the handler and says so instead of hanging.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Any, Final

from ._types import Message, Receive, Scope, Send
from .request import Connection

__all__ = ["WebSocket", "WebSocketDisconnect", "WebSocketStateError"]

#: Normal closure, per RFC 6455 §7.4.1.
CLOSE_NORMAL: Final = 1000
#: The handler refused the payload it was given.
CLOSE_UNSUPPORTED_DATA: Final = 1003
#: The payload was not what the handler asked for, e.g. text that is not JSON.
CLOSE_INVALID_DATA: Final = 1007
#: The message was larger than the connection allows.
CLOSE_TOO_LARGE: Final = 1009


class WebSocketDisconnect(Exception):
    """The other end went away.

    Raised by every receive once the connection is gone, so a handler that
    loops over messages ends by catching this rather than by checking a flag.
    """

    def __init__(self, code: int = CLOSE_NORMAL, reason: str = "") -> None:
        self.code = code
        self.reason = reason
        super().__init__(f"disconnected with code {code}" + (f": {reason}" if reason else ""))


class WebSocketStateError(RuntimeError):
    """The handler did something the connection's state does not allow."""


class WebSocket(Connection):
    """One WebSocket connection, from the handshake to the close."""

    __slots__ = ("_accepted", "_client_closed", "_closed", "_connected", "_receive", "_send")

    def __init__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        path_params: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(scope, path_params=path_params)
        self._receive = receive
        self._send = send
        self._connected = False
        self._accepted = False
        self._closed = False
        self._client_closed = False

    # -- the handshake --------------------------------------------------

    @property
    def subprotocols(self) -> list[str]:
        """What the client offered in ``Sec-WebSocket-Protocol``."""
        return [str(item) for item in self.scope.get("subprotocols") or ()]

    @property
    def accepted(self) -> bool:
        return self._accepted

    @property
    def closed(self) -> bool:
        """Whether either end has closed the connection."""
        return self._closed or self._client_closed

    async def accept(
        self,
        *,
        subprotocol: str | None = None,
        headers: Iterable[tuple[str, str]] = (),
    ) -> None:
        """Complete the handshake, optionally choosing a subprotocol."""
        if self._accepted:
            raise WebSocketStateError("this connection has already been accepted")
        if self._closed:
            raise WebSocketStateError("this connection is closed")
        await self._await_connect()
        message: Message = {"type": "websocket.accept"}
        if subprotocol is not None:
            message["subprotocol"] = subprotocol
        raw = [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in headers]
        if raw:
            message["headers"] = raw
        await self._send(message)
        self._accepted = True

    async def _await_connect(self) -> None:
        """Take the ``websocket.connect`` the server opens with, once."""
        if self._connected:
            return
        message = await self._receive()
        kind = message["type"]
        if kind == "websocket.connect":
            self._connected = True
            return
        if kind == "websocket.disconnect":
            self._client_closed = True
            raise WebSocketDisconnect(int(message.get("code", CLOSE_NORMAL)))
        raise WebSocketStateError(f"expected websocket.connect, got {kind!r}")

    # -- receiving ------------------------------------------------------

    async def receive(self) -> Message:
        """The next raw ASGI message; a disconnect is raised, not returned."""
        if not self._accepted:
            raise WebSocketStateError("accept() the connection before receiving from it")
        if self.closed:
            raise WebSocketDisconnect(CLOSE_NORMAL)
        message = await self._receive()
        if message["type"] == "websocket.disconnect":
            self._client_closed = True
            raise WebSocketDisconnect(
                int(message.get("code", CLOSE_NORMAL)), str(message.get("reason", ""))
            )
        return message

    async def receive_text(self) -> str:
        message = await self.receive()
        text = message.get("text")
        if text is None:
            await self.close(CLOSE_UNSUPPORTED_DATA, "expected a text message")
            raise WebSocketDisconnect(CLOSE_UNSUPPORTED_DATA, "expected a text message")
        return str(text)

    async def receive_bytes(self) -> bytes:
        message = await self.receive()
        data = message.get("bytes")
        if data is None:
            await self.close(CLOSE_UNSUPPORTED_DATA, "expected a binary message")
            raise WebSocketDisconnect(CLOSE_UNSUPPORTED_DATA, "expected a binary message")
        return bytes(data)

    async def receive_json(self) -> Any:
        """The next message, decoded as JSON; bad JSON closes with 1007."""
        from ._compat import json_loads

        message = await self.receive()
        raw = message.get("text")
        payload = raw.encode("utf-8") if isinstance(raw, str) else message.get("bytes")
        if payload is None:
            await self.close(CLOSE_UNSUPPORTED_DATA, "expected a message with a payload")
            raise WebSocketDisconnect(CLOSE_UNSUPPORTED_DATA, "expected a message with a payload")
        try:
            return json_loads(payload)
        except ValueError:
            await self.close(CLOSE_INVALID_DATA, "expected JSON")
            raise WebSocketDisconnect(CLOSE_INVALID_DATA, "expected JSON") from None

    # -- sending --------------------------------------------------------

    async def send_text(self, data: str) -> None:
        await self._send_message({"type": "websocket.send", "text": data})

    async def send_bytes(self, data: bytes) -> None:
        await self._send_message({"type": "websocket.send", "bytes": data})

    async def send_json(self, data: Any) -> None:
        from ._compat import json_dumps

        await self._send_message(
            {"type": "websocket.send", "text": json_dumps(data).decode("utf-8")}
        )

    async def _send_message(self, message: Message) -> None:
        if not self._accepted:
            raise WebSocketStateError("accept() the connection before sending on it")
        if self.closed:
            raise WebSocketDisconnect(CLOSE_NORMAL)
        await self._send(message)

    # -- iterating ------------------------------------------------------

    async def iter_text(self) -> AsyncIterator[str]:
        """Every text message until the other end goes away."""
        try:
            while True:
                yield await self.receive_text()
        except WebSocketDisconnect:
            return

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        try:
            while True:
                yield await self.receive_bytes()
        except WebSocketDisconnect:
            return

    async def iter_json(self) -> AsyncIterator[Any]:
        try:
            while True:
                yield await self.receive_json()
        except WebSocketDisconnect:
            return

    # -- ending it ------------------------------------------------------

    async def close(self, code: int = CLOSE_NORMAL, reason: str = "") -> None:
        """Close the connection; closing one that is already closed does nothing."""
        if self._closed or self._client_closed:
            return
        self._closed = True
        await self._send({"type": "websocket.close", "code": code, "reason": reason})

    async def deny(self, reason: str = "") -> None:
        """Refuse the handshake before accepting it.

        Closing a connection that was never accepted is what ASGI defines as
        rejecting the handshake, so the client sees an HTTP error rather than an
        upgrade. Plain ``websocket.close``, not the optional response extension,
        so it behaves the same on every server.
        """
        if self._accepted:
            raise WebSocketStateError("this connection has already been accepted")
        await self._await_connect()
        await self.close(CLOSE_NORMAL, reason)

    def __repr__(self) -> str:
        state = "closed" if self.closed else ("open" if self._accepted else "connecting")
        return f"<WebSocket {self.path} {state}>"
