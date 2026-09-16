# featherweb

A lightweight async web framework for Python with **zero required dependencies** — the
standard library and nothing else. Class-based controllers in the shape of Spring Boot,
input and output inferred from your type hints, and its own HTTP/1.1 and WebSocket server
built on `asyncio`.

```python
from dataclasses import dataclass

from featherweb import App, Get, Post, Route


@dataclass
class Task:
    id: int
    title: str
    done: bool = False


TASKS = [Task(1, "write a framework"), Task(2, "write its README", done=True)]


@Route("/tasks")
class TaskController:
    @Get
    async def index(self, done: bool | None = None) -> list[Task]:
        return [task for task in TASKS if done is None or task.done is done]

    @Get("/{task_id:int}")
    async def show(self, task_id: int) -> Task:
        return TASKS[task_id - 1]

    @Post(status=201)
    async def create(self, task: Task) -> Task:
        TASKS.append(task)
        return task


app = App(controllers=[TaskController])

if __name__ == "__main__":
    app.run()
```

```sh
python app.py                      # http://127.0.0.1:8000/tasks
featherweb run app:app --port 9000 # or through the CLI
uvicorn app:app                    # or on any ASGI server
```

Nothing is declared twice. `done: bool | None` is a query parameter because that is the
only place a bool could come from; `task_id` is a path parameter because the route says
so; `task: Task` is the JSON body because a dataclass cannot be anything else. The return
annotation decides the response shape, and a field outside it is never sent.

## Install

```sh
pip install featherweb
```

Python 3.12 or newer. There is one optional extra, and only for the JWT algorithms the
standard library cannot do:

```sh
pip install "featherweb[crypto]"   # adds RS256/RS384/RS512 and ES256/ES384/ES512
```

## What it does

### Input, inferred

A handler receives exactly what it declares. The rules are applied in order, and anything
that fits none of them is an error when the route is registered, not a surprise in
production.

```python
from typing import Annotated
from uuid import UUID

from featherweb import Cookie, Get, Header, Query, Request, Route


@Route("/search")
class SearchController:
    @Get("/{tenant:uuid}")
    async def search(
        self,
        tenant: UUID,  # named in the route -> path
        request: Request,  # framework types, as themselves
        q: str = "",  # simple type -> query string
        tags: list[str] | None = None,  # repeatable query parameter
        per_page: Annotated[int, Query("perPage")] = 25,  # a different name on the wire
        agent: Annotated[str, Header("user-agent")] = "",
        session: Annotated[str | None, Cookie()] = None,
    ) -> dict[str, object]: ...
```

A value that will not convert is a **422** naming the field and the reason, with every
problem in the request reported at once rather than one per round trip:

```json
{"detail": [{"location": "query", "field": "perPage", "message": "expected an integer"}]}
```

### Output, from the return type

`-> None` answers 204, `-> str` is `text/plain`, `-> bytes` is `application/octet-stream`,
and anything else is JSON shaped by the annotation. Fields are read by attribute, so an
ORM row serializes as happily as a dataclass — and whatever it carries beyond the declared
type is left out, which is how a password column stops being an incident.

For a body plus a status, headers or cookies, return `Response[T]`:

```python
from featherweb import Post, Response


@Post(status=201)
async def create(self, task: Task) -> Response[Task]:
    response = Response(task, headers={"location": f"/tasks/{task.id}"})
    response.set_cookie("last_created", str(task.id), httponly=True)
    return response
```

### Files and streaming

```python
from collections.abc import AsyncIterator

from featherweb import FileResponse, Get, Request, Route, StreamingResponse, UploadFile
from featherweb.staticfiles import StaticFiles


@Route("/files")
class FileController:
    @Get("/report")
    async def report(self, request: Request) -> FileResponse:
        # ETag, Last-Modified, If-None-Match -> 304, and Range -> 206, all handled.
        return FileResponse("report.pdf", request=request, filename="report.pdf")

    @Get("/events")
    async def events(self) -> StreamingResponse:
        async def ticks() -> AsyncIterator[str]:
            for index in range(10):
                yield f"tick {index}\n"

        return StreamingResponse(ticks(), media_type="text/plain")

    @Post("/upload")
    async def upload(self, document: UploadFile) -> dict[str, object]:
        # Parsed off the socket as it arrives and spooled to disk past 1 MB,
        # so a 100 MB upload costs a buffer and a temporary file, not 100 MB.
        return {"name": document.filename, "size": document.size}


app.mount("/static", StaticFiles("assets", max_age=3600))
```

### WebSocket

```python
from featherweb import Route, WebSocket, Ws


@Route("/ws")
class SocketController:
    @Ws("/echo")
    async def echo(self, ws: WebSocket) -> None:
        await ws.accept()
        async for message in ws.iter_text():
            await ws.send_text(f"echo: {message}")
```

Framing, masking, fragmentation and ping/pong happen below you, in whichever server is
running — featherweb's own or uvicorn's. The handler is the same either way.

### Authentication

Two strategies, one configuration point:

```python
from featherweb import App, Authenticated, Get, Identity, Roles, Route, Session, SessionAuth

auth = SessionAuth(secret=SECRET)


@Route("/admin")
@Authenticated  # applies to every method below
class AdminController:
    @Get("/me")
    async def me(self, identity: Identity) -> dict[str, object]:
        return {"id": identity.id, "roles": sorted(identity.roles)}

    @Get("/reports")
    @Roles("admin")  # the class's rule *and* this one
    async def reports(self) -> list[str]:
        return ["everything"]


app = App(controllers=[AdminController], auth=auth)
```

No identity is **401**; an identity without the role is **403**. Swap `SessionAuth` for
`JWTAuth(SECRET, algorithms=["HS256"])` and the handlers do not change.

The JWT layer follows RFC 8725: the caller names the algorithms and a token never gets to
choose how it is verified, `alg: none` is refused whatever you ask for, and nothing in the
payload is read before the signature checks out.

### Middleware and errors

```python
from featherweb import ControllerAdvice, ExceptionHandler, Middleware, Next, Request, Response


@Middleware(order=10)  # lowest order is the outermost layer
class Timing:
    async def __call__(self, request: Request, call_next: Next) -> Response:
        response = await call_next(request)
        response.headers["x-served-by"] = "featherweb"
        return response


@ControllerAdvice  # or on a controller, for that controller only
class Errors:
    @ExceptionHandler(TaskNotFound)
    async def not_found(self, exc: TaskNotFound) -> Response[dict[str, str]]:
        return Response({"detail": str(exc)}, status=404)
```

### Testing

`TestClient` calls the application directly, with no socket and no server:

```python
from featherweb.testing import TestClient


async def test_tasks_are_listed():
    async with TestClient(app) as client:
        response = await client.get("/tasks", params={"done": True})
    assert response.status == 200
    assert response.json()[0]["title"] == "write its README"
```

## Running it

```sh
featherweb run app:app --host 0.0.0.0 --port 8000
featherweb run app:app --workers 4                  # shares the port via SO_REUSEPORT
featherweb run app:app --ssl-certfile cert.pem --ssl-keyfile key.pem
```

`--workers` needs `SO_REUSEPORT`, so on Windows it falls back to one process and says so.
Being an ASGI 3 application, it also runs anywhere else:

```sh
uvicorn app:app --workers 4
hypercorn app:app
granian --interface asgi app:app
```

And the bundled server runs any ASGI application, not only featherweb's:

```python
from featherweb.server.runner import run

run(some_starlette_app, port=8000)
```

## How it is built

- **ASGI 3 is the boundary, both ways.** The app runs under uvicorn, hypercorn or granian;
  the server runs anyone's ASGI app. The test suite exercises both directions.
- **Cost at registration, not per request.** Route converters, parameter extractors and
  return serializers are compiled once when a controller is registered. Nothing that can
  be decided at startup — including duplicate routes — is decided per request.
- **Lazy everything.** Body, JSON, form, cookies and query parse on first access.
- **No global magic.** No implicit global or contextvar `request`, and no dependency
  injection container: a handler gets what it declares, and only framework types
  (`Request`, `WebSocket`, `Identity`, `Session`) are injectable.
- **Import budget.** Heavy modules — multipart, websocket, staticfiles, auth, and even
  `logging` — stay off the package's import path until something needs them.

Measured on Windows with Python 3.12 (`python benchmarks/measure.py --all`): 20 ms to
import in a bare interpreter and 9.7 ms in a process that already has `typing` loaded,
16.8 MB resident with an application built, and throughput within about 5% of
Starlette on the same uvicorn. [PLAN.md](PLAN.md) records all of it, including the two
targets that were missed and why.

## What it is not

No ORM, no dependency injection container, no database integration, no OAuth, no HTTP/2 or
HTTP/3, and no automatic OpenAPI yet — the type hints that would generate it are in place,
but generating it is not. Those are deliberate omissions, not a roadmap gap.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync                     # create .venv (Python 3.12) with the dev dependencies
uv run pytest               # 829 tests, across TestClient, the bundled server and uvicorn
uv run ruff check           # lint
uv run ruff format          # formatting
uv run pyright              # type checking, strict
python benchmarks/measure.py --all   # the targets in PLAN.md section 2
python examples/demo/app.py          # a page that pokes every feature above
```

The API reference is [docs/api.md](docs/api.md). The roadmap, the architecture and the
per-component security requirements are in [PLAN.md](PLAN.md), which is written in
Portuguese, and [RELEASING.md](RELEASING.md) covers cutting a version.

## Status

Version 0.1: everything above works and is tested, but the public API may still move
before 1.0. The two areas carrying the most risk — the HTTP/1.1 parser and the
authentication layer — are built to the requirements in PLAN.md section 6, with the
request-smuggling and token-forgery cases covered by tests.

## License

MIT — see [LICENSE](LICENSE).
