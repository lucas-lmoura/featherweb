"""WebSocket handlers, end to end.

Every case runs twice: once against featherweb's own server and once against
uvicorn, because the handler is the same either way and the framing underneath
is not. That is the point of keeping ASGI as the boundary.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest
import uvicorn
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from featherweb import App, Get, Route, WebSocket, Ws
from featherweb._types import Scope
from featherweb.server.runner import Server
from featherweb.websocket import WebSocketDisconnect, WebSocketStateError

type Connect = Callable[..., Any]


@Route("/ws")
class SocketController:
    @Ws("/echo")
    async def echo(self, ws: WebSocket) -> None:
        await ws.accept()
        async for message in ws.iter_text():
            await ws.send_text(f"echo:{message}")

    @Ws("/binary")
    async def binary(self, ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_bytes((await ws.receive_bytes())[::-1])

    @Ws("/json")
    async def json(self, ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_json({"got": await ws.receive_json()})

    @Ws("/subprotocol")
    async def subprotocol(self, ws: WebSocket) -> None:
        offered = ws.subprotocols
        await ws.accept(subprotocol=offered[0] if offered else None)
        await ws.send_text(",".join(offered))

    @Ws("/room/{name}")
    async def room(self, ws: WebSocket, name: str) -> None:
        await ws.accept()
        await ws.send_text(name)

    @Ws("/query")
    async def query(self, ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_text(ws.query.get("who", "nobody"))

    @Ws("/headers")
    async def headers(self, ws: WebSocket) -> None:
        await ws.accept(headers=[("x-server", "featherweb")])
        await ws.send_text(ws.headers.get("x-client", "none"))

    @Ws("/closing")
    async def closing(self, ws: WebSocket) -> None:
        await ws.accept()
        await ws.close(4000, "bye now")

    @Ws("/denied")
    async def denied(self, ws: WebSocket) -> None:
        await ws.deny()

    @Ws("/boom")
    async def boom(self, ws: WebSocket) -> None:
        await ws.accept()
        raise RuntimeError("handler exploded")

    @Ws("/counts")
    async def counts(self, ws: WebSocket) -> None:
        await ws.accept()
        seen = 0
        try:
            while True:
                await ws.receive_text()
                seen += 1
        except WebSocketDisconnect:
            RECORD["seen"] = seen

    @Get("/plain")
    async def plain(self) -> str:
        return "http still works"


#: Written by a handler after the client has gone, so the test can check it.
RECORD: dict[str, Any] = {}


class _QuietServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:
        return None


@pytest.fixture(params=["featherweb", "uvicorn"])
async def connect(request: pytest.FixtureRequest) -> AsyncIterator[Connect]:
    """A ``websockets.connect`` bound to an application on a real port."""
    app = App(controllers=[SocketController])
    stop: Callable[[], Awaitable[None]]
    if request.param == "featherweb":
        server = Server(app, host="127.0.0.1", port=0)
        await server.startup()
        port, stop = server.bound_port, server.shutdown
    else:
        config = uvicorn.Config(
            app, host="127.0.0.1", port=0, log_level="warning", access_log=False, ws="auto"
        )
        running = _QuietServer(config)
        task = asyncio.create_task(running.serve())
        while not running.started:  # noqa: ASYNC110 - uvicorn has no "started" awaitable
            await asyncio.sleep(0.01)
        port = int(running.servers[0].sockets[0].getsockname()[1])

        async def stop_uvicorn() -> None:
            running.should_exit = True
            await task

        stop = stop_uvicorn

    def _connect(path: str, **kwargs: Any) -> Any:
        return websockets.connect(f"ws://127.0.0.1:{port}{path}", **kwargs)

    try:
        yield _connect
    finally:
        await stop()


# -- talking ----------------------------------------------------------------


async def test_text_echoes_back(connect: Connect) -> None:
    async with connect("/ws/echo") as ws:
        await ws.send("hello")
        assert await ws.recv() == "echo:hello"


async def test_the_connection_stays_open_for_more(connect: Connect) -> None:
    async with connect("/ws/echo") as ws:
        for index in range(5):
            await ws.send(f"m{index}")
            assert await ws.recv() == f"echo:m{index}"


async def test_binary_messages_stay_binary(connect: Connect) -> None:
    async with connect("/ws/binary") as ws:
        await ws.send(b"abc")
        assert await ws.recv() == b"cba"


async def test_json_goes_both_ways(connect: Connect) -> None:
    async with connect("/ws/json") as ws:
        await ws.send('{"a": 1}')
        assert await ws.recv() == '{"got":{"a":1}}'


async def test_a_large_message_survives_fragmentation(connect: Connect) -> None:
    """The client splits this into frames; the server has to put it back."""
    payload = "x" * 200_000
    async with connect("/ws/echo") as ws:
        await ws.send(payload)
        assert await ws.recv() == f"echo:{payload}"


async def test_an_empty_message_is_a_message(connect: Connect) -> None:
    async with connect("/ws/echo") as ws:
        await ws.send("")
        assert await ws.recv() == "echo:"


async def test_ping_is_answered(connect: Connect) -> None:
    async with connect("/ws/echo") as ws:
        await asyncio.wait_for(await ws.ping(b"beat"), timeout=5)


# -- the scope --------------------------------------------------------------


async def test_a_path_parameter_reaches_the_handler(connect: Connect) -> None:
    async with connect("/ws/room/lobby") as ws:
        assert await ws.recv() == "lobby"


async def test_the_query_string_is_there(connect: Connect) -> None:
    async with connect("/ws/query?who=ada") as ws:
        assert await ws.recv() == "ada"


async def test_request_headers_are_there(connect: Connect) -> None:
    async with connect("/ws/headers", additional_headers={"x-client": "pytest"}) as ws:
        assert await ws.recv() == "pytest"


async def test_a_subprotocol_is_negotiated(connect: Connect) -> None:
    async with connect("/ws/subprotocol", subprotocols=["chat", "superchat"]) as ws:
        assert ws.subprotocol == "chat"
        assert await ws.recv() == "chat,superchat"


# -- ending it --------------------------------------------------------------


async def test_the_server_can_close_with_a_code_and_reason(connect: Connect) -> None:
    with pytest.raises(ConnectionClosed) as info:
        async with connect("/ws/closing") as ws:
            await ws.recv()
    received = info.value.rcvd  # .code is deprecated in websockets 17
    assert received is not None
    assert (received.code, received.reason) == (4000, "bye now")


async def test_a_handler_that_never_accepts_refuses_the_handshake(connect: Connect) -> None:
    with pytest.raises(InvalidStatus) as info:
        async with connect("/ws/denied"):
            pass
    assert info.value.response.status_code == 403


async def test_a_path_with_no_websocket_refuses_the_handshake(connect: Connect) -> None:
    with pytest.raises(InvalidStatus):
        async with connect("/ws/nowhere"):
            pass


async def test_a_failing_handler_closes_with_1011(connect: Connect) -> None:
    with pytest.raises(ConnectionClosed) as info:
        async with connect("/ws/boom") as ws:
            await ws.recv()
    received = info.value.rcvd
    assert received is not None
    assert received.code == 1011


async def test_the_handler_sees_the_client_leave(connect: Connect) -> None:
    RECORD.clear()
    async with connect("/ws/counts") as ws:
        await ws.send("one")
        await ws.send("two")
    for _ in range(100):  # the disconnect reaches the handler just after we close
        if "seen" in RECORD:
            break
        await asyncio.sleep(0.01)
    assert RECORD.get("seen") == 2


async def test_http_still_works_on_the_same_server(connect: Connect) -> None:
    """An upgrade route must not disturb the ordinary requests beside it."""
    async with connect("/ws/echo") as ws:
        await ws.send("hi")
        assert await ws.recv() == "echo:hi"


# -- the state machine, without a socket ------------------------------------


def make_socket(messages: list[dict[str, Any]]) -> tuple[WebSocket, list[dict[str, Any]]]:
    queue = list(messages)
    sent: list[dict[str, Any]] = []

    async def receive() -> Any:
        return queue.pop(0) if queue else {"type": "websocket.disconnect", "code": 1005}

    async def send(message: Any) -> None:
        sent.append(dict(message))

    scope: Scope = {"type": "websocket", "path": "/x", "headers": [], "query_string": b""}
    return WebSocket(scope, receive, send), sent


async def test_sending_before_accepting_is_an_error() -> None:
    socket, _ = make_socket([{"type": "websocket.connect"}])
    with pytest.raises(WebSocketStateError, match="accept"):
        await socket.send_text("too early")


async def test_receiving_before_accepting_is_an_error() -> None:
    socket, _ = make_socket([{"type": "websocket.connect"}])
    with pytest.raises(WebSocketStateError, match="accept"):
        await socket.receive()


async def test_accepting_twice_is_an_error() -> None:
    socket, _ = make_socket([{"type": "websocket.connect"}])
    await socket.accept()
    with pytest.raises(WebSocketStateError, match="already been accepted"):
        await socket.accept()


async def test_closing_twice_sends_one_close() -> None:
    socket, sent = make_socket([{"type": "websocket.connect"}])
    await socket.accept()
    await socket.close()
    await socket.close()
    assert [message["type"] for message in sent].count("websocket.close") == 1


async def test_a_disconnect_while_accepting_is_raised() -> None:
    socket, _ = make_socket([{"type": "websocket.disconnect", "code": 1001}])
    with pytest.raises(WebSocketDisconnect) as info:
        await socket.accept()
    assert info.value.code == 1001


async def test_text_asked_for_but_bytes_arrived_closes_the_connection() -> None:
    socket, sent = make_socket(
        [{"type": "websocket.connect"}, {"type": "websocket.receive", "bytes": b"x"}]
    )
    await socket.accept()
    with pytest.raises(WebSocketDisconnect) as info:
        await socket.receive_text()
    assert info.value.code == 1003
    assert sent[-1]["code"] == 1003


async def test_json_that_is_not_json_closes_with_1007() -> None:
    socket, sent = make_socket(
        [{"type": "websocket.connect"}, {"type": "websocket.receive", "text": "{oops"}]
    )
    await socket.accept()
    with pytest.raises(WebSocketDisconnect):
        await socket.receive_json()
    assert sent[-1]["code"] == 1007


async def test_iterating_ends_on_disconnect_instead_of_raising() -> None:
    socket, _ = make_socket(
        [
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": "a"},
            {"type": "websocket.receive", "text": "b"},
            {"type": "websocket.disconnect", "code": 1000},
        ]
    )
    await socket.accept()
    assert [message async for message in socket.iter_text()] == ["a", "b"]


async def test_accept_can_choose_a_subprotocol_and_headers() -> None:
    socket, sent = make_socket([{"type": "websocket.connect"}])
    await socket.accept(subprotocol="chat", headers=[("X-Server", "featherweb")])
    assert sent[0]["subprotocol"] == "chat"
    assert sent[0]["headers"] == [(b"x-server", b"featherweb")]


async def test_denying_after_accepting_is_an_error() -> None:
    socket, _ = make_socket([{"type": "websocket.connect"}])
    await socket.accept()
    with pytest.raises(WebSocketStateError, match="already been accepted"):
        await socket.deny()


def test_the_repr_says_which_state_it_is_in() -> None:
    socket, _ = make_socket([])
    assert "connecting" in repr(socket)


async def test_iterating_bytes_ends_on_disconnect() -> None:
    socket, _ = make_socket(
        [
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "bytes": b"a"},
            {"type": "websocket.receive", "bytes": b"b"},
            {"type": "websocket.disconnect", "code": 1000},
        ]
    )
    await socket.accept()
    assert [chunk async for chunk in socket.iter_bytes()] == [b"a", b"b"]


async def test_iterating_json_ends_on_disconnect() -> None:
    socket, _ = make_socket(
        [
            {"type": "websocket.connect"},
            {"type": "websocket.receive", "text": '{"n": 1}'},
            {"type": "websocket.disconnect", "code": 1000},
        ]
    )
    await socket.accept()
    assert [payload async for payload in socket.iter_json()] == [{"n": 1}]


async def test_json_is_read_from_a_binary_message_too() -> None:
    socket, _ = make_socket(
        [{"type": "websocket.connect"}, {"type": "websocket.receive", "bytes": b'{"n": 2}'}]
    )
    await socket.accept()
    assert await socket.receive_json() == {"n": 2}


async def test_a_message_with_no_payload_closes_with_1003() -> None:
    socket, sent = make_socket([{"type": "websocket.connect"}, {"type": "websocket.receive"}])
    await socket.accept()
    with pytest.raises(WebSocketDisconnect):
        await socket.receive_json()
    assert sent[-1]["code"] == 1003


async def test_bytes_asked_for_but_text_arrived_closes_the_connection() -> None:
    socket, sent = make_socket(
        [{"type": "websocket.connect"}, {"type": "websocket.receive", "text": "x"}]
    )
    await socket.accept()
    with pytest.raises(WebSocketDisconnect) as info:
        await socket.receive_bytes()
    assert info.value.code == 1003
    assert sent[-1]["code"] == 1003


async def test_sending_after_a_close_is_refused() -> None:
    socket, _ = make_socket([{"type": "websocket.connect"}])
    await socket.accept()
    await socket.close()
    with pytest.raises(WebSocketDisconnect):
        await socket.send_text("too late")


async def test_receiving_after_a_close_is_refused() -> None:
    socket, _ = make_socket([{"type": "websocket.connect"}])
    await socket.accept()
    await socket.close()
    with pytest.raises(WebSocketDisconnect):
        await socket.receive()


async def test_accepting_a_closed_connection_is_an_error() -> None:
    socket, _ = make_socket([{"type": "websocket.connect"}])
    await socket.deny()
    with pytest.raises(WebSocketStateError, match="closed"):
        await socket.accept()


async def test_a_message_that_is_not_the_handshake_is_an_error() -> None:
    socket, _ = make_socket([{"type": "websocket.receive", "text": "early"}])
    with pytest.raises(WebSocketStateError, match=r"expected websocket\.connect"):
        await socket.accept()


async def test_the_flags_track_the_state() -> None:
    socket, _ = make_socket([{"type": "websocket.connect"}])
    assert not socket.accepted
    assert not socket.closed
    await socket.accept()
    assert socket.accepted
    assert not socket.closed
    assert "open" in repr(socket)
    await socket.close()
    assert socket.closed
    assert "closed" in repr(socket)
