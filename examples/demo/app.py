"""A demo application exercising everything featherweb can do so far.

Run it and open http://127.0.0.1:8000, which serves a page that pokes every
route below, WebSocket included::

    python examples/demo/app.py
    python examples/demo/app.py --port 9000

Or through the CLI, from the repository root::

    python -m featherweb run examples.demo.app:app

It is also an ordinary ASGI application, so any server can host it::

    uvicorn examples.demo.app:app
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

from featherweb import (
    CORS,
    App,
    ControllerAdvice,
    Delete,
    ExceptionHandler,
    FileResponse,
    Form,
    Get,
    GZip,
    Header,
    HTTPError,
    Middleware,
    Next,
    Post,
    Query,
    Request,
    Response,
    Route,
    StreamingResponse,
    UploadFile,
    WebSocket,
    Ws,
)
from featherweb.staticfiles import StaticFiles

HERE = Path(__file__).parent
STATIC = HERE / "static"


# -- the model ---------------------------------------------------------------


class Colour(Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"


@dataclass
class NewTask:
    """What a client sends to create a task; this shape is the validation."""

    title: str
    done: bool = False
    colour: Colour = Colour.BLUE
    tags: list[str] = field(default_factory=list[str])


@dataclass
class Task:
    """What a client gets back. Fields outside this type are never sent."""

    id: int
    title: str
    done: bool
    colour: str
    tags: list[str]


class TaskNotFound(Exception):
    """Raised by the store, turned into a 404 by the handler below."""

    def __init__(self, task_id: int) -> None:
        self.task_id = task_id
        super().__init__(f"no task {task_id}")


TASKS: dict[int, Task] = {
    1: Task(id=1, title="write the parser", done=True, colour="green", tags=["http"]),
    2: Task(id=2, title="serve a websocket", done=True, colour="blue", tags=["ws"]),
    3: Task(id=3, title="sign a cookie", done=False, colour="red", tags=["auth"]),
}


# -- middlewares -------------------------------------------------------------


@Middleware(order=10)
class Timing:
    """Adds how long the handler took, so the demo page can show it."""

    async def __call__(self, request: Request, call_next: Next) -> Response[Any]:
        started = asyncio.get_running_loop().time()
        response = await call_next(request)
        elapsed = (asyncio.get_running_loop().time() - started) * 1000
        response.headers["x-elapsed-ms"] = f"{elapsed:.2f}"
        return response


# -- HTTP --------------------------------------------------------------------


@Route
class PageController:
    """The demo page itself, plus the files it loads."""

    @Get
    async def index(self) -> FileResponse:
        return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8")

    @Get("/health")
    async def health(self) -> dict[str, str]:
        return {"status": "ok", "time": datetime.now(UTC).isoformat()}


@Route("/api/tasks")
class TaskController:
    """Input and output inferred from the annotations, nothing declared twice."""

    @Get
    async def index(self, done: bool | None = None, limit: int = 10) -> list[Task]:
        """``?done=true&limit=2`` — both read from the query string and converted."""
        tasks = list(TASKS.values())
        if done is not None:
            tasks = [task for task in tasks if task.done is done]
        return tasks[:limit]

    @Get("/{task_id:int}")
    async def show(self, task_id: int) -> Task:
        """``task_id`` matches the route, so it is the path parameter."""
        if task_id not in TASKS:
            raise TaskNotFound(task_id)
        return TASKS[task_id]

    @Post(status=201)
    async def create(self, task: NewTask) -> Task:
        """A dataclass parameter can only be the JSON body, so it is."""
        task_id = max(TASKS, default=0) + 1
        created = Task(
            id=task_id,
            title=task.title,
            done=task.done,
            colour=task.colour.value,
            tags=task.tags,
        )
        TASKS[task_id] = created
        return created

    @Delete("/{task_id:int}")
    async def delete(self, task_id: int) -> None:
        """No return annotation but ``None``, so this answers 204."""
        if TASKS.pop(task_id, None) is None:
            raise TaskNotFound(task_id)

    @Get("/echo/headers")
    async def echo_headers(
        self,
        agent: Annotated[str, Header("user-agent")] = "unknown",
        per_page: Annotated[int, Query("perPage")] = 25,
    ) -> dict[str, Any]:
        """Markers pin a parameter to a source and give it the name on the wire."""
        return {"agent": agent, "per_page": per_page}


@Route("/api/files")
class FileController:
    """Bodies that do not fit in memory, in both directions."""

    @Get("/stream")
    async def stream(self, lines: int = 5, delay: float = 0.3) -> StreamingResponse:
        """Sent a chunk at a time: watch it arrive rather than appear at once."""

        async def produce() -> AsyncIterator[str]:
            for index in range(1, lines + 1):
                yield f"line {index} of {lines}\n"
                await asyncio.sleep(delay)

        return StreamingResponse(produce(), media_type="text/plain; charset=utf-8")

    @Get("/download")
    async def download(self, request: Request) -> FileResponse:
        """Handles ETag, If-None-Match and Range, so a reload answers 304."""
        return FileResponse(STATIC / "sample.txt", request=request, filename="sample.txt")

    @Post("/upload")
    async def upload(
        self,
        note: Annotated[str, Form()] = "",
        document: UploadFile | None = None,
    ) -> dict[str, Any]:
        """Spooled to disk as it arrives; a big file never sits in memory."""
        if document is None:
            raise HTTPError(400, "attach a file under the name 'document'")
        head = (await document.read(60)).decode("utf-8", "replace")
        return {
            "note": note,
            "filename": document.filename,
            "content_type": document.content_type,
            "size": document.size,
            "spooled_to_disk": document.spooled_to_disk,
            "starts_with": head,
        }


@Route("/api/errors")
class ErrorController:
    """What the framework does when something goes wrong."""

    @Get("/http/{status:int}")
    async def raise_http(self, status: int) -> None:
        raise HTTPError(status, f"you asked for a {status}")

    @Get("/boom")
    async def boom(self) -> None:
        raise RuntimeError("this one was not expected")


# -- WebSocket ---------------------------------------------------------------


@Route("/ws")
class SocketController:
    @Ws("/echo")
    async def echo(self, ws: WebSocket) -> None:
        """Every message comes back with a prefix, until the client leaves."""
        await ws.accept()
        await ws.send_text("connected — type something")
        async for message in ws.iter_text():
            await ws.send_text(f"echo: {message}")

    @Ws("/clock")
    async def clock(self, ws: WebSocket) -> None:
        """Pushes without being asked, which is the point of a socket."""
        await ws.accept()
        try:
            while True:
                await ws.send_json({"now": datetime.now(UTC).strftime("%H:%M:%S")})
                await asyncio.sleep(1)
        except Exception:  # the client closed the tab
            return

    @Ws("/room/{name}")
    async def room(self, ws: WebSocket, name: str) -> None:
        """A path parameter reaches a socket handler like any other."""
        await ws.accept()
        await ws.send_text(f"welcome to {name}")
        async for message in ws.iter_text():
            await ws.send_text(f"[{name}] {message}")


# -- errors ------------------------------------------------------------------


@ControllerAdvice
class Errors:
    """One place to turn the application's own exceptions into responses."""

    @ExceptionHandler(TaskNotFound)
    async def not_found(self, exc: TaskNotFound) -> Response[dict[str, Any]]:
        return Response({"detail": f"task {exc.task_id} does not exist"}, status=404)


# -- the application ---------------------------------------------------------

app = App(
    controllers=[
        PageController,
        TaskController,
        FileController,
        ErrorController,
        SocketController,
        Errors,
    ],
    middlewares=[Timing, GZip(minimum_size=256), CORS(allow_origins=["*"])],
    debug=True,
)
app.mount("/static", StaticFiles(STATIC, max_age=60))


@app.on_startup
def announce() -> None:
    print("featherweb demo ready — open http://127.0.0.1:8000")


if __name__ == "__main__":
    import argparse

    from featherweb.server.runner import run

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    options = parser.parse_args()
    run(app, host=options.host, port=options.port)
