"""Integration tests: a raw ASGI application served over a real socket."""

from __future__ import annotations

import asyncio
import json

import pytest

from featherweb._types import Message, Receive, Scope, Send
from featherweb.server.parser import Limits
from featherweb.server.protocol import ServerConfig

from .conftest import StartServer, connect

GET = b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n"


# -- applications -----------------------------------------------------------


async def hello(scope: Scope, receive: Receive, send: Send) -> None:
    assert scope["type"] == "http"
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": b"hello"})


async def read_body(receive: Receive) -> bytes:
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    return bytes(body)


async def echo(scope: Scope, receive: Receive, send: Send) -> None:
    del scope
    body = await read_body(receive)
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


async def dump_scope(scope: Scope, receive: Receive, send: Send) -> None:
    del receive
    payload = json.dumps(
        {
            "http_version": scope["http_version"],
            "method": scope["method"],
            "scheme": scope["scheme"],
            "path": scope["path"],
            "raw_path": scope["raw_path"].decode(),
            "query_string": scope["query_string"].decode(),
            "root_path": scope["root_path"],
            "headers": [[name.decode(), value.decode()] for name, value in scope["headers"]],
            "client_host": scope["client"][0],
            "server_port": scope["server"][1],
            "asgi": dict(scope["asgi"]),
        }
    ).encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": payload})


async def stream(scope: Scope, receive: Receive, send: Send) -> None:
    del scope, receive
    await send({"type": "http.response.start", "status": 200, "headers": []})
    for part in (b"one", b"two", b"three"):
        await send({"type": "http.response.body", "body": part, "more_body": True})
    await send({"type": "http.response.body", "body": b"", "more_body": False})


async def boom(scope: Scope, receive: Receive, send: Send) -> None:
    del scope, receive, send
    raise RuntimeError("application blew up")


def responder(status: int, headers: list[tuple[bytes, bytes]], body: bytes = b"") -> object:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    return app


# -- basics -----------------------------------------------------------------


async def test_get(start_server: StartServer) -> None:
    server = await start_server(hello)
    async with connect(server.bound_port) as client:
        await client.send(GET)
        response = await client.read_response()
    assert response.status == 200
    assert response.reason == "OK"
    assert response.body == b"hello"
    assert response.header("content-length") == "5"
    assert response.header("content-type") == "text/plain; charset=utf-8"
    assert response.header("server") == "featherweb"
    assert response.header("date") is not None


async def test_scope(start_server: StartServer) -> None:
    server = await start_server(dump_scope)
    async with connect(server.bound_port) as client:
        await client.send(b"GET /a%20b?q=1 HTTP/1.1\r\nHost: example.com\r\nX-A: 1\r\n\r\n")
        response = await client.read_response()
    scope = json.loads(response.body)
    assert scope["http_version"] == "1.1"
    assert scope["method"] == "GET"
    assert scope["scheme"] == "http"
    assert scope["path"] == "/a b"
    assert scope["raw_path"] == "/a%20b"
    assert scope["query_string"] == "q=1"
    assert scope["root_path"] == ""
    assert ["x-a", "1"] in scope["headers"]
    assert scope["client_host"] == "127.0.0.1"
    assert scope["server_port"] == server.bound_port
    assert scope["asgi"] == {"version": "3.0", "spec_version": "2.3"}


async def test_post_with_body(start_server: StartServer) -> None:
    server = await start_server(echo)
    async with connect(server.bound_port) as client:
        await client.send(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 11\r\n\r\nhello world")
        response = await client.read_response()
    assert response.body == b"hello world"


async def test_chunked_request_body(start_server: StartServer) -> None:
    server = await start_server(echo)
    async with connect(server.bound_port) as client:
        await client.send(
            b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        )
        response = await client.read_response()
    assert response.body == b"hello world"
    assert response.header("connection") is None


async def test_body_split_across_packets(start_server: StartServer) -> None:
    server = await start_server(echo)
    async with connect(server.bound_port) as client:
        await client.send(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 10\r\n\r\nhell")
        await asyncio.sleep(0.05)
        await client.send(b"o body")
        response = await client.read_response()
    assert response.body == b"hello body"


async def test_large_upload(start_server: StartServer) -> None:
    payload = b"x" * (2 * 1024 * 1024)

    async def slow_echo(scope: Scope, receive: Receive, send: Send) -> None:
        del scope
        await asyncio.sleep(0.05)  # let the read buffer fill up and backpressure kick in
        body = await read_body(receive)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": str(len(body)).encode()})

    server = await start_server(slow_echo)
    async with connect(server.bound_port) as client:
        header = f"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: {len(payload)}\r\n\r\n"
        await client.send(header.encode() + payload)
        response = await client.read_response()
    assert response.body == str(len(payload)).encode()


# -- connection management --------------------------------------------------


async def test_keep_alive_reuses_the_connection(start_server: StartServer) -> None:
    server = await start_server(hello)
    async with connect(server.bound_port) as client:
        for _ in range(3):
            await client.send(GET)
            response = await client.read_response()
            assert response.status == 200
            assert response.body == b"hello"
            assert response.header("connection") is None


async def test_pipelined_requests(start_server: StartServer) -> None:
    server = await start_server(echo)
    first = b"POST /1 HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n\r\none"
    second = b"POST /2 HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\n\r\ntwo"
    third = b"POST /3 HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nthree"
    async with connect(server.bound_port) as client:
        await client.send(first + second + third)
        bodies = [(await client.read_response()).body for _ in range(3)]
    assert bodies == [b"one", b"two", b"three"]


async def test_connection_close_is_honoured(start_server: StartServer) -> None:
    server = await start_server(hello)
    async with connect(server.bound_port) as client:
        await client.send(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        response = await client.read_response()
        assert response.header("connection") == "close"
        assert await client.read_eof() == b""


async def test_http_10_closes_by_default(start_server: StartServer) -> None:
    server = await start_server(hello)
    async with connect(server.bound_port) as client:
        await client.send(b"GET / HTTP/1.0\r\n\r\n")
        response = await client.read_response()
        assert response.status == 200
        assert response.header("connection") == "close"
        assert await client.read_eof() == b""


async def test_http_10_keep_alive(start_server: StartServer) -> None:
    server = await start_server(hello)
    async with connect(server.bound_port) as client:
        await client.send(b"GET / HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")
        response = await client.read_response()
        assert response.header("connection") == "keep-alive"
        await client.send(b"GET / HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")
        assert (await client.read_response()).status == 200


async def test_unread_request_body_closes_the_connection(start_server: StartServer) -> None:
    server = await start_server(responder(413, [], b"too big"))
    async with connect(server.bound_port) as client:
        # Announce more than is sent: the server cannot know where the next request starts.
        await client.send(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\nhalf")
        response = await client.read_response()
        assert response.status == 413
        assert response.header("connection") == "close"


# -- response framing -------------------------------------------------------


async def test_streaming_response_uses_chunked(start_server: StartServer) -> None:
    server = await start_server(stream)
    async with connect(server.bound_port) as client:
        await client.send(GET)
        response = await client.read_response()
        assert response.header("transfer-encoding") == "chunked"
        assert response.header("content-length") is None
        assert response.body == b"onetwothree"
        # The connection survives a streamed response.
        await client.send(GET)
        assert (await client.read_response()).status == 200


async def test_streaming_response_in_http_10_closes(start_server: StartServer) -> None:
    server = await start_server(stream)
    async with connect(server.bound_port) as client:
        await client.send(b"GET / HTTP/1.0\r\n\r\n")
        response = await client.read_response()
        assert response.header("transfer-encoding") is None
        assert response.header("connection") == "close"
        assert response.body == b"onetwothree"


async def test_head_has_no_body_but_keeps_the_length(start_server: StartServer) -> None:
    server = await start_server(hello)
    async with connect(server.bound_port) as client:
        await client.send(b"HEAD / HTTP/1.1\r\nHost: x\r\n\r\n")
        response = await client.read_response(method="HEAD")
        assert response.header("content-length") == "5"
        assert response.body == b""
        await client.send(GET)  # the connection is still in sync
        assert (await client.read_response()).body == b"hello"


async def test_204_has_no_content_length(start_server: StartServer) -> None:
    server = await start_server(responder(204, [(b"content-length", b"0")]))
    async with connect(server.bound_port) as client:
        await client.send(GET)
        response = await client.read_response()
        assert response.status == 204
        assert response.header("content-length") is None
        assert response.header("transfer-encoding") is None
        await client.send(GET)
        assert (await client.read_response()).status == 204


async def test_application_headers_are_preserved(start_server: StartServer) -> None:
    headers = [(b"x-one", b"1"), (b"x-two", b"2"), (b"x-one", b"3")]
    server = await start_server(responder(201, headers, b"ok"))
    async with connect(server.bound_port) as client:
        await client.send(GET)
        response = await client.read_response()
    assert response.status == 201
    assert response.reason == "Created"
    assert [value for name, value in response.headers if name == "x-one"] == ["1", "3"]
    assert response.header("x-two") == "2"


async def test_application_may_set_its_own_date_and_server(start_server: StartServer) -> None:
    headers = [(b"date", b"Sun, 01 Jan 2023 00:00:00 GMT"), (b"server", b"custom")]
    server = await start_server(responder(200, headers, b"ok"))
    async with connect(server.bound_port) as client:
        await client.send(GET)
        response = await client.read_response()
    assert response.header_names().count("date") == 1
    assert response.header("date") == "Sun, 01 Jan 2023 00:00:00 GMT"
    assert response.header("server") == "custom"


async def test_application_transfer_encoding_is_ignored(start_server: StartServer) -> None:
    server = await start_server(responder(200, [(b"transfer-encoding", b"chunked")], b"body"))
    async with connect(server.bound_port) as client:
        await client.send(GET)
        response = await client.read_response()
    assert response.header("transfer-encoding") is None
    assert response.header("content-length") == "4"
    assert response.body == b"body"


async def test_expect_100_continue(start_server: StartServer) -> None:
    server = await start_server(echo)
    async with connect(server.bound_port) as client:
        await client.send(
            b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 4\r\nExpect: 100-continue\r\n\r\n"
        )
        informational = await client.read_response()
        assert informational.status == 100
        await client.send(b"data")
        response = await client.read_response()
    assert response.status == 200
    assert response.body == b"data"


# -- errors -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (
            b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 6\r\nTransfer-Encoding: chunked\r\n\r\n",
            400,
        ),
        (b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length: 1\r\nContent-Length: 2\r\n\r\n", 400),
        (b"GET / HTTP/1.1\r\nHost: x\r\nX-A: 1\r\n\tfolded\r\n\r\n", 400),
        (b"GET / HTTP/1.1\r\n\r\n", 400),
        (b"GET / HTTP/1.1\nHost: x\n\n", 400),
        (b"GET / HTTP/3.0\r\nHost: x\r\n\r\n", 505),
        (b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: gzip\r\n\r\n", 501),
    ],
)
async def test_malformed_requests_are_rejected(
    start_server: StartServer, raw: bytes, status: int
) -> None:
    server = await start_server(hello)
    async with connect(server.bound_port) as client:
        await client.send(raw)
        response = await client.read_response()
        assert response.status == status
        assert response.header("connection") == "close"
        assert await client.read_eof() == b""


async def test_body_limit_is_enforced(start_server: StartServer) -> None:
    config = ServerConfig(limits=Limits(max_body_size=16))
    server = await start_server(echo, config=config)
    async with connect(server.bound_port) as client:
        await client.send(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 64\r\n\r\n")
        response = await client.read_response()
    assert response.status == 413


async def test_application_error_becomes_500(start_server: StartServer) -> None:
    server = await start_server(boom)
    async with connect(server.bound_port) as client:
        await client.send(GET)
        response = await client.read_response()
        assert response.status == 500
        assert await client.read_eof() == b""


async def test_application_that_never_responds_becomes_500(start_server: StartServer) -> None:
    async def silent(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive, send

    server = await start_server(silent)
    async with connect(server.bound_port) as client:
        await client.send(GET)
        assert (await client.read_response()).status == 500


# -- timeouts and shutdown --------------------------------------------------


async def test_idle_connection_is_closed(start_server: StartServer) -> None:
    server = await start_server(hello, config=ServerConfig(keep_alive_timeout=0.2))
    async with connect(server.bound_port) as client:
        await client.send(GET)
        assert (await client.read_response()).status == 200
        assert await client.read_eof() == b""


async def test_slow_headers_get_a_timeout(start_server: StartServer) -> None:
    server = await start_server(hello, config=ServerConfig(header_timeout=0.2))
    async with connect(server.bound_port) as client:
        await client.send(b"GET / HTTP/1.1\r\nHost: x\r\n")  # never finished
        response = await client.read_response()
        assert response.status == 408
        assert await client.read_eof() == b""


async def test_shutdown_closes_idle_connections(start_server: StartServer) -> None:
    server = await start_server(hello)
    async with connect(server.bound_port) as client:
        await client.send(GET)
        assert (await client.read_response()).status == 200
        await server.shutdown()
        assert await client.read_eof() == b""


async def test_shutdown_waits_for_the_running_request(start_server: StartServer) -> None:
    async def slow(scope: Scope, receive: Receive, send: Send) -> None:
        del scope, receive
        await asyncio.sleep(0.2)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"done"})

    server = await start_server(slow)
    async with connect(server.bound_port) as client:
        await client.send(GET)
        await asyncio.sleep(0.05)
        shutdown = asyncio.create_task(server.shutdown())
        response = await client.read_response()
        assert response.status == 200
        assert response.body == b"done"
        assert response.header("connection") == "close"
        await shutdown


async def test_shutdown_stops_accepting(start_server: StartServer) -> None:
    server = await start_server(hello)
    port = server.bound_port
    await server.shutdown()
    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection("127.0.0.1", port)


async def test_client_disconnect_is_reported(start_server: StartServer) -> None:
    seen: list[Message] = []
    done = asyncio.Event()

    async def watcher(scope: Scope, receive: Receive, send: Send) -> None:
        del scope
        seen.append(await receive())  # the request itself
        seen.append(await receive())  # blocks until the client goes away
        done.set()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    server = await start_server(watcher)
    async with connect(server.bound_port) as client:
        await client.send(GET)
        await asyncio.sleep(0.05)
        client.close()
        async with asyncio.timeout(2):
            await done.wait()
    assert seen[0]["type"] == "http.request"
    assert seen[1]["type"] == "http.disconnect"
