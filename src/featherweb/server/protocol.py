"""Connection handling: one :class:`HttpProtocol` per TCP connection.

The protocol drives the parser, exposes each request to the application as an
ASGI ``scope``/``receive``/``send`` triple, and writes the response back with
the right framing. Requests on a connection are served one at a time; anything
pipelined behind the current one stays in the parser buffer until its turn.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, cast

from .._types import ASGIApp, Message, Scope
from .parser import (
    DEFAULT_LIMITS,
    BodyData,
    EndOfMessage,
    HttpParser,
    Limits,
    ParserError,
    RequestHead,
)
from .ws_protocol import WebSocketCycle, is_upgrade, subprotocols_of

__all__ = ["HttpProtocol", "RequestCycle", "ServerConfig", "ServerState"]

logger: Final = logging.getLogger("featherweb.server")

_DISCONNECT: Final[Message] = {"type": "http.disconnect"}

_DAYS: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS: Final = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)  # fmt: skip

_date_cache: tuple[int, bytes] = (0, b"")

_PHRASES: dict[int, bytes] = {
    200: b"OK",
    201: b"Created",
    204: b"No Content",
    301: b"Moved Permanently",
    302: b"Found",
    304: b"Not Modified",
    400: b"Bad Request",
    401: b"Unauthorized",
    403: b"Forbidden",
    404: b"Not Found",
    405: b"Method Not Allowed",
    408: b"Request Timeout",
    413: b"Content Too Large",
    414: b"URI Too Long",
    417: b"Expectation Failed",
    422: b"Unprocessable Content",
    431: b"Request Header Fields Too Large",
    500: b"Internal Server Error",
    501: b"Not Implemented",
    505: b"HTTP Version Not Supported",
}


def http_date(timestamp: float | None = None) -> bytes:
    """Current date as an IMF-fixdate, cached for a second."""
    global _date_cache
    seconds = int(time.time() if timestamp is None else timestamp)
    cached_seconds, cached_value = _date_cache
    if seconds == cached_seconds and cached_value:
        return cached_value
    now = time.gmtime(seconds)
    value = (
        f"{_DAYS[now.tm_wday]}, {now.tm_mday:02d} {_MONTHS[now.tm_mon - 1]} "
        f"{now.tm_year:04d} {now.tm_hour:02d}:{now.tm_min:02d}:{now.tm_sec:02d} GMT"
    ).encode("ascii")
    _date_cache = (seconds, value)
    return value


def reason_phrase(status: int) -> bytes:
    """Reason phrase for a status code; the uncommon ones are looked up lazily."""
    phrase = _PHRASES.get(status)
    if phrase is None:
        from http import HTTPStatus  # deferred: keeps the server import cheap

        try:
            phrase = HTTPStatus(status).phrase.encode("ascii")
        except ValueError:
            phrase = b""
        _PHRASES[status] = phrase
    return phrase


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Everything the connection layer needs to know, decided once at startup."""

    limits: Limits = DEFAULT_LIMITS
    #: Seconds allowed between the first byte of a request and the end of its headers.
    header_timeout: float = 10.0
    #: Seconds an idle kept-alive connection is held open.
    keep_alive_timeout: float = 5.0
    #: Reading is paused above this many buffered body bytes, resumed below the low mark.
    body_high_water: int = 64 * 1024
    body_low_water: int = 16 * 1024
    server_header: bytes | None = b"featherweb"
    date_header: bool = True
    root_path: str = ""
    #: Ceiling on one assembled WebSocket message.
    max_message_size: int = 16 * 1024 * 1024


class ServerState:
    """Shared between the runner and every connection it accepted."""

    __slots__ = ("connections", "drained", "should_exit", "tasks")

    def __init__(self) -> None:
        self.connections: set[HttpProtocol] = set()
        self.tasks: set[asyncio.Task[None]] = set()
        self.should_exit = False
        #: Set while no connection is open, so shutdown can wait for the last one.
        self.drained = asyncio.Event()

    def add(self, connection: HttpProtocol) -> None:
        self.connections.add(connection)
        self.drained.clear()

    def discard(self, connection: HttpProtocol) -> None:
        self.connections.discard(connection)
        if not self.connections:
            self.drained.set()


def _address(value: object) -> tuple[str, int] | None:
    if not isinstance(value, tuple):
        return None  # a unix socket has a path, not a host and a port
    parts = cast(tuple[Any, ...], value)
    if len(parts) < 2:
        return None
    return str(parts[0]), int(parts[1])


class HttpProtocol(asyncio.Protocol):
    """Reads requests off one connection and writes the responses back."""

    def __init__(
        self,
        app: ASGIApp,
        config: ServerConfig | None = None,
        *,
        state: ServerState | None = None,
    ) -> None:
        self.app = app
        self.config = config if config is not None else ServerConfig()
        self.state = state if state is not None else ServerState()
        self._loop = asyncio.get_running_loop()
        self._parser = HttpParser(self.config.limits)
        self._transport: asyncio.Transport | None = None
        self._cycle: RequestCycle | None = None
        #: Set once a request has been upgraded; from then on the bytes on this
        #: connection are frames, not requests.
        self._websocket: WebSocketCycle | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._read_paused = False
        self._writable = asyncio.Event()
        self._writable.set()
        self._client: tuple[str, int] | None = None
        self._server: tuple[str, int] | None = None
        self._scheme = "http"
        self._closing = False

    # -- asyncio.Protocol -----------------------------------------------

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = cast(asyncio.Transport, transport)
        self._client = _address(transport.get_extra_info("peername"))
        self._server = _address(transport.get_extra_info("sockname"))
        if transport.get_extra_info("sslcontext") is not None:
            self._scheme = "https"
        self.state.add(self)
        if self.state.should_exit:
            self._transport.close()
            return
        self._start_timer(self.config.keep_alive_timeout, self._on_idle_timeout)

    def data_received(self, data: bytes) -> None:
        if self._websocket is not None:
            self._websocket.feed_data(data)
            return
        if self._cycle is None:
            # A request is starting: slowloris gets the header timeout, not the idle one.
            self._start_timer(self.config.header_timeout, self._on_header_timeout)
        self._parser.feed_data(data)
        self._resume_parsing()

    def eof_received(self) -> bool:
        self._parser.feed_eof()
        cycle = self._cycle
        if cycle is None:
            return False  # nothing in flight, let the transport close itself
        self._closing = True  # no keep-alive past a half-closed connection
        self._resume_parsing()
        cycle = self._cycle
        if cycle is not None:
            # No more request data can arrive; buffered body is still delivered.
            cycle.on_disconnect()
        return True  # keep writing: the response may still be on its way

    def connection_lost(self, exc: Exception | None) -> None:
        self._cancel_timer()
        self.state.discard(self)
        self._transport = None
        self._writable.set()
        if self._websocket is not None:
            self._websocket.on_disconnect()
        if self._cycle is not None:
            self._cycle.on_disconnect()

    def pause_writing(self) -> None:
        self._writable.clear()

    def resume_writing(self) -> None:
        self._writable.set()

    # -- writing --------------------------------------------------------

    def write(self, data: bytes) -> None:
        transport = self._transport
        if transport is not None and not transport.is_closing():
            transport.write(data)

    async def drain(self) -> None:
        """Wait while the transport's write buffer is over its high-water mark."""
        await self._writable.wait()

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()

    def abort(self) -> None:
        if self._transport is not None:
            self._transport.abort()

    def shutdown(self) -> None:
        """Stop serving: close now when idle, after the current response otherwise."""
        self._closing = True
        if self._cycle is None:
            self._cancel_timer()
            self.close()

    @property
    def should_close(self) -> bool:
        return self._closing or self.state.should_exit

    # -- parsing --------------------------------------------------------

    def _resume_parsing(self) -> None:
        if self._transport is None:
            return
        try:
            while True:
                event = self._parser.next_event()
                if isinstance(event, RequestHead):
                    self._begin_request(event)
                elif isinstance(event, BodyData):
                    if self._cycle is not None:
                        self._cycle.feed_body(event.data)
                elif isinstance(event, EndOfMessage):
                    if self._cycle is not None:
                        self._cycle.feed_eom()
                else:
                    break
        except ParserError as exc:
            logger.debug("rejected request: %s", exc.message)
            self._fail(exc.status, exc.message)
            return
        self._update_read_pause()

    def _begin_request(self, head: RequestHead) -> None:
        self._cancel_timer()
        if is_upgrade(head):
            self._begin_websocket(head)
            return
        cycle = RequestCycle(self, head, self._build_scope(head))
        self._cycle = cycle
        task = self._loop.create_task(cycle.run(self.app))
        self.state.tasks.add(task)
        task.add_done_callback(self.state.tasks.discard)

    def _begin_websocket(self, head: RequestHead) -> None:
        """Hand the connection over to the frame layer for good."""
        cycle = WebSocketCycle(
            self,
            head,
            self._build_websocket_scope(head),
            max_message_size=self.config.max_message_size,
        )
        self._websocket = cycle
        self._closing = True  # there is no keep-alive after an upgrade
        task = self._loop.create_task(cycle.run(self.app))
        self.state.tasks.add(task)
        task.add_done_callback(self.state.tasks.discard)

    def _build_websocket_scope(self, head: RequestHead) -> Scope:
        scope = self._build_scope(head)
        scope["type"] = "websocket"
        scope["scheme"] = "wss" if self._scheme == "https" else "ws"
        scope["subprotocols"] = subprotocols_of(head)
        del scope["method"]
        return scope

    def _build_scope(self, head: RequestHead) -> Scope:
        return {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": head.http_version,
            "method": head.method,
            "scheme": self._scheme,
            "path": head.path,
            "raw_path": head.raw_path,
            "query_string": head.query_string,
            "root_path": self.config.root_path,
            "headers": head.headers,
            "client": self._client,
            "server": self._server,
        }

    @property
    def request_complete(self) -> bool:
        return self._parser.message_complete

    # -- read backpressure ----------------------------------------------

    def _update_read_pause(self) -> None:
        cycle = self._cycle
        transport = self._transport
        if cycle is None or transport is None or transport.is_closing():
            return
        if not self._read_paused and cycle.buffered_body >= self.config.body_high_water:
            transport.pause_reading()
            self._read_paused = True

    def maybe_resume_reading(self) -> None:
        if not self._read_paused:
            return
        cycle = self._cycle
        if cycle is not None and cycle.buffered_body > self.config.body_low_water:
            return
        transport = self._transport
        if transport is not None and not transport.is_closing():
            transport.resume_reading()
            self._read_paused = False

    # -- request lifecycle ----------------------------------------------

    def on_response_complete(self, cycle: RequestCycle) -> None:
        if self._cycle is not cycle:
            return
        self._cycle = None
        self.maybe_resume_reading()
        transport = self._transport
        if transport is None or transport.is_closing():
            return
        if not cycle.keep_alive or self.should_close or not self._parser.message_complete:
            transport.close()
            return
        self._parser.start_next_message()
        self._start_timer(self.config.keep_alive_timeout, self._on_idle_timeout)
        self._resume_parsing()  # a pipelined request may already be buffered

    def on_app_error(self, cycle: RequestCycle) -> None:
        if self._cycle is cycle:
            self._cycle = None
        if not cycle.response_started:
            self._fail(500, "internal server error")
        else:
            self.close()  # the response is half written, there is no honest way out

    def _fail(self, status: int, message: str) -> None:
        """Answer with a minimal response and close; used for errors only."""
        transport = self._transport
        if transport is None or transport.is_closing():
            return
        if self._cycle is not None and self._cycle.response_started:
            transport.close()
            return
        self._cycle = None
        body = message.encode("utf-8")
        head = [
            b"HTTP/1.1 ",
            str(status).encode("ascii"),
            b" ",
            reason_phrase(status),
            b"\r\n",
            b"content-type: text/plain; charset=utf-8\r\n",
            b"content-length: ",
            str(len(body)).encode("ascii"),
            b"\r\n",
            b"connection: close\r\n",
        ]
        if self.config.date_header:
            head += [b"date: ", http_date(), b"\r\n"]
        if self.config.server_header:
            head += [b"server: ", self.config.server_header, b"\r\n"]
        head.append(b"\r\n")
        transport.write(b"".join(head) + body)
        transport.close()

    # -- timers ---------------------------------------------------------

    def _start_timer(self, delay: float, callback: Callable[[], None]) -> None:
        self._cancel_timer()
        if delay > 0:
            self._timer = self._loop.call_later(delay, callback)

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _on_idle_timeout(self) -> None:
        self._timer = None
        if self._cycle is None:
            self.close()

    def _on_header_timeout(self) -> None:
        self._timer = None
        if self._cycle is None:
            self._fail(408, "request header timeout")


class RequestCycle:
    """One request/response exchange, exposed to the application as ASGI."""

    def __init__(self, protocol: HttpProtocol, head: RequestHead, scope: Scope) -> None:
        self.protocol = protocol
        self.head = head
        self.scope = scope
        self.keep_alive = head.keep_alive
        self.response_started = False
        self.response_complete = False
        self.body_complete = False
        self.disconnected = False
        self._body = bytearray()
        self._event = asyncio.Event()
        self._continue_sent = not head.expect_continue
        self._body_finished = False
        self._status = 200
        self._headers: list[tuple[bytes, bytes]] = []
        self._head_written = False
        self._chunked = False
        self._omit_body = False

    @property
    def buffered_body(self) -> int:
        return len(self._body)

    # -- fed by the protocol --------------------------------------------

    def feed_body(self, data: bytes) -> None:
        self._body += data
        self._event.set()

    def feed_eom(self) -> None:
        self.body_complete = True
        self._event.set()

    def on_disconnect(self) -> None:
        self.disconnected = True
        self._event.set()

    # -- ASGI -----------------------------------------------------------

    async def run(self, app: ASGIApp) -> None:
        try:
            await app(self.scope, self.receive, self.send)
        except Exception:
            logger.exception("unhandled exception in the ASGI application")
            self.protocol.on_app_error(self)
            return
        if not self.response_started:
            logger.error("the ASGI application returned without starting a response")
            self.protocol.on_app_error(self)
            return
        if not self.response_complete:
            await self.send({"type": "http.response.body", "body": b"", "more_body": False})

    async def receive(self) -> Message:
        if self.response_complete:
            return _DISCONNECT
        if not self._continue_sent:
            self._continue_sent = True
            self.protocol.write(b"HTTP/1.1 100 Continue\r\n\r\n")
        if self._body_finished:
            # The whole body was already delivered: from here on, only a
            # disconnect can happen.
            while not self.disconnected and not self.response_complete:
                self._event.clear()
                await self._event.wait()
            return _DISCONNECT
        while not self._body and not self.body_complete and not self.disconnected:
            self._event.clear()
            await self._event.wait()
        if not self._body and not self.body_complete:
            return _DISCONNECT  # gone before the body was complete
        data = bytes(self._body)
        self._body.clear()
        self.protocol.maybe_resume_reading()
        more_body = not self.body_complete
        self._body_finished = not more_body
        return {"type": "http.request", "body": data, "more_body": more_body}

    async def send(self, message: Message) -> None:
        if self.response_complete:
            raise RuntimeError("the response is already complete")
        message_type = str(message["type"])

        if not self.response_started:
            if message_type != "http.response.start":
                raise RuntimeError(f"expected 'http.response.start', got {message_type!r}")
            self.response_started = True
            self._status = int(message["status"])
            self._headers = [
                (bytes(name).lower(), bytes(value)) for name, value in message.get("headers") or ()
            ]
            return

        if message_type != "http.response.body":
            raise RuntimeError(f"expected 'http.response.body', got {message_type!r}")
        body = bytes(message.get("body") or b"")
        more_body = bool(message.get("more_body", False))

        if not self._head_written:
            self.protocol.write(self._build_head(len(body), more_body=more_body))
            self._head_written = True
        if body and not self._omit_body:
            if self._chunked:
                self.protocol.write(f"{len(body):x}\r\n".encode("ascii") + body + b"\r\n")
            else:
                self.protocol.write(body)
        if not more_body:
            if self._chunked and not self._omit_body:
                self.protocol.write(b"0\r\n\r\n")
            self.response_complete = True
            self._event.set()
            self.protocol.on_response_complete(self)
        await self.protocol.drain()

    # -- response framing -----------------------------------------------

    def _build_head(self, body_length: int, *, more_body: bool) -> bytes:
        config = self.protocol.config
        status = self._status
        version = self.head.http_version
        self._omit_body = self.head.method == "HEAD" or status < 200 or status in (204, 304)
        # RFC 9110: no Content-Length on 1xx/204, and a 304 mirrors the 200 it replaces.
        allow_content_length = status >= 200 and status not in (204, 304)

        fields: list[bytes] = []
        has_content_length = False
        has_date = False
        has_server = False
        for name, value in self._headers:
            if name == b"transfer-encoding":
                continue  # framing is the server's business, not the application's
            if name == b"content-length":
                if not allow_content_length:
                    continue
                has_content_length = True
            elif name == b"connection":
                if b"close" in value.lower():
                    self.keep_alive = False
                continue  # re-emitted below, with the decision actually applied
            elif name == b"date":
                has_date = True
            elif name == b"server":
                has_server = True
            fields.append(name + b": " + value + b"\r\n")

        if not has_content_length:
            if self._omit_body:
                if allow_content_length and not more_body:  # HEAD: length without body
                    fields.append(b"content-length: " + str(body_length).encode("ascii") + b"\r\n")
            elif not more_body:
                fields.append(b"content-length: " + str(body_length).encode("ascii") + b"\r\n")
            elif version == "1.1":
                self._chunked = True
                fields.append(b"transfer-encoding: chunked\r\n")
            else:
                self.keep_alive = False  # HTTP/1.0 streaming: framed by closing

        if not self.body_complete or self.protocol.should_close:
            # An unread request body would be mistaken for the next request.
            self.keep_alive = False
        if not self.keep_alive:
            fields.append(b"connection: close\r\n")
        elif version == "1.0":
            fields.append(b"connection: keep-alive\r\n")

        head = [b"HTTP/1.1 ", str(status).encode("ascii"), b" ", reason_phrase(status), b"\r\n"]
        if config.date_header and not has_date:
            head += [b"date: ", http_date(), b"\r\n"]
        if config.server_header and not has_server:
            head += [b"server: ", config.server_header, b"\r\n"]
        head += fields
        head.append(b"\r\n")
        return b"".join(head)
