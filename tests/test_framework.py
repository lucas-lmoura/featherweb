"""The framework suite.

Every test here runs three times: in process through ``TestClient``, over a
socket against featherweb's own server, and over a socket against uvicorn. What
the framework promises must hold in all three.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Any
from uuid import UUID

import pytest

from featherweb import (
    CORS,
    App,
    ControllerAdvice,
    Cookie,
    Delete,
    ExceptionHandler,
    Get,
    GZip,
    Header,
    HTTPError,
    Middleware,
    Next,
    Patch,
    Post,
    Put,
    Query,
    RedirectResponse,
    Request,
    Response,
    Route,
)

from .conftest import Serve


class UserNotFound(HTTPError):
    status = 404
    detail = "user not found"


class Unpayable(Exception):
    """An exception with no HTTP meaning of its own."""


@Route
class RootController:
    @Get
    async def index(self) -> str:
        return "hello"

    @Get("/bytes")
    async def raw(self) -> bytes:
        return b"\x00\x01\x02"

    @Get("/nothing")
    async def nothing(self) -> None:
        return None

    @Get("/redirect")
    async def redirect(self) -> RedirectResponse:
        return RedirectResponse("/", status=302)

    @Get("/boom")
    async def boom(self) -> str:
        raise RuntimeError("something went wrong")

    @Get("/forbidden")
    async def forbidden(self) -> str:
        raise PermissionError("not for you")

    @Get("/teapot")
    async def teapot(self) -> str:
        raise HTTPError(418, "I am a teapot")


@Route("/users")
class UserController:
    @Get
    async def index(self, request: Request) -> list[dict[str, Any]]:
        page = request.query.get("page", "1")
        return [{"id": 1, "page": page}]

    @Get("/{id:int}")
    async def show(self, id: int) -> dict[str, Any]:
        if id != 1:
            raise UserNotFound
        return {"id": id, "name": "ada"}

    @Post(status=201)
    async def create(self, request: Request) -> Response[dict[str, Any]]:
        payload = await request.json()
        response = Response({"id": 2, **payload}, status=201)
        response.set_cookie("sid", "s3cret", httponly=True)
        response.headers["x-created"] = "yes"
        return response

    @Put("/{id:int}")
    async def replace(self, id: int, request: Request) -> dict[str, Any]:
        return {"id": id, **await request.json()}

    @Patch("/{id:int}")
    async def update(self, id: int) -> dict[str, Any]:
        return {"id": id, "patched": True}

    @Delete("/{id:int}")
    async def destroy(self, id: int) -> None:
        return None

    @Post("/form")
    async def from_form(self, request: Request) -> dict[str, Any]:
        form = await request.form()
        return {"name": form.get("name"), "tags": form.getlist("tag")}

    @Get("/{id:int}/echo")
    async def echo(self, id: int, request: Request) -> dict[str, Any]:
        return {
            "id": id,
            "method": request.method,
            "path": request.path,
            "agent": request.headers.get("x-agent"),
            "session": request.cookies.get("session"),
            "params": dict(request.path_params),
        }

    @ExceptionHandler(Unpayable)
    async def unpayable(self, exc: Unpayable) -> Response[dict[str, str]]:
        return Response({"detail": f"local: {exc}"}, status=402)

    @Get("/unpayable")
    async def raise_unpayable(self) -> str:
        raise Unpayable("no money")


class Colour(Enum):
    RED = "red"
    BLUE = "blue"


@dataclass
class Address:
    city: str
    country: str = "pt"


@dataclass
class UserIn:
    name: str
    email: str
    colour: Colour = Colour.RED
    address: Address | None = None
    tags: list[str] = field(default_factory=list[str])


@dataclass
class UserOut:
    id: int
    name: str
    colour: Colour
    address: Address | None = None


class StoredUser:
    """What a repository might hand back: more than the response declares."""

    def __init__(self, id: int, name: str, colour: Colour) -> None:
        self.id = id
        self.name = name
        self.colour = colour
        self.address = Address("lisbon")
        self.password = "do not send me"


@Route("/typed")
class TypedController:
    @Get
    async def index(self, page: int = 1, tag: list[str] = []) -> list[UserOut]:  # noqa: B006
        return [StoredUser(page, ",".join(tag) or "ada", Colour.BLUE)]  # type: ignore[list-item]

    @Get("/{id:int}")
    async def show(self, id: int) -> UserOut:
        return StoredUser(id, "ada", Colour.RED)  # type: ignore[return-value]

    @Post(status=201)
    async def create(self, data: UserIn) -> Response[UserOut]:
        response = Response(UserOut(1, data.name, data.colour, data.address))
        response.headers["x-created"] = data.email
        return response

    @Get("/headers")
    async def from_headers(
        self,
        agent: Annotated[str, Header("x-agent")] = "none",
        session: Annotated[str | None, Cookie()] = None,
        size: Annotated[int, Query("perPage")] = 10,
    ) -> dict[str, Any]:
        return {"agent": agent, "session": session, "size": size}


@Route("/files")
class FileController:
    @Get("/{path:path}")
    async def show(self, path: str) -> dict[str, str]:
        return {"path": path}

    @Get("/by-id/{id:uuid}")
    async def by_id(self, id: UUID) -> dict[str, str]:
        return {"id": str(id)}


@ControllerAdvice
class GlobalErrors:
    @ExceptionHandler(PermissionError)
    async def forbidden(self, exc: PermissionError) -> Response[dict[str, str]]:
        return Response({"detail": str(exc)}, status=403)

    @ExceptionHandler(Unpayable)
    async def unpayable(self, exc: Unpayable) -> Response[dict[str, str]]:
        return Response({"detail": f"global: {exc}"}, status=402)


@Middleware(order=10)
class Tagging:
    async def __call__(self, request: Request, call_next: Next) -> Response[Any]:
        response = await call_next(request)
        response.headers["x-tag"] = "outer" if "x-tag" not in response.headers else "kept"
        response.headers.append("x-order", "10")
        return response


@Middleware(order=20)
class Inner:
    async def __call__(self, request: Request, call_next: Next) -> Response[Any]:
        response = await call_next(request)
        response.headers.append("x-order", "20")
        return response


def build_app(**kwargs: Any) -> App:
    return App(
        controllers=[
            RootController,
            UserController,
            TypedController,
            FileController,
            GlobalErrors,
        ],
        middlewares=[Tagging, Inner],
        **kwargs,
    )


# -- responses --------------------------------------------------------------


async def test_text_response(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get("/")
    assert response.status == 200
    assert response.text == "hello"
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["content-length"] == "5"


async def test_json_response(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get("/users/1")
    assert response.status == 200
    assert response.json() == {"id": 1, "name": "ada"}
    assert response.headers["content-type"] == "application/json"


async def test_bytes_response(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get("/bytes")
    assert response.body == b"\x00\x01\x02"
    assert response.headers["content-type"] == "application/octet-stream"


async def test_none_becomes_204(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get("/nothing")
    assert response.status == 204
    assert response.body == b""
    assert "content-length" not in response.headers


async def test_status_from_the_decorator(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.post("/users", json={"name": "grace"})
    assert response.status == 201
    assert response.json() == {"id": 2, "name": "grace"}
    assert response.headers["x-created"] == "yes"
    assert response.cookies == {"sid": "s3cret"}


async def test_explicit_none_return_keeps_the_declared_status(serve: Serve) -> None:
    client = await serve(build_app())
    assert (await client.delete("/users/3")).status == 204


async def test_redirect(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get("/redirect")
    assert response.status == 302
    assert response.headers["location"] == "/"


async def test_head_is_answered_by_the_get_route(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.head("/")
    assert response.status == 200
    assert response.headers["content-length"] == "5"


# -- routing ----------------------------------------------------------------


async def test_path_parameters(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get("/users/7/echo", headers={"x-agent": "pytest"})
    payload = response.json()
    assert payload["id"] == 7
    assert payload["params"] == {"id": 7}
    assert payload["method"] == "GET"
    assert payload["agent"] == "pytest"


async def test_path_converter(serve: Serve) -> None:
    client = await serve(build_app())
    assert (await client.get("/files/css/site.css")).json() == {"path": "css/site.css"}


async def test_uuid_converter(serve: Serve) -> None:
    client = await serve(build_app())
    value = "12345678-1234-5678-1234-567812345678"
    assert (await client.get(f"/files/by-id/{value}")).json() == {"id": value}


async def test_query_string(serve: Serve) -> None:
    client = await serve(build_app())
    assert (await client.get("/users", params={"page": 3})).json() == [{"id": 1, "page": "3"}]


async def test_cookies_are_read(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get("/users/1/echo", cookies={"session": "abc"})
    assert response.json()["session"] == "abc"


async def test_unknown_path_is_404(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get("/nope")
    assert response.status == 404
    assert response.json() == {"detail": "not found"}


async def test_wrong_method_is_405_with_allow(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.patch("/users")
    assert response.status == 405
    assert set(response.headers["allow"].split(", ")) == {"GET", "HEAD", "POST"}


async def test_trailing_slash_matches(serve: Serve) -> None:
    client = await serve(build_app())
    assert (await client.get("/users/1/")).status == 200


# -- bodies -----------------------------------------------------------------


async def test_json_body(serve: Serve) -> None:
    client = await serve(build_app())
    assert (await client.put("/users/9", json={"name": "hopper"})).json() == {
        "id": 9,
        "name": "hopper",
    }


async def test_malformed_json_body_is_400(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.put(
        "/users/9", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert response.status == 400
    assert "malformed JSON" in response.json()["detail"]


async def test_urlencoded_form(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.post(
        "/users/form",
        content="name=ada&tag=x&tag=y",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.json() == {"name": "ada", "tags": ["x", "y"]}


async def test_body_over_the_limit_is_413(serve: Serve) -> None:
    client = await serve(build_app(max_body_size=64))
    response = await client.put("/users/1", content=b"x" * 500)
    assert response.status == 413


# -- errors -----------------------------------------------------------------


async def test_http_error_is_rendered_as_detail(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get("/teapot")
    assert response.status == 418
    assert response.json() == {"detail": "I am a teapot"}


async def test_http_error_subclass(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get("/users/2")
    assert response.status == 404
    assert response.json() == {"detail": "user not found"}


async def test_global_exception_handler(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get("/forbidden")
    assert response.status == 403
    assert response.json() == {"detail": "not for you"}


async def test_local_handler_wins_over_the_global_one(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get("/users/unpayable")
    assert response.status == 402
    assert response.json() == {"detail": "local: no money"}


async def test_unhandled_exception_is_a_plain_500(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get("/boom")
    assert response.status == 500
    assert response.json() == {"detail": "internal server error"}


async def test_debug_mode_returns_the_traceback(serve: Serve) -> None:
    client = await serve(build_app(debug=True))
    response = await client.get("/boom")
    assert response.status == 500
    assert "something went wrong" in response.json()["detail"]


# -- middlewares ------------------------------------------------------------


async def test_middlewares_run_in_order(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get("/")
    # The innermost layer touches the response first.
    assert response.headers.getlist("x-order") == ["20", "10"]
    assert response.headers["x-tag"] == "outer"


async def test_middlewares_also_wrap_error_responses(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get("/nope")
    assert response.status == 404
    assert response.headers.getlist("x-order") == ["20", "10"]


async def test_cors_preflight(serve: Serve) -> None:
    app = App(controllers=[RootController], middlewares=[CORS(allow_origins=["https://a.test"])])
    client = await serve(app)
    response = await client.options(
        "/",
        headers={
            "origin": "https://a.test",
            "access-control-request-method": "GET",
            "access-control-request-headers": "x-token",
        },
    )
    assert response.status == 204
    assert response.headers["access-control-allow-origin"] == "https://a.test"
    assert "GET" in response.headers["access-control-allow-methods"]
    assert response.headers["access-control-allow-headers"] == "x-token"
    assert response.headers["vary"] == "Origin"


async def test_cors_on_a_normal_request(serve: Serve) -> None:
    app = App(controllers=[RootController], middlewares=[CORS()])
    client = await serve(app)
    response = await client.get("/", headers={"origin": "https://a.test"})
    assert response.headers["access-control-allow-origin"] == "*"


async def test_cors_ignores_unknown_origins(serve: Serve) -> None:
    app = App(controllers=[RootController], middlewares=[CORS(allow_origins=["https://a.test"])])
    client = await serve(app)
    response = await client.get("/", headers={"origin": "https://evil.test"})
    assert "access-control-allow-origin" not in response.headers


async def test_gzip_compresses_large_responses(serve: Serve) -> None:
    @Route("/big")
    class BigController:
        @Get
        async def big(self) -> str:
            return "hello " * 500

    app = App(controllers=[BigController], middlewares=[GZip(minimum_size=100)])
    client = await serve(app)
    response = await client.get("/big", headers={"accept-encoding": "gzip"})
    assert response.headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in response.headers["vary"]
    assert gzip.decompress(response.body) == b"hello " * 500


async def test_gzip_leaves_small_responses_alone(serve: Serve) -> None:
    app = App(controllers=[RootController], middlewares=[GZip(minimum_size=100)])
    client = await serve(app)
    response = await client.get("/", headers={"accept-encoding": "gzip"})
    assert "content-encoding" not in response.headers
    assert response.text == "hello"


async def test_gzip_needs_the_client_to_ask(serve: Serve) -> None:
    app = App(controllers=[RootController], middlewares=[GZip(minimum_size=1)])
    client = await serve(app)
    response = await client.get("/", headers={"accept-encoding": "identity"})
    assert "content-encoding" not in response.headers


# -- lifespan ---------------------------------------------------------------


async def test_lifespan_hooks_run(serve: Serve) -> None:
    events: list[str] = []

    @Route("/state")
    class StateController:
        @Get
        async def state(self) -> list[str]:
            return list(events)

    app = App(
        controllers=[StateController],
        on_startup=[lambda: events.append("startup")],
        on_shutdown=[lambda: events.append("shutdown")],
    )
    client = await serve(app)
    assert (await client.get("/state")).json() == ["startup"]


async def test_async_lifespan_hooks_run(serve: Serve) -> None:
    ready: list[str] = []

    async def warm_up() -> None:
        ready.append("warm")

    @Route("/ready")
    class ReadyController:
        @Get
        async def ready(self) -> list[str]:
            return list(ready)

    app = App(controllers=[ReadyController], on_startup=[warm_up])
    client = await serve(app)
    assert (await client.get("/ready")).json() == ["warm"]


@pytest.mark.parametrize("path", ["/", "/users/1", "/nope"])
async def test_every_response_is_framed(serve: Serve, path: str) -> None:
    """Whatever the status, the client can tell where the response ends."""
    client = await serve(build_app())
    response = await client.get(path)
    assert response.status in (200, 404)
    assert int(response.headers["content-length"]) == len(response.body)


# -- typed input and output -------------------------------------------------


async def test_query_parameters_are_converted(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get("/typed", params={"page": 3, "tag": ["a", "b"]})
    assert response.status == 200
    assert response.json() == [
        {
            "id": 3,
            "name": "a,b",
            "colour": "blue",
            "address": {"city": "lisbon", "country": "pt"},
        }
    ]


async def test_only_the_declared_fields_are_sent(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get("/typed/7")
    assert response.json() == {
        "id": 7,
        "name": "ada",
        "colour": "red",
        "address": {"city": "lisbon", "country": "pt"},
    }
    assert "password" not in response.text


async def test_a_json_body_becomes_a_dataclass(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.post(
        "/typed",
        json={
            "name": "grace",
            "email": "grace@example.com",
            "colour": "blue",
            "address": {"city": "new york", "country": "us"},
            "extra": "ignored",
        },
    )
    assert response.status == 201
    assert response.headers["x-created"] == "grace@example.com"
    assert response.json() == {
        "id": 1,
        "name": "grace",
        "colour": "blue",
        "address": {"city": "new york", "country": "us"},
    }


async def test_headers_cookies_and_aliases(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get(
        "/typed/headers",
        params={"perPage": 5},
        headers={"x-agent": "pytest"},
        cookies={"session": "s1"},
    )
    assert response.json() == {"agent": "pytest", "session": "s1", "size": 5}


async def test_a_bad_query_value_is_422(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.get("/typed", params={"page": "nope"})
    assert response.status == 422
    assert response.json() == {
        "detail": [{"location": "query", "field": "page", "message": "expected an integer"}]
    }


async def test_a_bad_body_field_is_422(serve: Serve) -> None:
    client = await serve(build_app())
    response = await client.post("/typed", json={"name": "grace", "address": {"city": 1}})
    assert response.status == 422
    reported = {error["field"]: error["message"] for error in response.json()["detail"]}
    assert reported == {"email": "field required", "address.city": "expected a string"}
