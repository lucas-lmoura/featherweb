"""The incoming request.

Nothing is parsed until it is asked for: headers, query string, cookies and the
body each decode on first access and are then remembered.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from ._types import Message, Receive, Scope
from .exceptions import HTTPError

if TYPE_CHECKING:  # imported lazily: these are off the package import path
    from .auth.guards import Identity
    from .auth.session import Session
    from .multipart import MultipartLimits, UploadFile

__all__ = ["Connection", "FormData", "Headers", "QueryParams", "Request"]

#: Buffered bodies larger than this are refused with 413; streaming is unaffected.
DEFAULT_MAX_BODY_SIZE: Final = 1024 * 1024

#: Marks "the body has not been parsed yet", which ``None`` cannot express.
_UNREAD: Final = object()


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


class FormData(QueryParams):
    """A submitted form: the text fields, and the files that came with them.

    It reads like the query string for the text fields, so a handler that only
    wants those does not have to know how the body was encoded.
    """

    __slots__ = ("_files",)

    def __init__(
        self,
        fields: Sequence[tuple[str, str]],
        files: Sequence[tuple[str, UploadFile]],
    ) -> None:
        self._items = list(fields)
        self._files = list(files)

    @property
    def files(self) -> list[tuple[str, UploadFile]]:
        """Every uploaded file, in the order the body listed them."""
        return list(self._files)

    def getfile(self, key: str) -> UploadFile | None:
        """The first file sent under ``key``, if there is one."""
        for name, file in self._files:
            if name == key:
                return file
        return None

    def getfilelist(self, key: str) -> list[UploadFile]:
        """Every file sent under ``key``."""
        return [file for name, file in self._files if name == key]

    async def close(self) -> None:
        """Close every file, dropping the temporary ones."""
        for _, file in self._files:
            await file.close()

    def __repr__(self) -> str:
        return f"FormData({self._items!r}, {len(self._files)} files)"


class Connection:
    """What an HTTP request and a WebSocket connection have in common.

    Both arrive as an ASGI scope with a path, headers, a query string and
    cookies, and all of those are parsed only when something asks for them.
    """

    __slots__ = (
        "_cookies",
        "_headers",
        "_query",
        "identity",
        "path_params",
        "scope",
        "session",
    )

    def __init__(self, scope: Scope, *, path_params: Mapping[str, Any] | None = None) -> None:
        self.scope = scope
        self.path_params: Mapping[str, Any] = path_params or {}
        self._headers: Headers | None = None
        self._query: QueryParams | None = None
        self._cookies: dict[str, str] | None = None
        #: Filled in by the configured authentication strategy, when there is
        #: one and this route needs it; ``None`` means "not authenticated".
        self.identity: Identity | None = None
        #: Only a session-based strategy sets this.
        self.session: Session | None = None

    @property
    def path(self) -> str:
        return str(self.scope["path"])

    @property
    def scheme(self) -> str:
        return str(self.scope.get("scheme", "http"))

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


class Request(Connection):
    """One HTTP request, as the application sees it."""

    __slots__ = (
        "_body",
        "_form",
        "_json",
        "_max_body_size",
        "_receive",
        "_stream_consumed",
    )

    def __init__(
        self,
        scope: Scope,
        receive: Receive,
        *,
        max_body_size: int = DEFAULT_MAX_BODY_SIZE,
        path_params: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(scope, path_params=path_params)
        self._receive = receive
        self._max_body_size = max_body_size
        self._body: bytes | None = None
        self._json: Any = _UNREAD
        self._form: FormData | None = None
        self._stream_consumed = False

    # -- request line ---------------------------------------------------

    @property
    def method(self) -> str:
        return str(self.scope["method"])

    @property
    def http_version(self) -> str:
        return str(self.scope.get("http_version", "1.1"))

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
        """The body decoded as JSON, parsed once; malformed input is a 400."""
        if self._json is not _UNREAD:
            return self._json
        from ._compat import json_loads

        body = await self.body()
        try:
            self._json = json_loads(body)
        except ValueError as exc:
            raise HTTPError(400, f"malformed JSON body: {exc}") from exc
        return self._json

    async def form(self, *, limits: MultipartLimits | None = None) -> FormData:
        """The submitted form, url-encoded or multipart, parsed once.

        A multipart body is read straight off the stream, so an upload never
        has to fit in memory; the files it produces are closed for you when the
        response goes out.
        """
        if self._form is not None:
            return self._form
        content_type = self.headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type == "multipart/form-data":
            from .multipart import parse_multipart

            fields, files = await parse_multipart(
                self.stream(), content_type, **({"limits": limits} if limits else {})
            )
            self._form = FormData(fields, files)
            return self._form
        if media_type not in ("", "application/x-www-form-urlencoded"):
            raise HTTPError(415, f"unsupported content type {media_type!r}")
        self._form = FormData(QueryParams(await self.body()).multi_items(), ())
        return self._form

    async def close(self) -> None:
        """Release anything the request is holding, such as spooled uploads."""
        if self._form is not None:
            await self._form.close()

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
