"""The incoming request.

Nothing is parsed until it is asked for: headers, query string, cookies and the
body each decode on first access and are then remembered.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from typing import Any, Final

from ._types import Message, Receive, Scope
from .exceptions import HTTPError

__all__ = ["Headers", "QueryParams", "Request"]

#: Buffered bodies larger than this are refused with 413; streaming is unaffected.
DEFAULT_MAX_BODY_SIZE: Final = 1024 * 1024


class Headers(Mapping[str, str]):
    """Read-only, case-insensitive view over the request headers."""

    __slots__ = ("_raw",)

    def __init__(self, raw: Sequence[tuple[bytes, bytes]]) -> None:
        self._raw = raw

    def __getitem__(self, key: str) -> str:
        wanted = key.lower().encode("latin-1")
        for name, value in self._raw:
            if name == wanted:
                return value.decode("latin-1")
        raise KeyError(key)

    def getlist(self, key: str) -> list[str]:
        """Every value sent under ``key``, in order."""
        wanted = key.lower().encode("latin-1")
        return [value.decode("latin-1") for name, value in self._raw if name == wanted]

    def __iter__(self) -> Iterator[str]:
        seen: set[str] = set()
        for name, _ in self._raw:
            key = name.decode("latin-1")
            if key not in seen:
                seen.add(key)
                yield key

    def __len__(self) -> int:
        return len({name for name, _ in self._raw})

    def __repr__(self) -> str:
        return f"Headers({dict(self)!r})"


class QueryParams(Mapping[str, str]):
    """Read-only view over the query string; repeated keys keep every value."""

    __slots__ = ("_items",)

    def __init__(self, query_string: bytes) -> None:
        from urllib.parse import parse_qsl  # deferred: urllib.parse is not a cheap import

        self._items: list[tuple[str, str]] = parse_qsl(
            query_string.decode("latin-1"), keep_blank_values=True
        )

    def __getitem__(self, key: str) -> str:
        for name, value in self._items:
            if name == key:
                return value
        raise KeyError(key)

    def getlist(self, key: str) -> list[str]:
        """Every value sent under ``key``, in order."""
        return [value for name, value in self._items if name == key]

    def multi_items(self) -> list[tuple[str, str]]:
        return list(self._items)

    def __iter__(self) -> Iterator[str]:
        seen: set[str] = set()
        for name, _ in self._items:
            if name not in seen:
                seen.add(name)
                yield name

    def __len__(self) -> int:
        return len({name for name, _ in self._items})

    def __repr__(self) -> str:
        return f"QueryParams({self._items!r})"


class Request:
    """One HTTP request, as the application sees it."""

    __slots__ = (
        "_body",
        "_cookies",
        "_headers",
        "_max_body_size",
        "_query",
        "_receive",
        "_stream_consumed",
        "path_params",
        "scope",
    )

    def __init__(
        self,
        scope: Scope,
        receive: Receive,
        *,
        max_body_size: int = DEFAULT_MAX_BODY_SIZE,
        path_params: Mapping[str, Any] | None = None,
    ) -> None:
        self.scope = scope
        self.path_params: Mapping[str, Any] = path_params or {}
        self._receive = receive
        self._max_body_size = max_body_size
        self._headers: Headers | None = None
        self._query: QueryParams | None = None
        self._cookies: dict[str, str] | None = None
        self._body: bytes | None = None
        self._stream_consumed = False

    # -- request line ---------------------------------------------------

    @property
    def method(self) -> str:
        return str(self.scope["method"])

    @property
    def path(self) -> str:
        return str(self.scope["path"])

    @property
    def scheme(self) -> str:
        return str(self.scope.get("scheme", "http"))

    @property
    def http_version(self) -> str:
        return str(self.scope.get("http_version", "1.1"))

    @property
    def client(self) -> tuple[str, int] | None:
        client: Any = self.scope.get("client")
        return None if client is None else (str(client[0]), int(client[1]))

    @property
    def url(self) -> str:
        """Absolute URL, rebuilt from the scope and the ``Host`` header."""
        host = self.headers.get("host") or "localhost"
        query = self.scope.get("query_string", b"")
        suffix = "?" + bytes(query).decode("latin-1") if query else ""
        return f"{self.scheme}://{host}{self.path}{suffix}"

    # -- lazily parsed --------------------------------------------------

    @property
    def headers(self) -> Headers:
        if self._headers is None:
            self._headers = Headers(self.scope.get("headers") or ())
        return self._headers

    @property
    def query(self) -> QueryParams:
        if self._query is None:
            self._query = QueryParams(self.scope.get("query_string", b""))
        return self._query

    @property
    def cookies(self) -> Mapping[str, str]:
        if self._cookies is None:
            self._cookies = _parse_cookies(self.headers.getlist("cookie"))
        return self._cookies

    # -- body -----------------------------------------------------------

    async def stream(self) -> AsyncIterator[bytes]:
        """Yield the body as it arrives, without buffering it.

        Can only be consumed once; use :meth:`body` when you need it twice.
        """
        if self._body is not None:
            yield self._body
            return
        if self._stream_consumed:
            raise RuntimeError("the request body has already been consumed")
        self._stream_consumed = True
        while True:
            message: Message = await self._receive()
            message_type = message["type"]
            if message_type == "http.request":
                chunk: bytes = message.get("body") or b""
                if chunk:
                    yield chunk
                if not message.get("more_body", False):
                    return
            elif message_type == "http.disconnect":
                raise ClientDisconnected

    async def body(self) -> bytes:
        """The whole body, read once and remembered."""
        if self._body is None:
            chunks = bytearray()
            async for chunk in self.stream():
                chunks += chunk
                if len(chunks) > self._max_body_size:
                    raise HTTPError(413, "request body too large")
            self._body = bytes(chunks)
        return self._body

    async def json(self) -> Any:
        """The body decoded as JSON; malformed input is a 400."""
        from ._compat import json_loads

        body = await self.body()
        try:
            return json_loads(body)
        except ValueError as exc:
            raise HTTPError(400, f"malformed JSON body: {exc}") from exc

    async def form(self) -> QueryParams:
        """A ``application/x-www-form-urlencoded`` body, parsed like a query string.

        Multipart bodies arrive with file uploads, in a later phase.
        """
        content_type = self.headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type == "multipart/form-data":
            raise HTTPError(415, "multipart bodies are not supported yet")
        if media_type not in ("", "application/x-www-form-urlencoded"):
            raise HTTPError(415, f"unsupported content type {media_type!r}")
        return QueryParams(await self.body())

    def __repr__(self) -> str:
        return f"<Request {self.method} {self.path}>"


class ClientDisconnected(Exception):
    """Raised while reading a body the client will never finish sending."""


def _parse_cookies(headers: list[str]) -> dict[str, str]:
    from urllib.parse import unquote

    cookies: dict[str, str] = {}
    for header in headers:
        for part in header.split(";"):
            name, separator, value = part.partition("=")
            if not separator:
                continue
            name = name.strip()
            if name:
                cookies[name] = unquote(value.strip().strip('"'))
    return cookies
