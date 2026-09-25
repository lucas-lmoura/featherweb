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

```sh
$ curl localhost:8000/tasks?done=true
[{"id": 2, "title": "write its README", "done": true}]

$ curl -X POST localhost:8000/tasks -d '{"id": 3, "title": "ship it"}'
{"id": 3, "title": "ship it", "done": false}          # 201 Created
```

Nothing is declared twice. `done: bool | None` is a query parameter because that is the
only place a bool could come from; `task_id` is a path parameter because the route says
so; `task: Task` is the JSON body because a dataclass cannot be anything else. The return
annotation decides the response shape, and a field outside it is never sent.

## Contents

- [Install](#install)
- [Why featherweb](#why-featherweb)
- [How it works](#how-it-works)
- [Guide](#guide): [controllers and routing](#controllers-and-routing) ·
  [input](#input-inferred) · [output](#output-from-the-return-type) ·
  [errors](#errors) · [middleware](#middleware) · [lifespan](#lifespan) ·
  [files and streaming](#files-uploads-and-streaming) · [WebSocket](#websocket) ·
  [authentication](#authentication) · [testing](#testing)
- [Running in production](#running-in-production)
- [Compared with other frameworks](#compared-with-other-frameworks)
- [What it is not](#what-it-is-not)
- [Development](#development)

## Install

```sh
pip install featherweb
# or
uv add featherweb
```

Python 3.12 or newer, on Linux, macOS and Windows. Installing it installs nothing else.
There is one optional extra, and only for the JWT algorithms the standard library cannot
do:

```sh
pip install "featherweb[crypto]"   # adds RS256/RS384/RS512 and ES256/ES384/ES512
```

## Why featherweb

Most Python web frameworks make you choose. Starlette is small and fast but hands you a
raw request to pick apart yourself. FastAPI reads your type hints but brings Pydantic,
Starlette and a dependency-injection system with it, and needs a separate server. Django
and Flask give you structure, but they were designed for the synchronous world.

featherweb is an attempt to keep the good part of each and leave the weight behind:

- **Types are the contract.** You annotate a handler the way you would annotate any
  function, and that annotation is the whole specification: where each argument comes
  from, how it is converted, what gets rejected with a 422, and what the response looks
  like. There is no second schema to keep in sync.
- **Controllers, not loose functions.** Related routes live in one class under one
  prefix, with guards and error handlers that apply to the whole class. Anyone who has
  used Spring Boot or ASP.NET recognises the shape immediately.
- **Nothing to install.** Zero runtime dependencies means nothing to audit, nothing to pin
  and nothing that breaks on a transitive upgrade. It imports in about 20 ms, which matters
  for CLIs, serverless cold starts and test suites that import the app thousands of times.
- **The server is included.** An HTTP/1.1 and WebSocket server written for this framework,
  with request-smuggling defences and conservative limits. It is still plain ASGI 3
  underneath, so uvicorn, hypercorn and granian work just as well.
- **Errors at startup, not in production.** A handler parameter the framework cannot
  provide, a duplicate route or a path converter that does not match its annotation is an
  exception when the app is built, not a 500 on the first request that hits it.

It is a good fit for JSON APIs, internal services, small sites and tools where you want
structure and typed input without a stack of dependencies. See
[What it is not](#what-it-is-not) for where it does not fit.

## How it works

```text
          featherweb's server or uvicorn / hypercorn / granian
                               │
  socket ─► HTTP/1.1 parser ─► ASGI 3 (scope, receive, send)
                                          │
                                          ▼
                                         App ─► middlewares ─► auth ─► Router
                                                                        │
                                             controller method ◄────────┘
                                                 │
                                                 ▼
                                   serializer from the return type ─► response
```

The work splits into two moments, and as much as possible happens in the first:

**When the app is built** (`App(controllers=[...])` or `app.scan("package")`), every
handler is inspected once. Its signature becomes a list of small extractor functions (one
per parameter, already knowing whether to read the path, the query, a header or the body,
and how to convert it), its return annotation becomes a serializer, and its route becomes
an entry in the router with the path converters compiled. Anything wrong is raised here.

**When a request arrives**, the server parses it into an ASGI scope and hands it to the
app. The middlewares wrap the call in `order`, the auth strategy attaches an `Identity` if
the route needs one, the router finds the controller method, and the precompiled
extractors run. Every validation failure is collected into a single 422. The handler's
return value then goes through the precompiled serializer. Nothing is parsed before it is
needed: the body, JSON, form data, cookies and query string are all read on first access.

A few rules follow from that design and are kept on purpose:

- **ASGI 3 is the boundary, both ways.** The app runs under any ASGI server, and the
  bundled server runs any ASGI app, Starlette or Django included. The test suite runs the
  same tests through `TestClient`, featherweb's server and uvicorn.
- **No global magic.** There is no implicit global `request` and no dependency-injection
  container. A handler receives exactly what it declares, and the only injectable objects
  are the framework's own: `Request`, `WebSocket`, `Identity` and `Session`.
- **Pay for what you use.** Multipart, WebSocket, static files and auth are separate
  modules that are only imported the first time something asks for them.

## Guide

### Controllers and routing

A controller is a plain class decorated with `@Route`. Each method decorated with a verb
becomes a route, and the paths are joined with the class prefix:

```python
from uuid import UUID

from featherweb import App, Delete, Get, Put, Route


@Route("/users")
class UserController:
    @Get  # GET /users
    async def index(self) -> list[str]: ...

    @Get("/{user_id:uuid}")  # GET /users/3f2a...  (anything else is a 404)
    async def show(self, user_id: UUID) -> dict[str, str]: ...

    @Put("/{user_id:uuid}")
    async def update(self, user_id: UUID, name: str) -> None: ...  # None -> 204

    @Delete("/{user_id:uuid}", status=202)
    async def remove(self, user_id: UUID) -> None: ...


@Route  # no prefix
class HealthController:
    @Get("/health")
    async def health(self) -> str:
        return "ok"


app = App(controllers=[UserController, HealthController])
```

- Verbs: `Get`, `Post`, `Put`, `Patch`, `Delete`, `Options`, `Head`, and `Ws` for
  WebSocket. A `GET` route answers `HEAD` for free.
- Path converters: `{name}`, `{name:int}`, `{name:float}`, `{name:uuid}` and
  `{name:path}`, which swallows the rest of the path. A value that does not match the
  converter means the route does not match, so the answer is a 404, not a 422.
- `/users` and `/users/` are the same route.
- Controllers are instantiated once, without arguments, when the app is built.

In a larger project, mark controllers where they live and let the app find them:

```python
from featherweb import App

app = App()
app.scan("myproject.controllers")  # imports the package recursively and registers them
```

### Input, inferred

A handler receives exactly what it declares. For each parameter the first rule that fits
wins:

1. its name appears in the route path → **path** parameter;
2. it is `Request`, `WebSocket`, `Identity` or `Session` → **the object itself**;
3. it is a dataclass, `TypedDict` or pydantic-style model → the **JSON body**;
4. it is `UploadFile` or `list[UploadFile]` → an **uploaded file**;
5. it is a simple type (`str`, `int`, `float`, `bool`, `Enum`, `Literal`, `UUID`,
   `datetime`, `date`, `time`, `Decimal`, `X | None`, `list[X]`) → the **query string**;
6. `Annotated[T, marker]` overrides all of the above.

Anything that fits none of them is an error when the route is registered, not a surprise
in production. A parameter with a default is optional, and one without it is required.

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

The markers are `Query`, `Header`, `Cookie`, `Form`, `File` and `Body`. `Body()` takes the
whole JSON body as any type, and `Body("field")` picks one field out of it:

```python
from typing import Annotated

from featherweb import Body, Form, Post, Route


@Route("/auth")
class LoginController:
    @Post("/token")
    async def token(self, username: Annotated[str, Body("username")]) -> dict[str, str]: ...

    @Post("/form")  # an HTML form, url-encoded or multipart
    async def form(self, email: Annotated[str, Form()]) -> str: ...
```

A value that will not convert is a **422** naming the field and the reason, with every
problem in the request reported at once rather than one per round trip:

```json
{"detail": [{"location": "query", "field": "perPage", "message": "expected an integer"}]}
```

### Output, from the return type

The return annotation decides the response:

| Annotation | Response |
|---|---|
| `-> None` | `204 No Content` |
| `-> str` | `text/plain` |
| `-> bytes` | `application/octet-stream` |
| `-> Response[T]`, `FileResponse`, `StreamingResponse`, `RedirectResponse` | as built |
| anything else | JSON shaped by the annotation |

JSON output is built from the declared type, not from the object you return. Fields are
read by attribute, so an ORM row serializes as well as a dataclass does. Whatever the object
carries beyond the declared type is left out, which is how a password column stops being
an incident:

```python
from dataclasses import dataclass

from featherweb import Get, Route


@dataclass
class PublicUser:
    id: int
    name: str


@Route("/users")
class UserController:
    @Get("/{user_id:int}")
    async def show(self, user_id: int) -> PublicUser:
        return await database.fetch_user(user_id)  # has .password_hash too; it is not sent
```

For a body plus a status, headers or cookies, return `Response[T]`. The body is still
shaped by `T`:

```python
from featherweb import Post, Response


@Post(status=201)
async def create(self, task: Task) -> Response[Task]:
    response = Response(task, headers={"location": f"/tasks/{task.id}"})
    response.set_cookie("last_created", str(task.id), httponly=True)
    return response
```

### Errors

Raise `HTTPError` anywhere for an HTTP answer, or subclass it for errors of your own:

```python
from featherweb import HTTPError


class TaskNotFound(HTTPError):
    status = 404
    detail = "no such task"


raise HTTPError(409, "that title is taken")  # {"detail": "that title is taken"}
```

To turn *any* exception into a response, write a handler. On a controller it applies to
that controller only. On a `@ControllerAdvice` class it applies to the whole app. The most
specific handler wins, local before global:

```python
from featherweb import ControllerAdvice, ExceptionHandler, Response


@ControllerAdvice
class Errors:
    @ExceptionHandler(LookupError)
    async def not_found(self, exc: LookupError) -> Response[dict[str, str]]:
        return Response({"detail": str(exc)}, status=404)

    @ExceptionHandler(PermissionError)
    async def forbidden(self, exc: PermissionError) -> Response[dict[str, str]]:
        return Response({"detail": "not allowed"}, status=403)
```

An unhandled exception is logged and answered with a 500 whose body says only
`{"detail": "internal server error"}`. `App(debug=True)` puts the traceback there instead,
which is useful in development and should stay off in production.

### Middleware

A middleware is a class with `async __call__(request, call_next)`. The lowest `order` is
the outermost layer, and because exceptions become responses inside the chain, a
middleware sees 404s and 500s too:

```python
from featherweb import App, CORS, GZip, Middleware, Next, Request, Response


@Middleware(order=10)
class Timing:
    async def __call__(self, request: Request, call_next: Next) -> Response:
        response = await call_next(request)
        response.headers["x-served-by"] = "featherweb"
        return response


app = App(
    controllers=[TaskController],
    middlewares=[Timing, GZip(minimum_size=500), CORS(allow_origins=["https://example.com"])],
)
```

`CORS` and `GZip` are included. A class is instantiated for you, and an instance is used
as it is, which is how a configured middleware gets in.

### Lifespan

Open connections and pools when the server starts, and close them when it stops. Hooks
can be sync or async:

```python
from featherweb import App

app = App(controllers=[TaskController])
pool = DatabasePool(DATABASE_URL)


@app.on_startup
async def connect() -> None:
    await pool.open()


@app.on_shutdown
async def disconnect() -> None:
    await pool.close()
```

### Files, uploads and streaming

```python
from collections.abc import AsyncIterator

from featherweb import FileResponse, Get, Post, Request, Route, StreamingResponse, UploadFile
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

`StaticFiles` refuses path traversal twice. The path segments are checked before anything
touches the disk, and the resolved path must still be inside the directory afterwards,
which also catches a symlink pointing out of it.

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
running, featherweb's own or uvicorn's. The handler is the same either way. There are
also `receive_json`/`send_json` and `iter_bytes`/`iter_json`, and a disconnect ends the
loop cleanly.

### Authentication

Choose a strategy, pass it to `App`, and guard controllers with `@Authenticated` and
`@Roles`. Sessions live in a signed cookie, so there is no server-side store:

```python
from dataclasses import dataclass

from featherweb import (
    App,
    Authenticated,
    Get,
    HTTPError,
    Identity,
    Post,
    Roles,
    Route,
    Session,
    SessionAuth,
)

auth = SessionAuth(secret=SECRET)  # a list of secrets rotates keys without logouts


@dataclass
class Credentials:
    username: str
    password: str


@Route("/account")
class AccountController:
    @Post("/login")
    async def login(self, credentials: Credentials, session: Session) -> dict[str, str]:
        user = await users.check_password(credentials.username, credentials.password)
        if user is None:
            raise HTTPError(401, "wrong username or password")
        auth.login(session, user.name, roles=user.roles)
        return {"hello": user.name}

    @Post("/logout")
    async def logout(self, session: Session) -> None:
        auth.logout(session)


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


app = App(controllers=[AccountController, AdminController], auth=auth)
```

No identity is **401**, and an identity without the role is **403**. For an API, swap in
bearer tokens. The guarded handlers do not change:

```python
from featherweb import JWTAuth

auth = JWTAuth(SECRET, algorithms=["HS256"])
token = auth.issue("ada", roles=["admin"], expires_in=3600)  # Authorization: Bearer <token>
```

The JWT layer follows RFC 8725. You name the algorithms, and a token never gets to choose
how it is verified. `alg: none` is refused whatever you ask for, `exp`/`nbf`/`iat` are
checked, and nothing in the payload is read before the signature checks out. Signatures
are compared with `hmac.compare_digest`, and session cookies default to `HttpOnly`,
`Secure` and `SameSite=Lax`.

### Testing

`TestClient` calls the application directly, with no socket and no server, and runs the
lifespan hooks:

```python
from featherweb.testing import TestClient


async def test_tasks_are_listed():
    async with TestClient(app) as client:
        response = await client.get("/tasks", params={"done": True})
    assert response.status == 200
    assert response.json()[0]["title"] == "write its README"


async def test_a_bad_id_is_a_422():
    async with TestClient(app) as client:
        response = await client.post("/tasks", json={"id": "nope", "title": "x"})
    assert response.status == 422
```

## Running in production

The bundled server is meant for production, not only development:

```sh
featherweb run app:app --host 0.0.0.0 --port 8000
featherweb run app:app --workers 4                  # one process per core, same port
featherweb run app:app --ssl-certfile cert.pem --ssl-keyfile key.pem
```

It enforces conservative limits by default: an 8 KB request line, 64 KB of headers across
at most 100 fields, a 1 MB body (`App(max_body_size=...)`) and a 10 s header timeout
against slowloris. It rejects the request-smuggling vectors: `Content-Length` together
with `Transfer-Encoding`, conflicting `Content-Length` values, obs-fold and invalid header
names. Put it behind a reverse proxy (nginx, Caddy, a cloud load balancer) as you would any
Python server.

`--workers` shares the port with `SO_REUSEPORT`. Windows does not have it, so there it
falls back to a single process and says so. Being an ASGI 3 application, featherweb also
runs anywhere else:

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

## Compared with other frameworks

### Design

| | featherweb | FastAPI | Starlette | Litestar | Flask | Falcon |
|---|---|---|---|---|---|---|
| Runtime dependencies | **0** | 10 | 4 | 26 | 7 | 0 |
| Interface | ASGI | ASGI | ASGI | ASGI | WSGI | ASGI + WSGI |
| Input from type hints | yes | yes | no | yes | no | no |
| Response shaped by the return type | yes | yes | no | yes | no | no |
| Class-based controllers | yes | no | endpoints | yes | views | resources |
| Dependency injection | no, by design | yes | no | yes | no | no |
| Automatic OpenAPI | not yet | yes | no | yes | no | no |
| Production server included | yes | no | no | no | no | no |
| Auth (sessions, JWT, roles) included | yes | helpers | middleware | yes | sessions | no |

In short:

- **Choose FastAPI or Litestar** when you want OpenAPI documentation generated for you,
  dependency injection, Pydantic models and a large ecosystem of plugins and answers online.
- **Choose Starlette** when you want a minimal toolkit and prefer to parse requests yourself.
- **Choose Django** when you want an ORM, an admin and migrations out of the box.
- **Choose featherweb** when you want typed input and output with controllers, an included
  server and auth, and no dependency tree to maintain.

### Numbers

Measured with `python benchmarks/compare.py --all` on one Windows desktop, CPython 3.12.
They show the order of magnitude, not a ranking. Run the script on your own hardware
before relying on them.

**Import cost** is everything `import <framework>` loads beyond what a bare interpreter
already has. You pay it on every process start: CLI tools, serverless cold starts and test
runs.

| Framework | Import time | Modules loaded | Dependencies |
|---|---:|---:|---:|
| **featherweb** | **21 ms** | **29** | **0** |
| Starlette | 168 ms | 178 | 4 |
| Falcon | 204 ms | 229 | 0 |
| Flask | 271 ms | 264 | 7 |
| BlackSheep | 332 ms | 303 | 15 |
| Litestar | 435 ms | 430 | 26 |
| Quart | 449 ms | 437 | 20 |
| FastAPI | 468 ms | 318 | 10 |

**Throughput** is requests per second with every framework behind the same uvicorn, so the
difference is the framework and not the server. The load generator is written in Python and
runs on the same machine, which puts a ceiling on all of them. Absolute numbers on a Linux
server with `wrk` will be much higher, and differences of about 10% are within the noise
here.

| Stack | `/plaintext` | `/json` | `/user/7` |
|---|---:|---:|---:|
| featherweb + uvicorn | 8,358 | 7,339 | 7,034 |
| Starlette + uvicorn | 7,812 | 6,786 | 7,007 |
| featherweb, own server | 7,193 | 6,454 | 6,291 |
| FastAPI + uvicorn | 5,218 | 5,091 | 4,430 |

featherweb runs in the same range as Starlette, the thinnest layer of the group, even though
it validates input and shapes output from the types the way FastAPI does.

## What it is not

No ORM, no dependency injection container, no database integration, no OAuth, no HTTP/2 or
HTTP/3, and no automatic OpenAPI yet. The type hints that would generate OpenAPI are in
place, but generating it is not. The others are deliberate omissions, not a roadmap gap.
Use the database library you like. featherweb does not need to know about it.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync                     # create .venv (Python 3.12) with the dev dependencies
uv run pytest               # across TestClient, the bundled server and uvicorn
uv run ruff check           # lint
uv run ruff format          # formatting
uv run pyright              # type checking, strict
python benchmarks/measure.py --all   # the targets in PLAN.md section 2
uv sync --group bench && python benchmarks/compare.py --all   # against other frameworks
python examples/demo/app.py          # a page that pokes every feature above
```

The API reference is [docs/api.md](docs/api.md). The roadmap, the architecture and the
per-component security requirements are in [PLAN.md](PLAN.md), which is written in
Portuguese, and [RELEASING.md](RELEASING.md) covers cutting a version.

## Status

Version 0.1: everything above works and is tested, but the public API may still move
before 1.0. The two areas carrying the most risk, the HTTP/1.1 parser and the
authentication layer, are built to the requirements in PLAN.md section 6, with the
request-smuggling and token-forgery cases covered by tests.

## License

MIT — see [LICENSE](LICENSE).
