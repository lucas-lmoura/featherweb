"""An in-process client for tests: it calls the ASGI application directly.

No socket, no server, no event loop of its own — just the application under
test::

    async with TestClient(app) as client:       # runs the lifespan
        response = await client.get("/users/1")
        assert response.json() == {"id": 1}
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

from ._types import ASGIApp, Message, Scope
from .request import Headers

__all__ = ["TestClient", "TestResponse"]


@dataclass(slots=True)
class TestResponse:
    """What the application sent back."""

    # Not a pytest test class, despite the name.
    __test__ = False

    status: int
    headers: Headers
    body: bytes
    #: Cookies set by the response, already unquoted.
    cookies: dict[str, str]

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")

    def json(self) -> Any:
        from ._compat import json_loads

        return json_loads(self.body)

    def __repr__(self) -> str:
        return f"<TestResponse {self.status} {len(self.body)} bytes>"


class TestClient:
    """Drives an ASGI application in the current event loop."""

    # Not a pytest test class, despite the name.
    __test__ = False

    def __init__(
        self,
        app: ASGIApp,
        *,
        base_url: str = "http://testserver",
        root_path: str = "",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.app = app
        self.root_path = root_path
        self.default_headers = dict(headers or {})
        scheme, _, host = base_url.partition("://")
        self.scheme = scheme or "http"
        self.host = host or "testserver"
        self._lifespan: _Lifespan | None = None

    # -- lifespan -------------------------------------------------------

    async def __aenter__(self) -> Self:
        self._lifespan = _Lifespan(self.app)
        await self._lifespan.startup()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._lifespan is not None:
            await self._lifespan.shutdown()
            self._lifespan = None

    # -- requests -------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | str | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | str | None = None,
        json: Any = None,
        cookies: Mapping[str, str] | None = None,
    ) -> TestResponse:
        """Send one request and collect the response."""
        body = _body_of(content, json)
        given = {name.lower() for name in (*self.default_headers, *(headers or {}))}
        sent: list[tuple[str, str]] = [("host", self.host)]
        if "content-type" not in given:
            if json is not None:
                sent.append(("content-type", "application/json"))
            elif isinstance(content, str):
                sent.append(("content-type", "text/plain; charset=utf-8"))
        if body and "content-length" not in given:
            sent.append(("content-length", str(len(body))))
        if cookies:
            sent.append(("cookie", "; ".join(f"{k}={v}" for k, v in cookies.items())))
        for source in (self.default_headers, headers or {}):
            sent.extend(source.items())

        path, _, inline_query = path.partition("?")
        query = _query_string(params) or inline_query

        from urllib.parse import quote

        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method.upper(),
            "scheme": self.scheme,
            "path": self.root_path + path,
            "raw_path": quote(self.root_path + path).encode("latin-1"),
            "query_string": query.encode("latin-1"),
            "root_path": self.root_path,
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in sent
            ],
            "client": ("testclient", 50000),
            "server": (self.host, 80),
        }
        return await _call(self.app, scope, body)

    async def get(self, path: str, **kwargs: Any) -> TestResponse:
        return await self.request("GET", path, **kwargs)

    async def head(self, path: str, **kwargs: Any) -> TestResponse:
        return await self.request("HEAD", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> TestResponse:
        return await self.request("POST", path, **kwargs)

    async def put(self, path: str, **kwargs: Any) -> TestResponse:
        return await self.request("PUT", path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> TestResponse:
        return await self.request("PATCH", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> TestResponse:
        return await self.request("DELETE", path, **kwargs)

    async def options(self, path: str, **kwargs: Any) -> TestResponse:
        return await self.request("OPTIONS", path, **kwargs)


async def _call(app: ASGIApp, scope: Scope, body: bytes) -> TestResponse:
    pending: list[Message] = [{"type": "http.request", "body": body, "more_body": False}]
    status = 500
    raw_headers: list[tuple[bytes, bytes]] = []
    chunks = bytearray()

    async def receive() -> Message:
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = int(message["status"])
            raw_headers.extend(
                (bytes(name).lower(), bytes(value)) for name, value in message.get("headers") or ()
            )
        elif message["type"] == "http.response.body":
            chunks.extend(message.get("body") or b"")

    await app(scope, receive, send)
    headers = Headers(raw_headers)
    return TestResponse(status, headers, bytes(chunks), _cookies_of(headers))


def _cookies_of(headers: Headers) -> dict[str, str]:
    from urllib.parse import unquote

    cookies: dict[str, str] = {}
    for header in headers.getlist("set-cookie"):
        name, separator, rest = header.partition("=")
        if separator:
            cookies[name.strip()] = unquote(rest.split(";", 1)[0])
    return cookies


def _body_of(content: bytes | str | None, json: Any) -> bytes:
    if json is not None:
        from ._compat import json_dumps

        return json_dumps(json)
    if content is None:
        return b""
    return content.encode("utf-8") if isinstance(content, str) else content


def _query_string(params: Mapping[str, Any] | str | None) -> str:
    if params is None:
        return ""
    if isinstance(params, str):
        return params.lstrip("?")

    from urllib.parse import urlencode

    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if isinstance(value, list | tuple):
            pairs.extend((key, str(item)) for item in value)  # type: ignore[misc]
        else:
            pairs.append((key, str(value)))
    return urlencode(pairs)


class _Lifespan:
    """Runs the application's lifespan protocol alongside the test."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._events: asyncio.Queue[Message] = asyncio.Queue()
        self._replies: asyncio.Queue[Message] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    async def startup(self) -> None:
        scope: Scope = {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}}
        self._task = asyncio.create_task(self._run(scope))
        await self._events.put({"type": "lifespan.startup"})
        await self._expect("lifespan.startup.complete")

    async def shutdown(self) -> None:
        await self._events.put({"type": "lifespan.shutdown"})
        await self._expect("lifespan.shutdown.complete")
        if self._task is not None:
            await self._task

    async def _run(self, scope: Scope) -> None:
        await self.app(scope, self._events.get, self._replies.put)

    async def _expect(self, message_type: str) -> None:
        message = await self._replies.get()
        if message["type"] != message_type:
            raise RuntimeError(f"lifespan failed: {message.get('message') or message['type']}")
