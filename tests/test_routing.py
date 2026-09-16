"""Unit tests for the route table."""

from __future__ import annotations

import pytest

from featherweb.routing import (
    RouteConflictError,
    Router,
    normalize_path,
    path_param_names,
)


def build(*routes: tuple[str, str]) -> Router[str]:
    router: Router[str] = Router()
    for method, path in routes:
        router.add(method, path, f"{method} {path}")
    return router


def resolve(router: Router[str], method: str, path: str) -> tuple[str | None, dict[str, object]]:
    found = router.resolve(path)
    if found is None:
        return None, {}
    handlers, params = found
    return handlers.get(method), params


@pytest.mark.parametrize(
    ("given", "expected"),
    [("", "/"), ("/", "/"), ("users", "/users"), ("/users/", "/users"), ("/a/b/", "/a/b")],
)
def test_normalize_path(given: str, expected: str) -> None:
    assert normalize_path(given) == expected


def test_static_routes() -> None:
    router = build(("GET", "/"), ("GET", "/users"), ("POST", "/users"))
    assert resolve(router, "GET", "/") == ("GET /", {})
    assert resolve(router, "POST", "/users") == ("POST /users", {})
    assert resolve(router, "GET", "/missing") == (None, {})


def test_trailing_slash_is_ignored() -> None:
    router = build(("GET", "/users"))
    assert resolve(router, "GET", "/users/")[0] == "GET /users"


def test_path_parameters_are_converted() -> None:
    router = build(("GET", "/users/{id:int}"), ("GET", "/files/{size:float}"))
    assert resolve(router, "GET", "/users/42") == ("GET /users/{id:int}", {"id": 42})
    assert resolve(router, "GET", "/files/1.5") == ("GET /files/{size:float}", {"size": 1.5})


def test_string_is_the_default_converter() -> None:
    router = build(("GET", "/users/{name}"))
    assert resolve(router, "GET", "/users/ada") == ("GET /users/{name}", {"name": "ada"})


def test_a_failed_conversion_is_not_a_match() -> None:
    router = build(("GET", "/users/{id:int}"))
    assert resolve(router, "GET", "/users/ada") == (None, {})


def test_literal_segments_win_over_parameters() -> None:
    router = build(("GET", "/users/{id:int}"), ("GET", "/users/me"))
    assert resolve(router, "GET", "/users/me")[0] == "GET /users/me"
    assert resolve(router, "GET", "/users/7")[0] == "GET /users/{id:int}"


def test_backtracking_between_converters() -> None:
    router = build(("GET", "/x/{id:int}/count"), ("GET", "/x/{name}/detail"))
    assert resolve(router, "GET", "/x/12/count")[0] == "GET /x/{id:int}/count"
    assert resolve(router, "GET", "/x/ada/detail")[0] == "GET /x/{name}/detail"
    assert resolve(router, "GET", "/x/12/detail")[0] == "GET /x/{name}/detail"


def test_uuid_converter() -> None:
    from uuid import UUID

    router = build(("GET", "/items/{id:uuid}"))
    value = "12345678-1234-5678-1234-567812345678"
    handler, params = resolve(router, "GET", f"/items/{value}")
    assert handler == "GET /items/{id:uuid}"
    assert params == {"id": UUID(value)}
    assert resolve(router, "GET", "/items/nope") == (None, {})


def test_path_converter_swallows_the_rest() -> None:
    router = build(("GET", "/static/{file:path}"))
    assert resolve(router, "GET", "/static/css/site.css") == (
        "GET /static/{file:path}",
        {"file": "css/site.css"},
    )


def test_path_converter_must_come_last() -> None:
    router: Router[str] = Router()
    with pytest.raises(ValueError, match="last segment"):
        router.add("GET", "/static/{file:path}/x", "x")


def test_several_parameters() -> None:
    router = build(("GET", "/{org}/{repo}/issues/{number:int}"))
    handler, params = resolve(router, "GET", "/acme/web/issues/12")
    assert handler is not None
    assert params == {"org": "acme", "repo": "web", "number": 12}


def test_methods_on_the_same_path() -> None:
    router = build(("GET", "/users/{id:int}"), ("DELETE", "/users/{id:int}"))
    found = router.resolve("/users/3")
    assert found is not None
    handlers, params = found
    assert sorted(handlers) == ["DELETE", "GET"]
    assert params == {"id": 3}


def test_duplicate_route_is_refused() -> None:
    router = build(("GET", "/users"))
    with pytest.raises(RouteConflictError, match="already registered"):
        router.add("GET", "/users/", "other")


def test_duplicate_dynamic_route_is_refused() -> None:
    router = build(("GET", "/users/{id:int}"))
    with pytest.raises(RouteConflictError):
        router.add("GET", "/users/{id:int}", "other")


def test_unknown_converter() -> None:
    router: Router[str] = Router()
    with pytest.raises(ValueError, match="unknown converter"):
        router.add("GET", "/users/{id:number}", "x")


def test_malformed_parameter() -> None:
    router: Router[str] = Router()
    with pytest.raises(ValueError, match="malformed path parameter"):
        router.add("GET", "/users/{id", "x")


def test_path_param_names() -> None:
    assert path_param_names("/users/{id:int}/posts/{slug}") == ("id", "slug")
    assert path_param_names("/users") == ()
