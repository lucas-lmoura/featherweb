"""Tests for the runner: binding, serving until asked to stop, and shutdown."""

from __future__ import annotations

import asyncio

import pytest

from featherweb._compat import loop_factory
from featherweb.server.runner import Server

from .conftest import connect
from .test_server import GET, hello


def test_loop_factory_produces_a_usable_loop() -> None:
    factory = loop_factory()
    loop = factory()
    try:
        assert isinstance(loop, asyncio.AbstractEventLoop)
    finally:
        loop.close()


async def test_serve_runs_until_shutdown_is_requested() -> None:
    server = Server(hello, host="127.0.0.1", port=0)
    serving = asyncio.create_task(server.serve())
    await asyncio.sleep(0.1)
    assert server.sockets
    assert server.url == f"http://127.0.0.1:{server.bound_port}"

    async with connect(server.bound_port) as client:
        await client.send(GET)
        assert (await client.read_response()).status == 200

    server.request_shutdown()
    async with asyncio.timeout(5):
        await serving
    assert server.state.should_exit
    assert not server.sockets


async def test_startup_twice_is_an_error() -> None:
    server = Server(hello, host="127.0.0.1", port=0)
    await server.startup()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            await server.startup()
    finally:
        await server.shutdown()


async def test_shutdown_is_idempotent() -> None:
    server = Server(hello, host="127.0.0.1", port=0)
    await server.startup()
    await server.shutdown()
    await server.shutdown()
    assert server.bound_port  # the port requested at startup is remembered
