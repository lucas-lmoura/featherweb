# API reference

Everything importable from `featherweb`, plus the three modules that are loaded on
demand. The [README](../README.md) is the tour; this is the list.

Names in **bold** are imported lazily: they live in a module that `import featherweb`
does not load, and are fetched the first time you touch them.

---

## Application

### `App`

```python
App(
    *,
    controllers: Iterable[Any] = (),
    middlewares: Iterable[Any] = (),
    on_startup: Iterable[Callable] = (),
    on_shutdown: Iterable[Callable] = (),
    debug: bool = False,
    max_body_size: int = 1_048_576,
    auth: Any = None,
)
```

An ASGI 3 application. Classes passed in are instantiated once, without arguments;
instances are taken as they are, which is how a configured middleware gets in.

| Method | What it does |
|---|---|
| `register(*targets)` | Add controllers, middlewares or `@ControllerAdvice` classes after construction. |
| `scan(package)` | Import a package recursively and register everything marked in it. |
| `mount(prefix, application)` | Hand every path under `prefix` to `application(request, rest) -> Response`. |
| `on_startup(hook)` / `on_shutdown(hook)` | Decorators registering lifespan hooks; sync or async. |
| `run(**kwargs)` | Serve on the bundled server; arguments go to `Server`. |

`debug=True` puts the traceback of an unhandled exception in the response body instead of
only the log. Leave it off in production.

---

## Controllers and routing

| Name | Use |
|---|---|
| `Route` | `@Route("/prefix")` or bare `@Route` on a controller class. |
| `Get` `Post` `Put` `Patch` `Delete` `Options` `Head` | `@Get`, `@Get("/{id:int}")` or `@Get("/x", status=201)`. |
| `Ws` | `@Ws("/echo")`; the handler owns the connection and declares no status. |

Path converters: `{name}` (a segment), `{name:int}`, `{name:float}`, `{name:uuid}` and
`{name:path}`, which is greedy and swallows the rest. A converter that does not match is
simply not that route, so it ends as a 404 rather than a 422.

`/a` and `/a/` are the same route, and a `GET` route answers `HEAD` for free.

---

## Request

### `Request`

| Member | Type | Notes |
|---|---|---|
| `method` `path` `scheme` `http_version` | `str` | From the ASGI scope. |
| `client` | `tuple[str, int] \| None` | Peer address, when the server reports one. |
| `url` | `str` | Rebuilt from the scope and the `Host` header. |
| `headers` | `Headers` | Case-insensitive, keeps repeats (`getlist`). |
| `query` | `QueryParams` | Parsed on first access. |
| `cookies` | `Mapping[str, str]` | Parsed on first access. |
| `path_params` | `Mapping[str, Any]` | Already converted. |
| `identity` | `Identity \| None` | Filled in when the route needs authentication. |
| `session` | `Session \| None` | Only with a session-based strategy. |
| `await body()` | `bytes` | Whole body, read once; over `max_body_size` is a 413. |
| `await json()` | `Any` | Parsed once; malformed input is a 400. |
| `await form()` | `FormData` | Url-encoded or multipart, parsed once. |
| `async for chunk in stream()` | `bytes` | The body as it arrives, without buffering. |

`Connection` is the base `Request` and `WebSocket` share: path, headers, query, cookies.

---

## Response

### `Response[BodyT]`

```python
Response(content, *, status=None, headers=None, media_type=None)
```

`bytes` and `str` go out as they are; anything else is JSON. Leaving `status` unset lets
the verb's declared status apply — `set_status(code)` chooses one that the verb will not
override.

`set_cookie(name, value, *, max_age, expires, path, domain, secure, httponly, samesite)`
and `delete_cookie(name, *, path, domain)`. `samesite="none"` requires `secure=True`.

### `StreamingResponse`

```python
StreamingResponse(content, *, status=None, headers=None, media_type=None)
```

`content` is a sync or async iterable of `bytes` or `str`. Without a `Content-Length` the
server frames it as chunked. `GZip` leaves these alone rather than draining them.

### `FileResponse`

```python
FileResponse(
    path, *, request=None, status=None, headers=None, media_type=None,
    filename=None, disposition="attachment", chunk_size=None,
)
```

Always sets `ETag`, `Last-Modified` and `Accept-Ranges`. Pass `request` and it also honours
`If-None-Match`, `If-Modified-Since`, `Range` and `If-Range` — 304, 206 or 416. A missing
file or a directory raises `HTTPError(404)`. Read in 64 KB blocks in a thread, so a large
file does not stall the loop.

### `RedirectResponse`

```python
RedirectResponse(url, *, status=307, headers=None)
```

---

## Parameters

Inference order, applied per parameter:

1. a name that appears in the route path → that path parameter;
2. a framework type (`Request`, `WebSocket`, `Identity`, `Session`) → the object itself;
3. a dataclass, `TypedDict` or pydantic-style model → the JSON body;
4. `UploadFile`, or `list[UploadFile]` → an uploaded file;
5. anything else simple (`str`, `int`, `float`, `bool`, `Enum`, `Literal`, `X | None`,
   `list[X]`, `UUID`, `datetime`, `date`, `time`, `Decimal`) → the query string;
6. `Annotated[T, marker]` overrides all of it.

| Marker | Reads from | Alias |
|---|---|---|
| `Query("name")` | query string | the name on the wire |
| `Header("x-name")` | request header | underscores become dashes |
| `Cookie("name")` | cookie | |
| `Form("name")` | url-encoded or multipart field | |
| `File("name")` | multipart file | annotation must be `UploadFile` |
| `Body()` | the whole JSON body | `Body("field")` picks one field out of it |

A mutable default (`tags: list[str] = []`) is copied per request, so one request cannot
leak into the next. Anything the framework cannot provide is an error at registration.

### Validation failures

`ValidationError` is an `HTTPError` with status 422, so an `@ExceptionHandler` can catch
it. The body lists every problem at once:

```json
{"detail": [{"location": "query", "field": "page", "message": "expected an integer"}]}
```

`FieldError(location, field, message)` is one entry; `location` is one of `path`, `query`,
`header`, `cookie`, `body`, `form` or `file`.

---

## Errors

| Name | Use |
|---|---|
| `HTTPError(status, detail, *, headers)` | Raise it, or subclass with `status`/`detail` set. |
| `@ExceptionHandler(*types)` | On a controller method (local) or a `@ControllerAdvice` class (global). |
| `@ControllerAdvice` | Marks a class of global handlers. |

Resolution is local handler → global handler → default, and within each level the most
specific exception class in the MRO wins. A handler receives the exception, may declare
`Request`, and returns an annotated type or a `Response[T]` like any other.

---

## Middleware

```python
@Middleware(order=10)
class Timing:
    async def __call__(self, request: Request, call_next: Next) -> Response[Any]: ...
```

The lowest `order` is the outermost layer. Turning an exception into a response happens
inside the chain, so a middleware sees 404s and 500s too.

Ready to use: `CORS(allow_origins=..., allow_methods=..., allow_headers=...,
allow_credentials=..., expose_headers=..., max_age=...)` and
`GZip(minimum_size=500, level=6)`.

---

## WebSocket

### **`WebSocket`**

| Method | Notes |
|---|---|
| `await accept(*, subprotocol=None, headers=())` | Completes the handshake. |
| `await deny(reason="")` | Refuses it; the client sees an HTTP error. |
| `await receive_text()` / `receive_bytes()` / `receive_json()` | Wrong payload type closes with 1003; bad JSON with 1007. |
| `await send_text(s)` / `send_bytes(b)` / `send_json(x)` | Waits if the peer is not reading. |
| `async for x in iter_text()` / `iter_bytes()` / `iter_json()` | Ends when the peer goes away. |
| `await close(code=1000, reason="")` | Closing twice is a no-op. |
| `subprotocols` `accepted` `closed` | Plus everything on `Connection`. |

**`WebSocketDisconnect`** carries `code` and `reason`, and is raised by every receive once
the connection is gone. Sending before `accept()` raises `WebSocketStateError`.

---

## Static files and uploads

### **`StaticFiles`** — `featherweb.staticfiles`

```python
app.mount("/static", StaticFiles(directory, *, index=None, max_age=None))
```

Only `GET` and `HEAD`. Traversal is refused twice over: the segments are checked before
anything touches the filesystem, and the resolved path must still be inside the directory
afterwards — which also catches a symlink pointing out of it.

### **`UploadFile`** — `featherweb.multipart`

`filename`, `content_type`, `headers`, `size`, `spooled_to_disk`, and
`await read(size=-1)`, `await seek(offset)`, `await close()`. Spools to disk past 1 MB.
`MultipartLimits(max_parts, max_field_size, max_file_size, spool_max_size)` caps a body;
the framework closes the files when the response goes out.

---

## Authentication — `featherweb.auth`

### **`SessionAuth`**

```python
SessionAuth(
    secret, *, cookie_name="session", max_age=1_209_600, secure=True, httponly=True,
    samesite="lax", path="/", domain=None, user_key="user", roles_key="roles",
)
```

`secret` may be a list, newest first, to rotate the key without logging everyone out.
`login(session, user, roles=())`, `logout(session)`. A cookie that does not verify is
treated as absent, not as an error.

### **`JWTAuth`**

```python
JWTAuth(
    key, *, algorithms=("HS256",), signing_key=None, issuer=None, audience=None,
    leeway=0.0, subject_key="sub", roles_key="roles", header="authorization",
)
```

`issue(subject, *, roles=(), expires_in=3600, **claims)`. `algorithms` is an allow-list and
the only thing that decides how a signature is checked; `alg: none` is refused whatever you
pass. RS\*/ES\* need the `crypto` extra, with the public key as `key` and the private one
as `signing_key`.

`featherweb.auth.jwt` also exposes `encode(payload, key, *, algorithm, headers)` and
`decode(token, key, *, algorithms, audience, issuer, leeway, verify_exp)` on their own.

### Guards

`@Authenticated` and `@Roles(*roles)` go on a controller class or one of its methods. Both
levels apply — a class requiring `staff` and a method requiring `admin` needs both — while
several roles in one decorator are alternatives. No identity is 401; a missing role is 403.

**`Identity`** has `id`, `roles` and `claims`, plus `has_role(*roles)`.
**`Session`** is a `MutableMapping` that tracks whether it changed.

### Signed values — `featherweb.auth.signing`

`Signer(secrets, *, purpose="")` with `sign(value)` and `unsign(signed, *, max_age=None)`,
for cookies outside authentication. Comparison is constant-time, the first secret signs and
all of them verify, and the key is derived per purpose so a value signed for one thing does
not verify as another. Raises `BadSignature`, or `SignatureExpired` past `max_age`.

---

## Testing

```python
async with TestClient(app, *, base_url=..., root_path="", headers=...) as client:
    response = await client.get("/path", params=..., headers=..., cookies=...)
```

`get` `post` `put` `patch` `delete` `options` `head`, and `request(method, path, ...)` with
`content=`, `json=`, `params=`, `headers=` and `cookies=`. The response has `status`,
`headers`, `body`, `text`, `json()` and `cookies`. Calls the app directly — no socket, no
server — and runs the lifespan.

---

## Running

### `featherweb.server.runner`

```python
run(app, **kwargs)        # builds a Server and serves until shutdown
Server(
    app, *, host="127.0.0.1", port=8000, config=None, ssl_context=None, backlog=2048,
    reuse_port=False, shutdown_timeout=10.0, lifespan=True, lifespan_timeout=10.0,
)                         # startup(), serve(), shutdown(), bound_port, url, sockets
```

One server is one process. Several are `Supervisor`'s job, below, which is what
`--workers` uses — `run()` and `Server` know nothing about it.

`ServerConfig` holds the limits and timeouts: request line 8 KB, headers 64 KB across 100
fields, `header_timeout=10.0` against slowloris, `keep_alive_timeout=5.0`, the body
high/low water marks for backpressure, and `max_message_size` for WebSocket.

### `featherweb.server.workers`

`Supervisor(target, *, workers, **options)` runs one process per worker, each binding the
same port with `SO_REUSEPORT`; `supports_reuse_port()` says whether the platform can.
Windows cannot, and falls back to a single worker.

### Command line

```sh
featherweb run module:attribute [--host] [--port] [--workers] \
    [--ssl-certfile] [--ssl-keyfile] [--ssl-password] [--log-level]
```

Also `python -m featherweb`. Without a colon the attribute defaults to `app`.
