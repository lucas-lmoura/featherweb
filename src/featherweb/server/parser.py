"""Incremental HTTP/1.1 request parser.

The parser is deliberately strict: whenever a message could be framed in more
than one way it is rejected instead of guessed at, because guessing is what
makes request smuggling possible.

It performs no I/O. Bytes go in through :meth:`HttpParser.feed_data`, events
come out of :meth:`HttpParser.next_event` until it answers ``NEED_DATA``::

    parser.feed_data(chunk)
    while True:
        event = parser.next_event()
        if event is NEED_DATA:
            break
        ...
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import Final
from urllib.parse import unquote

__all__ = [
    "DEFAULT_LIMITS",
    "NEED_DATA",
    "BodyData",
    "EndOfMessage",
    "Event",
    "HttpParser",
    "Limits",
    "ParserError",
    "RequestHead",
]

_TCHAR: Final = rb"!#$%&'*+\-.^_`|~0-9A-Za-z"
# request-line = method SP request-target SP HTTP-version
_REQUEST_LINE_RE: Final = re.compile(rb"^([" + _TCHAR + rb"]+) ([!-~\x80-\xff]+) HTTP/(\d)\.(\d)$")
# field-line = field-name ":" OWS field-value OWS; no space is allowed before the colon
_HEADER_LINE_RE: Final = re.compile(
    rb"^([" + _TCHAR + rb"]+):[ \t]*([\t\x20-\x7e\x80-\xff]*?)[ \t]*$"
)
_CHUNK_SIZE_RE: Final = re.compile(rb"^([0-9A-Fa-f]{1,16})(?:;[\t\x20-\x7e\x80-\xff]*)?$")
_DIGITS_RE: Final = re.compile(rb"^[0-9]{1,19}$")


class ParserError(Exception):
    """A malformed request; ``status`` is the response the server should send."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"{status} {message}")
        self.status = status
        self.message = message


@dataclass(frozen=True, slots=True)
class Limits:
    """Conservative caps applied while parsing a single request."""

    max_request_line: int = 8 * 1024
    max_headers_size: int = 64 * 1024
    max_header_count: int = 100
    max_chunk_line: int = 4 * 1024
    #: ``None`` leaves the body size to the application layer (bodies are streamed).
    max_body_size: int | None = None


DEFAULT_LIMITS: Final = Limits()


@dataclass(frozen=True, slots=True)
class RequestHead:
    """Request line plus headers, with the message framing already resolved."""

    method: str
    http_version: str
    path: str
    raw_path: bytes
    query_string: bytes
    headers: list[tuple[bytes, bytes]]
    keep_alive: bool = True
    expect_continue: bool = False
    chunked: bool = False
    content_length: int | None = None

    @property
    def has_body(self) -> bool:
        return self.chunked or bool(self.content_length)


@dataclass(frozen=True, slots=True)
class BodyData:
    """A piece of the request body, as it arrived."""

    data: bytes


@dataclass(frozen=True, slots=True)
class EndOfMessage:
    """The request body is complete."""


class _NeedData:
    """Singleton returned when the parser cannot make progress yet."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "NEED_DATA"


NEED_DATA: Final = _NeedData()

type Event = RequestHead | BodyData | EndOfMessage | _NeedData


class _State(Enum):
    REQUEST_LINE = auto()
    HEADERS = auto()
    BODY_LENGTH = auto()
    CHUNK_SIZE = auto()
    CHUNK_DATA = auto()
    CHUNK_CRLF = auto()
    TRAILERS = auto()
    COMPLETE = auto()
    ERROR = auto()


class HttpParser:
    """Parses the stream of HTTP/1.1 requests coming from one connection."""

    __slots__ = (
        "_body_size",
        "_buffer",
        "_chunk_remaining",
        "_eof",
        "_error",
        "_headers",
        "_headers_size",
        "_limits",
        "_method",
        "_remaining",
        "_started",
        "_state",
        "_target",
        "_version",
    )

    def __init__(self, limits: Limits = DEFAULT_LIMITS) -> None:
        self._limits = limits
        self._buffer = bytearray()
        self._eof = False
        self._error: ParserError | None = None
        self._state = _State.REQUEST_LINE
        self._reset_message()

    def _reset_message(self) -> None:
        self._headers: list[tuple[bytes, bytes]] = []
        self._headers_size = 0
        self._method = ""
        self._target = b""
        self._version = ""
        self._remaining = 0
        self._chunk_remaining = 0
        self._body_size = 0
        self._started = False

    @property
    def message_complete(self) -> bool:
        """True once the current request has been fully received."""
        return self._state is _State.COMPLETE

    @property
    def idle(self) -> bool:
        """True while waiting for the first byte of a request."""
        return self._state is _State.REQUEST_LINE and not self._started

    def feed_data(self, data: bytes) -> None:
        self._buffer += data

    def feed_eof(self) -> None:
        self._eof = True

    def start_next_message(self) -> None:
        """Get ready for the next request on a kept-alive connection."""
        if self._state is not _State.COMPLETE:
            raise RuntimeError("the current request is not complete")
        self._state = _State.REQUEST_LINE
        self._reset_message()

    def next_event(self) -> Event:
        if self._error is not None:
            raise self._error
        try:
            while True:
                event = self._step()
                if event is not None:
                    return event
        except ParserError as exc:
            self._state = _State.ERROR
            self._error = exc
            raise

    # -- states ---------------------------------------------------------

    def _step(self) -> Event | None:
        state = self._state
        if state is _State.REQUEST_LINE:
            return self._step_request_line()
        if state is _State.HEADERS:
            return self._step_headers()
        if state is _State.BODY_LENGTH:
            return self._step_body_length()
        if state is _State.CHUNK_SIZE:
            return self._step_chunk_size()
        if state is _State.CHUNK_DATA:
            return self._step_chunk_data()
        if state is _State.CHUNK_CRLF:
            return self._step_chunk_crlf()
        if state is _State.TRAILERS:
            return self._step_trailers()
        return NEED_DATA  # COMPLETE: nothing more until start_next_message()

    def _step_request_line(self) -> Event | None:
        if not self._started:
            # RFC 9112: a server should ignore an empty line before the request line.
            if self._buffer.startswith(b"\r\n"):
                del self._buffer[:2]
            if not self._buffer:
                return self._need_data(allow_eof=True)
        line = self._read_line(self._limits.max_request_line, 414, "request line too long")
        if line is None:
            return self._need_data(allow_eof=not self._started)
        self._started = True
        match = _REQUEST_LINE_RE.match(line)
        if match is None:
            raise ParserError(400, "malformed request line")
        method, target, major, minor = match.groups()
        if major != b"1" or minor not in (b"0", b"1"):
            raise ParserError(505, "unsupported HTTP version")
        self._method = method.decode("ascii")
        if self._method == "CONNECT":
            raise ParserError(501, "CONNECT is not supported")
        self._target = target
        self._version = "1." + minor.decode("ascii")
        self._state = _State.HEADERS
        return None

    def _step_headers(self) -> Event | None:
        remaining = self._limits.max_headers_size - self._headers_size
        line = self._read_line(remaining, 431, "request header fields too large")
        if line is None:
            return self._need_data()
        self._headers_size += len(line) + 2
        if not line:
            return self._finish_headers()
        if line[:1] in (b" ", b"\t"):
            raise ParserError(400, "obsolete line folding is not allowed")
        match = _HEADER_LINE_RE.match(line)
        if match is None:
            raise ParserError(400, "malformed header field")
        if len(self._headers) >= self._limits.max_header_count:
            raise ParserError(431, "too many header fields")
        name, value = match.groups()
        self._headers.append((name.lower(), value))
        return None

    def _step_body_length(self) -> Event | None:
        if self._remaining == 0:
            self._state = _State.COMPLETE
            return EndOfMessage()
        if not self._buffer:
            return self._need_data()
        take = min(len(self._buffer), self._remaining)
        data = bytes(self._buffer[:take])
        del self._buffer[:take]
        self._remaining -= take
        return BodyData(data)

    def _step_chunk_size(self) -> Event | None:
        line = self._read_line(self._limits.max_chunk_line, 400, "chunk size line too long")
        if line is None:
            return self._need_data()
        match = _CHUNK_SIZE_RE.match(line)
        if match is None:
            raise ParserError(400, "malformed chunk size")
        size = int(match.group(1), 16)
        if size == 0:
            self._state = _State.TRAILERS
            return None
        self._account_body(size)
        self._chunk_remaining = size
        self._state = _State.CHUNK_DATA
        return None

    def _step_chunk_data(self) -> Event | None:
        if self._chunk_remaining == 0:
            self._state = _State.CHUNK_CRLF
            return None
        if not self._buffer:
            return self._need_data()
        take = min(len(self._buffer), self._chunk_remaining)
        data = bytes(self._buffer[:take])
        del self._buffer[:take]
        self._chunk_remaining -= take
        return BodyData(data)

    def _step_chunk_crlf(self) -> Event | None:
        line = self._read_line(2, 400, "chunk data not terminated by CRLF")
        if line is None:
            return self._need_data()
        if line:
            raise ParserError(400, "chunk data not terminated by CRLF")
        self._state = _State.CHUNK_SIZE
        return None

    def _step_trailers(self) -> Event | None:
        remaining = self._limits.max_headers_size - self._headers_size
        line = self._read_line(remaining, 431, "request header fields too large")
        if line is None:
            return self._need_data()
        self._headers_size += len(line) + 2
        if not line:
            self._state = _State.COMPLETE
            return EndOfMessage()
        # Trailers are validated and dropped: nothing in the framework reads them.
        if _HEADER_LINE_RE.match(line) is None:
            raise ParserError(400, "malformed trailer field")
        return None

    # -- helpers --------------------------------------------------------

    def _need_data(self, *, allow_eof: bool = False) -> Event:
        if self._eof and not allow_eof:
            raise ParserError(400, "connection closed in the middle of a request")
        return NEED_DATA

    def _read_line(self, max_size: int, status: int, message: str) -> bytes | None:
        """Read one CRLF-terminated line; a bare LF is rejected."""
        index = self._buffer.find(b"\n")
        if index == -1:
            if len(self._buffer) > max_size:
                raise ParserError(status, message)
            return None
        if index > max_size:
            raise ParserError(status, message)
        line = bytes(self._buffer[:index])
        del self._buffer[: index + 1]
        if not line.endswith(b"\r"):
            raise ParserError(400, "line not terminated by CRLF")
        return line[:-1]

    def _account_body(self, size: int) -> None:
        limit = self._limits.max_body_size
        self._body_size += size
        if limit is not None and self._body_size > limit:
            raise ParserError(413, "request body too large")

    def _finish_headers(self) -> RequestHead:
        headers = self._headers
        version = self._version
        content_lengths: list[bytes] = []
        transfer_encodings: list[bytes] = []
        connection_tokens: set[bytes] = set()
        expects: list[bytes] = []
        hosts = 0
        for name, value in headers:
            if name == b"content-length":
                content_lengths.append(value)
            elif name == b"transfer-encoding":
                transfer_encodings.append(value)
            elif name == b"connection":
                connection_tokens.update(
                    token.strip().lower() for token in value.split(b",") if token.strip()
                )
            elif name == b"host":
                hosts += 1
            elif name == b"expect":
                expects.append(value)

        if hosts > 1:
            raise ParserError(400, "duplicate Host header")
        if hosts == 0 and version == "1.1":
            raise ParserError(400, "missing Host header")

        chunked = False
        content_length: int | None = None
        if transfer_encodings:
            # Both framings at once is the classic smuggling vector.
            if content_lengths:
                raise ParserError(400, "Content-Length and Transfer-Encoding are exclusive")
            if len(transfer_encodings) > 1:
                raise ParserError(400, "duplicate Transfer-Encoding header")
            if transfer_encodings[0].strip().lower() != b"chunked":
                raise ParserError(501, "unsupported transfer coding")
            if version == "1.0":
                raise ParserError(400, "Transfer-Encoding is not allowed in HTTP/1.0")
            chunked = True
        elif content_lengths:
            if len(content_lengths) > 1:
                raise ParserError(400, "duplicate Content-Length header")
            raw_length = content_lengths[0]
            if _DIGITS_RE.match(raw_length) is None:
                raise ParserError(400, "malformed Content-Length")
            content_length = int(raw_length)
            self._account_body(content_length)

        expect_continue = False
        if expects:
            if len(expects) > 1 or expects[0].strip().lower() != b"100-continue":
                raise ParserError(417, "unsupported expectation")
            expect_continue = version == "1.1"

        if version == "1.1":
            keep_alive = b"close" not in connection_tokens
        else:
            keep_alive = b"keep-alive" in connection_tokens

        raw_path, query_string = self._split_target()
        try:
            path = unquote(raw_path.decode("ascii"))
        except UnicodeDecodeError:
            raise ParserError(400, "request target must be ASCII") from None

        if chunked:
            self._state = _State.CHUNK_SIZE
        else:
            self._remaining = content_length or 0
            self._state = _State.BODY_LENGTH

        return RequestHead(
            method=self._method,
            http_version=version,
            path=path,
            raw_path=raw_path,
            query_string=query_string,
            headers=headers,
            keep_alive=keep_alive,
            expect_continue=expect_continue,
            chunked=chunked,
            content_length=content_length,
        )

    def _split_target(self) -> tuple[bytes, bytes]:
        target = self._target
        if target.startswith(b"/"):
            raw_path, _, query = target.partition(b"?")
            return raw_path, query
        if target == b"*":
            return b"*", b""
        scheme, separator, rest = target.partition(b"://")
        if separator and scheme.lower() in (b"http", b"https"):
            slash = rest.find(b"/")
            if slash == -1:
                return b"/", b""
            raw_path, _, query = rest[slash:].partition(b"?")
            return raw_path, query
        raise ParserError(400, "unsupported request target")
