"""Helpers for driving the server over a real socket."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import unquote, urlencode

import pytest
import uvicorn

from featherweb import App
from featherweb._compat import json_dumps
from featherweb._types import ASGIApp
from featherweb.request import Headers
from featherweb.server.runner import Server
from featherweb.testing import TestClient, TestResponse

type StartServer = Callable[..., Awaitable[Server]]


@dataclass(slots=True)
class RawResponse:
    """A response read off the wire, with the framing already applied."""

    status: int
    reason: str
    headers: list[tuple[str, str]]
    body: bytes = b""

    def header(self, name: str) -> str | None:
        name = name.lower()
        for header, value in self.headers:
            if header == name:
                return value
        return None

    def header_names(self) -> list[str]:
        return [name for name, _ in self.headers]


class RawConnection:
    """A client that speaks bytes, so malformed requests can be sent on purpose."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer

    async def send(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def read_response(self, *, method: str = "GET") -> RawResponse:
        head = await self._reader.readuntil(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        version, _, rest = lines[0].partition(b" ")
        assert version == b"HTTP/1.1", version
        raw_status, _, raw_reason = rest.partition(b" ")
        status = int(raw_status)
        headers: list[tuple[str, str]] = []
        for line in lines[1:]:
            if not line:
                continue
            name, _, value = line.partition(b":")
            headers.append((name.decode().lower(), value.strip().decode()))
        response = RawResponse(status, raw_reason.decode(), headers)
        response.body = await self._read_body(response, method)
        return response

    async def _read_body(self, response: RawResponse, method: str) -> bytes:
        if method == "HEAD" or response.status < 200 or response.status in (204, 304):
            return b""
        if (response.header("transfer-encoding") or "").lower() == "chunked":
            return await self._read_chunked()
        length = response.header("content-length")
        if length is not None:
            return await self._reader.readexactly(int(length))
        return await self._reader.read()  # framed by the connection closing

    async def _read_chunked(self) -> bytes:
        body = bytearray()
        while True:
            size = int((await self._reader.readuntil(b"\r\n")).split(b";")[0], 16)
            if size == 0:
                await self._reader.readuntil(b"\r\n")
                return bytes(body)
            body += await self._reader.readexactly(size)
            assert await self._reader.readexactly(2) == b"\r\n"

    async def read_eof(self) -> bytes:
        return await self._reader.read()

    async def at_eof(self) -> bool:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(2):
                return await self._reader.read(1) == b""
        return False

    def close(self) -> None:
        self._writer.close()


@contextlib.asynccontextmanager
async def connect(port: int) -> AsyncGenerator[RawConnection]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    connection = RawConnection(reader, writer)
    try:
        yield connection
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


@pytest.fixture
async def start_server() -> AsyncIterator[StartServer]:
    """Start servers on an ephemeral port and shut them all down afterwards."""
    servers: list[Server] = []

    async def _start(app: ASGIApp, **kwargs: Any) -> Server:
        # The raw ASGI apps used in the server tests do not implement lifespan;
        # framework tests ask for it explicitly.
        kwargs.setdefault("lifespan", False)
        server = Server(app, host="127.0.0.1", port=0, **kwargs)
        await server.startup()
        servers.append(server)
        return server

    yield _start
    for server in servers:
        await server.shutdown()


# -- framework clients ------------------------------------------------------


class SocketClient:
    """Speaks HTTP to a running server, one connection per request.

    It offers the same surface as :class:`featherweb.testing.TestClient`, so the
    framework suite can run unchanged in-process, on our server and on uvicorn.
    """

    def __init__(self, port: int, *, host: str = "127.0.0.1") -> None:
        self.port = port
        self.host = host

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
        method = method.upper()
        body = _encode_body(content, json)
        given = {name.lower() for name in (headers or {})}
        lines = [f"Host: {self.host}", "Connection: close"]
        if "content-type" not in given:
            if json is not None:
                lines.append("Content-Type: application/json")
            elif isinstance(content, str):
                lines.append("Content-Type: text/plain; charset=utf-8")
        if body and "content-length" not in given:
            lines.append(f"Content-Length: {len(body)}")
        if cookies:
            lines.append("Cookie: " + "; ".join(f"{k}={v}" for k, v in cookies.items()))
        lines.extend(f"{name}: {value}" for name, value in (headers or {}).items())

        path, _, inline_query = path.partition("?")
        query = _encode_query(params) or inline_query
        target = f"{path}?{query}" if query else path
        head = f"{method} {target} HTTP/1.1\r\n" + "\r\n".join(lines) + "\r\n\r\n"

        async with connect(self.port) as connection:
            await connection.send(head.encode("latin-1") + body)
            raw = await connection.read_response(method=method)
        received = Headers([(name.encode(), value.encode()) for name, value in raw.headers])
        return TestResponse(raw.status, received, raw.body, _read_cookies(received))

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


def _encode_body(content: bytes | str | None, json: Any) -> bytes:
    if json is not None:
        return json_dumps(json)
    if content is None:
        return b""
    return content.encode("utf-8") if isinstance(content, str) else content


def _encode_query(params: Mapping[str, Any] | str | None) -> str:
    if params is None:
        return ""
    if isinstance(params, str):
        return params.lstrip("?")
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if isinstance(value, list | tuple):
            pairs.extend((key, str(item)) for item in cast(Sequence[Any], value))
        else:
            pairs.append((key, str(value)))
    return urlencode(pairs)


def _read_cookies(headers: Headers) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for header in headers.getlist("set-cookie"):
        name, separator, rest = header.partition("=")
        if separator:
            cookies[name.strip()] = unquote(rest.split(";", 1)[0])
    return cookies


type Client = TestClient | SocketClient
type Serve = Callable[[App], Awaitable[Client]]


class _QuietServer(uvicorn.Server):
    """uvicorn without the signal handlers, which pytest needs for itself."""

    def install_signal_handlers(self) -> None:
        return None


async def _start_uvicorn(app: App) -> tuple[int, Callable[[], Awaitable[None]]]:
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="warning",
        access_log=False,
        lifespan="on",
        ws="none",
    )
    server = _QuietServer(config)
    task = asyncio.create_task(server.serve())
    for _ in range(500):
        if server.started:
            break
        await asyncio.sleep(0.01)
    else:  # pragma: no cover - only on a badly broken machine
        raise RuntimeError("uvicorn did not start in time")
    address: Any = server.servers[0].sockets[0].getsockname()

    async def stop() -> None:
        server.should_exit = True
        await task

    return int(address[1]), stop


@pytest.fixture(params=["testclient", "featherweb", "uvicorn"])
def transport(request: pytest.FixtureRequest) -> str:
    """Which stack the framework suite runs against."""
    return str(request.param)


@pytest.fixture
async def serve(transport: str) -> AsyncIterator[Serve]:
    """Serve an application and hand back a client for it."""
    cleanups: list[Callable[[], Awaitable[Any]]] = []

    async def _serve(app: App) -> Client:
        if transport == "testclient":
            client = TestClient(app)
            await client.__aenter__()
            cleanups.append(lambda: client.__aexit__(None, None, None))
            return client
        if transport == "featherweb":
            server = Server(app, host="127.0.0.1", port=0)
            await server.startup()
            cleanups.append(server.shutdown)
            return SocketClient(server.bound_port)
        port, stop = await _start_uvicorn(app)
        cleanups.append(stop)
        return SocketClient(port)

    yield _serve
    for cleanup in reversed(cleanups):
        await cleanup()
