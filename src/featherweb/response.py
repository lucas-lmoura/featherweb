"""The outgoing response: body, status, headers and cookies."""

from __future__ import annotations

from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
)
from typing import Any, ClassVar, Final, Literal, cast

from ._types import Send

__all__ = [
    "FileResponse",
    "MutableHeaders",
    "RedirectResponse",
    "Response",
    "StreamingResponse",
]

#: Statuses that must not carry a body, and therefore no Content-Length either.
_BODYLESS: Final = frozenset({204, 304})
_EPOCH: Final = "Thu, 01 Jan 1970 00:00:00 GMT"


class MutableHeaders(MutableMapping[str, str]):
    """Case-insensitive headers that keep repeated fields."""

    __slots__ = ("_items",)

    def __init__(self, initial: Mapping[str, str] | None = None) -> None:
        self._items: list[tuple[str, str]] = []
        if initial:
            for name, value in initial.items():
                self._items.append((name.lower(), value))

    def __getitem__(self, key: str) -> str:
        key = key.lower()
        for name, value in self._items:
            if name == key:
                return value
        raise KeyError(key)

    def __setitem__(self, key: str, value: str) -> None:
        """Replace every value for ``key`` with this one."""
        key = key.lower()
        items: list[tuple[str, str]] = []
        replaced = False
        for name, existing in self._items:
            if name != key:
                items.append((name, existing))
            elif not replaced:
                items.append((key, value))
                replaced = True
        if not replaced:
            items.append((key, value))
        self._items = items

    def __delitem__(self, key: str) -> None:
        key = key.lower()
        remaining = [item for item in self._items if item[0] != key]
        if len(remaining) == len(self._items):
            raise KeyError(key)
        self._items = remaining

    def append(self, name: str, value: str) -> None:
        """Add a value without replacing the ones already there."""
        self._items.append((name.lower(), value))

    def getlist(self, key: str) -> list[str]:
        key = key.lower()
        return [value for name, value in self._items if name == key]

    def __iter__(self) -> Iterator[str]:
        seen: set[str] = set()
        for name, _ in self._items:
            if name not in seen:
                seen.add(name)
                yield name

    def __len__(self) -> int:
        return len({name for name, _ in self._items})

    def raw(self) -> list[tuple[bytes, bytes]]:
        """The ASGI form: a list of lowercase byte pairs."""
        return [(name.encode("latin-1"), value.encode("latin-1")) for name, value in self._items]

    def __repr__(self) -> str:
        return f"MutableHeaders({self._items!r})"


class Response[BodyT]:
    """A response with a typed body, plus the status, headers and cookies.

    The body is rendered once, on the way out: ``bytes`` and ``str`` are sent as
    they are, and anything else is serialized as JSON.
    """

    __slots__ = ("_body", "_cookies", "_status_set", "content", "headers", "media_type", "status")

    #: Media type used when the caller does not pass one; subclasses override it.
    default_media_type: ClassVar[str | None] = None
    #: Whether the whole body is in memory. A middleware that wants to rewrite
    #: it must leave the streamed responses alone rather than draining them.
    buffered: ClassVar[bool] = True

    def __init__(
        self,
        content: BodyT,
        *,
        status: int | None = None,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
    ) -> None:
        self.content = content
        #: Left at 200 unless asked for; the handler's declared status fills it in.
        self.status = 200 if status is None else status
        self._status_set = status is not None
        self.headers = MutableHeaders(headers)
        self.media_type = media_type if media_type is not None else type(self).default_media_type
        self._cookies: list[str] = []
        self._body: bytes | None = None

    @property
    def has_explicit_status(self) -> bool:
        """Whether the caller chose the status, rather than taking the default."""
        return self._status_set

    def set_status(self, status: int) -> None:
        """Choose the status now, as if it had been passed to the constructor.

        The handler's declared status does not override one chosen this way.
        """
        self.status = status
        self._status_set = True

    def render(self) -> bytes:
        """Serialize the body, remembering the result."""
        if self._body is None:
            body, media_type = _render(self.content, self.media_type)
            self._body = body
            self.media_type = media_type
        return self._body

    def set_body(self, body: bytes) -> None:
        """Replace the rendered body, for middlewares that re-encode it."""
        self._body = body
        self.headers["content-length"] = str(len(body))

    def raw_headers(self) -> list[tuple[bytes, bytes]]:
        """Headers in ASGI form, with content type, length and cookies filled in."""
        return self._headers_for(len(self.render()))

    def _headers_for(self, length: int | None) -> list[tuple[bytes, bytes]]:
        """The same headers for a body of ``length``; ``None`` means "unknown"."""
        headers = self.headers.raw()
        present = {name for name, _ in headers}
        if self.media_type and b"content-type" not in present:
            headers.append((b"content-type", self.media_type.encode("latin-1")))
        has_body = self.status >= 200 and self.status not in _BODYLESS
        if length is not None and has_body and b"content-length" not in present:
            headers.append((b"content-length", str(length).encode("latin-1")))
        headers.extend((b"set-cookie", cookie.encode("latin-1")) for cookie in self._cookies)
        return headers

    async def send(self, send: Send) -> None:
        """Write the response out over ASGI, in as many messages as it takes."""
        body = self.render()
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": self.raw_headers(),
            }
        )
        await send({"type": "http.response.body", "body": body})

    def set_cookie(
        self,
        name: str,
        value: str = "",
        *,
        max_age: int | None = None,
        expires: str | None = None,
        path: str = "/",
        domain: str | None = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: Literal["lax", "strict", "none"] | None = "lax",
    ) -> None:
        """Queue a ``Set-Cookie`` header."""
        from urllib.parse import quote

        parts = [f"{name}={quote(value)}"]
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        if expires is not None:
            parts.append(f"Expires={expires}")
        if path:
            parts.append(f"Path={path}")
        if domain:
            parts.append(f"Domain={domain}")
        if secure:
            parts.append("Secure")
        if httponly:
            parts.append("HttpOnly")
        if samesite is not None:
            if samesite not in ("lax", "strict", "none"):
                raise ValueError(f"invalid samesite value {samesite!r}")
            if samesite == "none" and not secure:
                raise ValueError("samesite='none' requires secure=True")
            parts.append(f"SameSite={samesite.capitalize()}")
        self._cookies.append("; ".join(parts))

    def delete_cookie(self, name: str, *, path: str = "/", domain: str | None = None) -> None:
        """Queue a ``Set-Cookie`` that expires the cookie immediately."""
        self.set_cookie(name, "", max_age=0, expires=_EPOCH, path=path, domain=domain)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.status}>"


class RedirectResponse(Response[None]):
    """Send the client somewhere else."""

    __slots__ = ()

    def __init__(
        self,
        url: str,
        *,
        status: int = 307,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(None, status=status, headers=headers)
        from urllib.parse import quote

        self.headers["location"] = quote(url, safe=":/%#?=@[]!$&'()*+,;~")


class StreamingResponse(Response[None]):
    """A body produced a chunk at a time, never held whole in memory.

    Without a ``Content-Length`` the server falls back to chunked transfer, so
    the length does not have to be known before the first byte goes out.
    """

    __slots__ = ("_content", "chunk_size")
    buffered: ClassVar[bool] = False

    def __init__(
        self,
        content: AsyncIterable[bytes | str] | Iterable[bytes | str],
        *,
        status: int | None = None,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
    ) -> None:
        super().__init__(None, status=status, headers=headers, media_type=media_type)
        self._content = content

    def render(self) -> bytes:
        """Nothing is buffered: the chunks are only produced while sending."""
        return b""

    def raw_headers(self) -> list[tuple[bytes, bytes]]:
        # The length is unknown, unless the caller happened to know it.
        return self._headers_for(None)

    async def send(self, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": self.raw_headers(),
            }
        )
        async for chunk in _chunks(self._content):
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _chunks(
    content: AsyncIterable[bytes | str] | Iterable[bytes | str],
) -> AsyncIterator[bytes]:
    """Iterate a sync or async source, encoding whatever text it yields."""
    if isinstance(content, AsyncIterable):
        async for chunk in content:
            yield chunk.encode("utf-8") if isinstance(chunk, str) else chunk
        return
    for chunk in content:
        yield chunk.encode("utf-8") if isinstance(chunk, str) else chunk


class FileResponse(Response[None]):
    """Send a file from disk, a block at a time.

    The validators a cache needs — ``ETag`` and ``Last-Modified`` — are always
    filled in. Pass the ``request`` as well and the conditional headers are
    honoured too: a client whose copy is still good gets 304, and a ``Range``
    gets 206.
    """

    __slots__ = ("_length", "_start", "chunk_size", "filename", "path")
    buffered: ClassVar[bool] = False

    #: How much is read from disk at a time.
    default_chunk_size: ClassVar[int] = 64 * 1024

    def __init__(
        self,
        path: Any,
        *,
        request: Any = None,
        status: int | None = None,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
        filename: str | None = None,
        disposition: Literal["inline", "attachment"] = "attachment",
        chunk_size: int | None = None,
    ) -> None:
        from pathlib import Path

        super().__init__(None, status=status, headers=headers, media_type=media_type)
        self.path = Path(path)
        self.filename = filename
        self.chunk_size = chunk_size or type(self).default_chunk_size
        stat = _stat_file(self.path)
        self._start = 0
        self._length = stat.st_size
        if self.media_type is None:
            self.media_type = _guess_media_type(self.filename or self.path.name)
        _default_header(self.headers, "etag", _etag(stat))
        _default_header(self.headers, "last-modified", _http_date(stat.st_mtime))
        _default_header(self.headers, "accept-ranges", "bytes")
        if filename is not None:
            _default_header(
                self.headers, "content-disposition", _content_disposition(disposition, filename)
            )
        if request is not None:
            self._apply_conditions(request, stat)

    def _apply_conditions(self, request: Any, stat: Any) -> None:
        """Turn the request's cache and range headers into 304, 206 or 416."""
        etag = self.headers["etag"]
        if _is_fresh(request, etag, stat.st_mtime):
            self.set_status(304)
            self._length = 0
            self.media_type = None
            for name in ("content-length", "content-type", "content-disposition"):
                self.headers.pop(name, None)
            return
        requested = _parse_range(request.headers.get("range"), stat.st_size)
        if requested is None:
            return
        if requested is _UNSATISFIABLE:
            self.set_status(416)
            self._length = 0
            self.headers["content-range"] = f"bytes */{stat.st_size}"
            return
        if not _range_applies(request, etag, stat.st_mtime):
            return  # the copy has moved on, so answer with the whole of it
        start, end = cast(tuple[int, int], requested)
        self.set_status(206)
        self._start = start
        self._length = end - start + 1
        self.headers["content-range"] = f"bytes {start}-{end}/{stat.st_size}"

    def render(self) -> bytes:
        """Nothing is buffered: the file is read while sending."""
        return b""

    def raw_headers(self) -> list[tuple[bytes, bytes]]:
        return self._headers_for(self._length)

    async def send(self, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": self.status,
                "headers": self.raw_headers(),
            }
        )
        if self._length == 0 or self.status in _BODYLESS:
            await send({"type": "http.response.body", "body": b""})
            return

        import asyncio

        remaining = self._length
        with self.path.open("rb") as handle:
            if self._start:
                handle.seek(self._start)
            while remaining > 0:
                # Reading in a thread: a big file must not stall the event loop.
                chunk = await asyncio.to_thread(handle.read, min(self.chunk_size, remaining))
                if not chunk:
                    break  # the file shrank under us; stop rather than wait forever
                remaining -= len(chunk)
                await send(
                    {"type": "http.response.body", "body": chunk, "more_body": remaining > 0}
                )
        if remaining > 0:
            await send({"type": "http.response.body", "body": b"", "more_body": False})


#: Marks a ``Range`` that names nothing inside the file.
_UNSATISFIABLE: Final = object()


def _default_header(headers: MutableHeaders, name: str, value: str) -> None:
    """Set ``name`` unless the caller already chose a value for it."""
    if name not in headers:
        headers[name] = value


def _stat_file(path: Any) -> Any:
    """``stat`` for a regular file; anything else is a 404."""
    import stat as stat_module

    from .exceptions import HTTPError

    try:
        stat = path.stat()
    except OSError:
        raise HTTPError(404, "file not found") from None
    if not stat_module.S_ISREG(stat.st_mode):
        raise HTTPError(404, "file not found")
    return stat


def _guess_media_type(name: str) -> str:
    import mimetypes

    guessed, encoding = mimetypes.guess_type(name)
    if guessed is None:
        return "application/octet-stream"
    # A ".txt.gz" is a compressed stream, not text the client can read directly.
    return guessed if encoding is None else "application/octet-stream"


def _etag(stat: Any) -> str:
    return f'"{int(stat.st_mtime):x}-{stat.st_size:x}"'


def _http_date(timestamp: float) -> str:
    from email.utils import formatdate

    return formatdate(timestamp, usegmt=True)


def _content_disposition(kind: str, filename: str) -> str:
    """RFC 6266: a quoted name, or a percent-encoded one when it is not ASCII."""
    from urllib.parse import quote

    try:
        filename.encode("ascii")
    except UnicodeEncodeError:
        return f"{kind}; filename*=utf-8''{quote(filename)}"
    escaped = filename.replace("\\", "\\\\").replace('"', '\\"')
    return f'{kind}; filename="{escaped}"'


def _is_fresh(request: Any, etag: str, mtime: float) -> bool:
    """Whether the client's copy is still good, per RFC 9110 §13.1."""
    if_none_match = request.headers.get("if-none-match")
    if if_none_match is not None:
        if if_none_match.strip() == "*":
            return True
        return any(_etags_match(candidate, etag) for candidate in if_none_match.split(","))
    since = _parse_http_date(request.headers.get("if-modified-since"))
    # Whole seconds: the header has no room for anything finer.
    return since is not None and int(mtime) <= since


def _etags_match(candidate: str, etag: str) -> bool:
    candidate = candidate.strip()
    if candidate.startswith("W/"):
        candidate = candidate[2:]
    return candidate == etag


def _range_applies(request: Any, etag: str, mtime: float) -> bool:
    """``If-Range``: answer part of the file only if the copy has not moved on."""
    if_range = request.headers.get("if-range")
    if if_range is None:
        return True
    if if_range.strip().startswith(('"', "W/")):
        return _etags_match(if_range, etag)
    parsed = _parse_http_date(if_range)
    return parsed is not None and int(mtime) <= parsed


def _parse_http_date(value: str | None) -> int | None:
    if not value:
        return None
    from email.utils import parsedate_to_datetime

    try:
        return int(parsedate_to_datetime(value).timestamp())
    except (TypeError, ValueError):
        return None


def _parse_range(header: str | None, size: int) -> tuple[int, int] | object | None:
    """One ``bytes=`` range as inclusive offsets, or ``None`` when there is none.

    Only a single range is answered: several ranges would need a second body
    format, ``multipart/byteranges``, for very little in return, so a request
    for them gets the whole file instead.
    """
    if not header:
        return None
    units, _, rest = header.partition("=")
    if units.strip().lower() != "bytes" or "," in rest:
        return None
    first, sep, last = rest.strip().partition("-")
    if not sep:
        return None
    try:
        if not first:  # "-500" asks for the final 500 bytes
            length = int(last)
            if length <= 0 or size == 0:
                return _UNSATISFIABLE
            return max(size - length, 0), size - 1
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start >= size or start > end:
        return _UNSATISFIABLE
    return start, min(end, size - 1)


def _render(content: Any, media_type: str | None) -> tuple[bytes, str | None]:
    if content is None:
        return b"", media_type
    if isinstance(content, bytes):
        return content, media_type or "application/octet-stream"
    if isinstance(content, bytearray):
        return bytes(content), media_type or "application/octet-stream"
    if isinstance(content, memoryview):
        return content.tobytes(), media_type or "application/octet-stream"
    if isinstance(content, str):
        return content.encode("utf-8"), media_type or "text/plain; charset=utf-8"

    from ._compat import json_dumps

    return json_dumps(content), media_type or "application/json"
