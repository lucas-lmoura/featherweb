"""Helpers for driving the server over a real socket."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from featherweb._types import ASGIApp
from featherweb.server.runner import Server

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
        server = Server(app, host="127.0.0.1", port=0, **kwargs)
        await server.startup()
        servers.append(server)
        return server

    yield _start
    for server in servers:
        await server.shutdown()
