"""Registration, scanning and the rules the application enforces at startup."""

from __future__ import annotations

from typing import Any

import pytest

from featherweb import (
    App,
    ControllerAdvice,
    ExceptionHandler,
    Get,
    HTTPError,
    Middleware,
    Next,
    Post,
    Request,
    Response,
    Route,
)
from featherweb.params import RouteConfigurationError
from featherweb.routing import RouteConflictError
from featherweb.testing import TestClient


@Route("/things")
class ThingController:
    @Get
    async def index(self) -> list[str]:
        return ["thing"]


# -- scanning ---------------------------------------------------------------


async def test_scan_registers_controllers_middlewares_and_advice() -> None:
    app = App()
    app.scan("tests.sample_app")
    async with TestClient(app) as client:
        listed = await client.get("/scanned/users")
        assert listed.status == 200
        assert listed.json() == ["ada"]
        assert listed.headers["x-stamp"] == "scanned"

        failed = await client.get("/scanned/boom")
        assert failed.status == 503
        assert failed.json() == {"detail": "scanned: gone"}


async def test_scan_does_not_register_reexported_classes_twice() -> None:
    app = App()
    app.scan("tests.sample_app")  # reexport.py imports the same controller


def test_scan_reports_the_module_that_failed_to_import() -> None:
    app = App()
    with pytest.raises(ImportError, match=r"cannot import 'tests\.nope'"):
        app.scan("tests.nope")


# -- registration rules -----------------------------------------------------


def test_duplicate_route_is_refused_at_startup() -> None:
    @Route("/dup")
    class First:
        @Get
        async def index(self) -> str:
            return "a"

    @Route("/dup")
    class Second:
        @Get
        async def index(self) -> str:
            return "b"

    with pytest.raises(RouteConflictError, match="GET /dup"):
        App(controllers=[First, Second])


def test_unmarked_class_is_refused() -> None:
    class Plain:
        pass

    with pytest.raises(TypeError, match="not registrable"):
        App(controllers=[Plain])


def test_a_type_the_framework_cannot_read_is_refused() -> None:
    class Database:
        """Not a model, not a framework type: there is nowhere to read it from."""

    @Route("/bad")
    class BadController:
        @Get
        async def index(self, db: Database) -> str:
            return str(db)

    with pytest.raises(RouteConfigurationError, match="cannot read Database from a request"):
        App(controllers=[BadController])


def test_a_parameter_without_an_annotation_is_refused() -> None:
    @Route("/bad")
    class BadController:
        @Get
        async def index(self, page) -> str:  # type: ignore[no-untyped-def]
            return str(page)  # type: ignore[no-any-expr]

    with pytest.raises(RouteConfigurationError, match="no annotation"):
        App(controllers=[BadController])


def test_two_global_handlers_for_the_same_exception_are_refused() -> None:
    @ControllerAdvice
    class Errors:
        @ExceptionHandler(ValueError)
        async def one(self, exc: ValueError) -> str:
            return "one"

        @ExceptionHandler(ValueError)
        async def two(self, exc: ValueError) -> str:
            return "two"

    with pytest.raises(TypeError, match="already has a handler"):
        App(controllers=[Errors])


def test_exception_handler_needs_an_exception_type() -> None:
    with pytest.raises(TypeError, match="not an exception type"):
        ExceptionHandler(str)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="at least one"):
        ExceptionHandler()


def test_route_decorator_rejects_functions() -> None:
    with pytest.raises(TypeError, match="only be used on a class"):

        @Route("/x")  # type: ignore[arg-type]
        def not_a_class() -> None: ...


def test_middleware_decorator_rejects_functions() -> None:
    with pytest.raises(TypeError, match="only be used on a class"):

        @Middleware(order=1)  # type: ignore[arg-type]
        def not_a_class() -> None: ...


def test_instances_are_accepted_as_well_as_classes() -> None:
    controller = ThingController()
    app = App(controllers=[controller])
    assert isinstance(app, App)


def test_register_after_construction() -> None:
    app = App()
    app.register(ThingController)
    assert isinstance(app, App)


# -- decorator shapes -------------------------------------------------------


async def test_verbs_work_with_and_without_parentheses() -> None:
    @Route("/shapes")
    class ShapeController:
        @Get
        async def bare(self) -> str:
            return "bare"

        @Get("/called")
        async def called(self) -> str:
            return "called"

        @Post("/status", status=202)
        async def with_status(self) -> str:
            return "accepted"

    async with TestClient(App(controllers=[ShapeController])) as client:
        assert (await client.get("/shapes")).text == "bare"
        assert (await client.get("/shapes/called")).text == "called"
        assert (await client.post("/shapes/status")).status == 202


async def test_one_method_can_answer_several_verbs() -> None:
    @Route("/multi")
    class MultiController:
        @Get
        @Post
        async def both(self, request: Request) -> str:
            return request.method

    async with TestClient(App(controllers=[MultiController])) as client:
        assert (await client.get("/multi")).text == "GET"
        assert (await client.post("/multi")).text == "POST"


async def test_bare_route_uses_the_root() -> None:
    @Route
    class BareController:
        @Get("/bare")
        async def index(self) -> str:
            return "root"

    async with TestClient(App(controllers=[BareController])) as client:
        assert (await client.get("/bare")).text == "root"


async def test_controller_advice_can_be_called() -> None:
    @ControllerAdvice()
    class Errors:
        @ExceptionHandler(KeyError)
        async def missing(self, exc: KeyError) -> Response[dict[str, str]]:
            return Response({"detail": "missing"}, status=404)

    @Route("/keys")
    class KeyController:
        @Get
        async def index(self) -> str:
            raise KeyError("nope")

    async with TestClient(App(controllers=[KeyController, Errors])) as client:
        assert (await client.get("/keys")).status == 404


# -- exception handling details ---------------------------------------------


async def test_the_closest_exception_class_wins() -> None:
    class Base(Exception):
        pass

    class Specific(Base):
        pass

    @ControllerAdvice
    class Errors:
        @ExceptionHandler(Base)
        async def base(self, exc: Base) -> Response[dict[str, str]]:
            return Response({"detail": "base"}, status=500)

        @ExceptionHandler(Specific)
        async def specific(self, exc: Specific) -> Response[dict[str, str]]:
            return Response({"detail": "specific"}, status=418)

    @Route("/raise")
    class RaiseController:
        @Get("/base")
        async def base(self) -> str:
            raise Base

        @Get("/specific")
        async def specific(self) -> str:
            raise Specific

    async with TestClient(App(controllers=[RaiseController, Errors])) as client:
        assert (await client.get("/raise/base")).json() == {"detail": "base"}
        assert (await client.get("/raise/specific")).json() == {"detail": "specific"}


async def test_a_handler_may_ask_for_the_request() -> None:
    @ControllerAdvice
    class Errors:
        @ExceptionHandler(ValueError)
        async def bad(self, exc: ValueError, request: Request) -> Response[dict[str, str]]:
            return Response({"path": request.path, "detail": str(exc)}, status=400)

    @Route("/v")
    class ValueController:
        @Get
        async def index(self) -> str:
            raise ValueError("bad input")

    async with TestClient(App(controllers=[ValueController, Errors])) as client:
        assert (await client.get("/v")).json() == {"path": "/v", "detail": "bad input"}


async def test_a_failing_handler_falls_back_to_the_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    @ControllerAdvice
    class Errors:
        @ExceptionHandler(ValueError)
        async def broken(self, exc: ValueError) -> str:
            raise RuntimeError("the handler itself is broken")

    @Route("/v")
    class ValueController:
        @Get
        async def index(self) -> str:
            raise ValueError("bad input")

    async with TestClient(App(controllers=[ValueController, Errors])) as client:
        response = await client.get("/v")
    assert response.status == 500
    assert "broken" in caplog.text


async def test_an_error_raised_by_a_middleware_is_still_rendered() -> None:
    @Middleware
    class Rejecting:
        async def __call__(self, request: Request, call_next: Next) -> Response[Any]:
            raise HTTPError(429, "slow down")

    async with TestClient(App(controllers=[ThingController], middlewares=[Rejecting])) as client:
        response = await client.get("/things")
    assert response.status == 429
    assert response.json() == {"detail": "slow down"}


async def test_http_error_headers_reach_the_response() -> None:
    @Route("/gate")
    class GateController:
        @Get
        async def index(self) -> str:
            raise HTTPError(401, "who are you", headers={"www-authenticate": "Bearer"})

    async with TestClient(App(controllers=[GateController])) as client:
        response = await client.get("/gate")
    assert response.status == 401
    assert response.headers["www-authenticate"] == "Bearer"


# -- ASGI edges -------------------------------------------------------------


async def test_websocket_scope_is_closed_politely() -> None:
    app = App(controllers=[ThingController])
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: Any) -> None:
        sent.append(dict(message))

    await app({"type": "websocket", "path": "/things"}, receive, send)
    assert sent == [{"type": "websocket.close", "code": 1001}]


async def test_unknown_scope_type_is_refused() -> None:
    app = App(controllers=[ThingController])

    async def receive() -> dict[str, Any]:
        return {}

    async def send(message: Any) -> None:
        del message

    with pytest.raises(RuntimeError, match="unsupported ASGI scope"):
        await app({"type": "carrier-pigeon"}, receive, send)


async def test_root_path_is_stripped_before_routing() -> None:
    app = App(controllers=[ThingController])
    async with TestClient(app, root_path="/api") as client:
        assert (await client.get("/things")).json() == ["thing"]


async def test_failing_startup_hook_stops_the_lifespan() -> None:
    def boom() -> None:
        raise RuntimeError("no database")

    app = App(controllers=[ThingController], on_startup=[boom])
    with pytest.raises(RuntimeError, match="lifespan failed"):
        async with TestClient(app):
            pass
