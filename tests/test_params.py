"""Where each handler parameter comes from, and what happens when it is wrong.

These cover section 5.3 of the plan: inference from type hints, the explicit
``Annotated`` markers, and the 422 that says which field was wrong and why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Any, Literal, TypedDict, cast
from uuid import UUID

import pytest

from featherweb import (
    App,
    Body,
    Cookie,
    Form,
    Get,
    Header,
    Post,
    Query,
    Request,
    Route,
)
from featherweb.params import RouteConfigurationError
from featherweb.testing import TestClient, TestResponse


class Colour(Enum):
    RED = "red"
    BLUE = "blue"


@dataclass
class Address:
    city: str
    zip_code: str = "00000"


@dataclass
class UserIn:
    name: str
    email: str
    age: int | None = None
    colour: Colour = Colour.RED
    address: Address | None = None
    tags: list[str] = field(default_factory=list[str])


class PointIn(TypedDict):
    x: int
    y: int


class FakeModel:
    """A pydantic-shaped model, recognised by duck typing alone."""

    def __init__(self, name: str) -> None:
        self.name = name

    @classmethod
    def model_validate(cls, payload: Any) -> FakeModel:
        if not isinstance(payload, dict):
            raise ValueError("name is required")
        fields = cast(dict[str, Any], payload)
        if "name" not in fields:
            raise ValueError("name is required")
        return cls(str(fields["name"]))

    def model_dump(self, mode: str = "python") -> dict[str, Any]:
        del mode
        return {"name": self.name}


async def call(controller: type, method: str, path: str, **kwargs: Any) -> TestResponse:
    async with TestClient(App(controllers=[controller])) as client:
        return await client.request(method, path, **kwargs)


# -- query string -----------------------------------------------------------


@Route("/q")
class QueryController:
    @Get
    async def simple(self, page: int = 1, size: int = 20) -> dict[str, int]:
        return {"page": page, "size": size}

    @Get("/types")
    async def types(
        self,
        flag: bool,
        ratio: float,
        colour: Colour,
        mode: Literal["fast", "slow"],
        token: UUID,
    ) -> dict[str, str]:
        return {
            "flag": str(flag),
            "ratio": str(ratio),
            "colour": colour.value,
            "mode": mode,
            "token": str(token),
        }

    @Get("/optional")
    async def optional(self, name: str | None) -> dict[str, str | None]:
        return {"name": name}

    @Get("/list")
    async def many(self, tag: list[str], number: list[int] = []) -> dict[str, Any]:  # noqa: B006
        return {"tag": tag, "number": number}

    @Get("/aliased")
    async def aliased(self, per_page: Annotated[int, Query("perPage")] = 10) -> dict[str, int]:
        return {"per_page": per_page}


async def test_query_defaults() -> None:
    assert (await call(QueryController, "GET", "/q")).json() == {"page": 1, "size": 20}


async def test_query_values_are_converted() -> None:
    response = await call(QueryController, "GET", "/q", params={"page": 3, "size": 5})
    assert response.json() == {"page": 3, "size": 5}


async def test_query_scalar_types() -> None:
    token = "12345678-1234-5678-1234-567812345678"
    response = await call(
        QueryController,
        "GET",
        "/q/types",
        params={"flag": "yes", "ratio": "1.5", "colour": "blue", "mode": "fast", "token": token},
    )
    assert response.json() == {
        "flag": "True",
        "ratio": "1.5",
        "colour": "blue",
        "mode": "fast",
        "token": token,
    }


async def test_optional_query_defaults_to_none() -> None:
    assert (await call(QueryController, "GET", "/q/optional")).json() == {"name": None}


async def test_repeated_query_parameters() -> None:
    response = await call(
        QueryController, "GET", "/q/list", params={"tag": ["a", "b"], "number": 3}
    )
    assert response.json() == {"tag": ["a", "b"], "number": [3]}


async def test_query_alias() -> None:
    response = await call(QueryController, "GET", "/q/aliased", params={"perPage": 7})
    assert response.json() == {"per_page": 7}


async def test_a_missing_required_query_parameter_is_422() -> None:
    response = await call(QueryController, "GET", "/q/types")
    assert response.status == 422
    detail = response.json()["detail"]
    assert {error["field"] for error in detail} == {"flag", "ratio", "colour", "mode", "token"}
    assert all(error["location"] == "query" for error in detail)
    assert all(error["message"] == "field required" for error in detail)


async def test_a_bad_query_value_is_422() -> None:
    response = await call(QueryController, "GET", "/q", params={"page": "nope"})
    assert response.status == 422
    assert response.json()["detail"] == [
        {"location": "query", "field": "page", "message": "expected an integer"}
    ]


async def test_an_unknown_enum_value_is_422() -> None:
    response = await call(QueryController, "GET", "/q/types", params={"colour": "green"})
    messages = {error["field"]: error["message"] for error in response.json()["detail"]}
    assert messages["colour"] == "expected one of 'red', 'blue'"


async def test_every_bad_field_is_reported_at_once() -> None:
    response = await call(QueryController, "GET", "/q", params={"page": "x", "size": "y"})
    assert {error["field"] for error in response.json()["detail"]} == {"page", "size"}


# -- path -------------------------------------------------------------------


@Route("/p")
class PathController:
    @Get("/{id:int}")
    async def show(self, id: int) -> dict[str, int]:
        return {"id": id}

    @Get("/loose/{id}")
    async def loose(self, id: int) -> dict[str, int]:
        return {"id": id}

    @Get("/{id:int}/tags/{tag}")
    async def tag(self, id: int, tag: str) -> dict[str, Any]:
        return {"id": id, "tag": tag}


async def test_path_parameter() -> None:
    assert (await call(PathController, "GET", "/p/7")).json() == {"id": 7}


async def test_path_parameter_is_validated_against_the_annotation() -> None:
    response = await call(PathController, "GET", "/p/loose/abc")
    assert response.status == 422
    assert response.json()["detail"] == [
        {"location": "path", "field": "id", "message": "expected an integer"}
    ]


async def test_several_path_parameters() -> None:
    assert (await call(PathController, "GET", "/p/3/tags/blue")).json() == {"id": 3, "tag": "blue"}


# -- headers and cookies ----------------------------------------------------


@Route("/h")
class HeaderController:
    @Get
    async def show(
        self,
        x_agent: Annotated[str, Header()] = "none",
        token: Annotated[str | None, Header("x-token")] = None,
        session: Annotated[str, Cookie()] = "anonymous",
    ) -> dict[str, str | None]:
        return {"agent": x_agent, "token": token, "session": session}

    @Get("/required")
    async def required(self, token: Annotated[str, Header("x-token")]) -> str:
        return token


async def test_headers_and_cookies() -> None:
    response = await call(
        HeaderController,
        "GET",
        "/h",
        headers={"x-agent": "pytest", "x-token": "abc"},
        cookies={"session": "s1"},
    )
    assert response.json() == {"agent": "pytest", "token": "abc", "session": "s1"}


async def test_header_defaults() -> None:
    assert (await call(HeaderController, "GET", "/h")).json() == {
        "agent": "none",
        "token": None,
        "session": "anonymous",
    }


async def test_a_missing_required_header_is_422() -> None:
    response = await call(HeaderController, "GET", "/h/required")
    assert response.json()["detail"] == [
        {"location": "header", "field": "x-token", "message": "field required"}
    ]


# -- bodies -----------------------------------------------------------------


@Route("/b")
class BodyController:
    @Post
    async def create(self, data: UserIn) -> dict[str, Any]:
        return {
            "name": data.name,
            "email": data.email,
            "age": data.age,
            "colour": data.colour.value,
            "city": data.address.city if data.address else None,
            "tags": data.tags,
        }

    @Post("/point")
    async def point(self, data: PointIn) -> dict[str, int]:
        return {"x": data["x"], "y": data["y"]}

    @Post("/many")
    async def many(self, data: list[UserIn]) -> dict[str, int]:
        return {"count": len(data)}

    @Post("/scalar")
    async def scalar(self, value: Annotated[int, Body()]) -> dict[str, int]:
        return {"value": value}

    @Post("/fields")
    async def fields(
        self,
        count: Annotated[int, Body("count")],
        label: Annotated[str, Body("label")] = "none",
    ) -> dict[str, Any]:
        return {"count": count, "label": label}

    @Post("/model")
    async def model(self, data: FakeModel) -> dict[str, str]:
        return {"name": data.name}

    @Post("/optional")
    async def optional(self, data: UserIn | None = None) -> dict[str, str | None]:
        return {"name": data.name if data else None}


async def test_dataclass_body() -> None:
    response = await call(
        BodyController,
        "POST",
        "/b",
        json={
            "name": "ada",
            "email": "ada@example.com",
            "age": 36,
            "colour": "blue",
            "address": {"city": "london"},
            "tags": ["x"],
            "extra": "ignored",
        },
    )
    assert response.json() == {
        "name": "ada",
        "email": "ada@example.com",
        "age": 36,
        "colour": "blue",
        "city": "london",
        "tags": ["x"],
    }


async def test_dataclass_body_defaults() -> None:
    response = await call(BodyController, "POST", "/b", json={"name": "a", "email": "b"})
    assert response.json() == {
        "name": "a",
        "email": "b",
        "age": None,
        "colour": "red",
        "city": None,
        "tags": [],
    }


async def test_typed_dict_body() -> None:
    assert (await call(BodyController, "POST", "/b/point", json={"x": 1, "y": 2})).json() == {
        "x": 1,
        "y": 2,
    }


async def test_list_body() -> None:
    payload = [{"name": "a", "email": "b"}, {"name": "c", "email": "d"}]
    assert (await call(BodyController, "POST", "/b/many", json=payload)).json() == {"count": 2}


async def test_scalar_body() -> None:
    assert (await call(BodyController, "POST", "/b/scalar", json=42)).json() == {"value": 42}


async def test_named_body_fields_are_picked_out_of_the_object() -> None:
    response = await call(BodyController, "POST", "/b/fields", json={"count": 7, "label": "x"})
    assert response.json() == {"count": 7, "label": "x"}


async def test_a_named_body_field_falls_back_to_its_default() -> None:
    response = await call(BodyController, "POST", "/b/fields", json={"count": 7})
    assert response.json() == {"count": 7, "label": "none"}


async def test_a_missing_named_body_field_is_422() -> None:
    response = await call(BodyController, "POST", "/b/fields", json={"label": "x"})
    assert response.status == 422
    assert response.json()["detail"] == [
        {"location": "body", "field": "count", "message": "field required"}
    ]


async def test_a_named_body_field_needs_an_object() -> None:
    """A body that is not an object has no fields to pick, and every one says so."""
    response = await call(BodyController, "POST", "/b/fields", json=[1, 2])
    assert response.status == 422
    assert response.json()["detail"] == [
        {"location": "body", "field": "count", "message": "expected an object"},
        {"location": "body", "field": "label", "message": "expected an object"},
    ]


async def test_duck_typed_model_body() -> None:
    assert (await call(BodyController, "POST", "/b/model", json={"name": "ada"})).json() == {
        "name": "ada"
    }


async def test_duck_typed_model_errors_become_422() -> None:
    response = await call(BodyController, "POST", "/b/model", json={})
    assert response.status == 422
    assert "name is required" in response.json()["detail"][0]["message"]


async def test_a_missing_body_is_422() -> None:
    response = await call(BodyController, "POST", "/b")
    assert response.status == 422
    assert response.json()["detail"] == [
        {"location": "body", "field": "", "message": "a request body is required"}
    ]


async def test_an_optional_body_may_be_absent() -> None:
    assert (await call(BodyController, "POST", "/b/optional")).json() == {"name": None}


async def test_missing_body_fields_are_422() -> None:
    response = await call(BodyController, "POST", "/b", json={"name": "ada"})
    assert response.status == 422
    assert response.json()["detail"] == [
        {"location": "body", "field": "email", "message": "field required"}
    ]


async def test_bad_body_fields_report_their_path() -> None:
    response = await call(
        BodyController,
        "POST",
        "/b",
        json={"name": "ada", "email": "e", "age": "old", "address": {"city": 12}},
    )
    assert response.status == 422
    reported = {error["field"]: error["message"] for error in response.json()["detail"]}
    assert reported == {"age": "expected an integer", "address.city": "expected a string"}


async def test_a_body_that_is_not_an_object_is_422() -> None:
    response = await call(BodyController, "POST", "/b", json=[1, 2])
    assert response.json()["detail"] == [
        {"location": "body", "field": "", "message": "expected an object"}
    ]


async def test_list_body_reports_the_index() -> None:
    response = await call(BodyController, "POST", "/b/many", json=[{"name": "a"}])
    assert response.json()["detail"] == [
        {"location": "body", "field": "0.email", "message": "field required"}
    ]


# -- forms ------------------------------------------------------------------


@Route("/f")
class FormController:
    @Post
    async def submit(
        self,
        name: Annotated[str, Form()],
        age: Annotated[int, Form()] = 0,
        tags: Annotated[list[str], Form("tag")] = [],  # noqa: B006 - copied per request
    ) -> dict[str, Any]:
        return {"name": name, "age": age, "tags": tags}


async def test_form_body() -> None:
    response = await call(
        FormController,
        "POST",
        "/f",
        content="name=ada&age=36&tag=x&tag=y",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.json() == {"name": "ada", "age": 36, "tags": ["x", "y"]}


async def test_a_mutable_default_is_not_shared_between_requests() -> None:
    """A handler that mutates a defaulted container must not affect the next request."""

    @Route("/m")
    class MutableController:
        @Get
        async def collect(
            self,
            tags: Annotated[list[str], Query()] = [],  # noqa: B006 - copied per request
        ) -> dict[str, Any]:
            tags.append("added")
            return {"tags": tags}

    async with TestClient(App(controllers=[MutableController])) as client:
        first = await client.get("/m")
        second = await client.get("/m")
    assert first.json() == {"tags": ["added"]}
    assert second.json() == {"tags": ["added"]}


async def test_a_missing_form_field_is_422() -> None:
    response = await call(
        FormController,
        "POST",
        "/f",
        content="age=1",
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert response.json()["detail"] == [
        {"location": "form", "field": "name", "message": "field required"}
    ]


# -- mixed and framework types ----------------------------------------------


async def test_path_query_body_and_request_together() -> None:
    @Route("/mix")
    class MixController:
        @Post("/{id:int}")
        async def update(
            self, id: int, data: UserIn, request: Request, notify: bool = False
        ) -> dict[str, Any]:
            return {
                "id": id,
                "name": data.name,
                "notify": notify,
                "method": request.method,
            }

    async with TestClient(App(controllers=[MixController])) as client:
        response = await client.post(
            "/mix/5", params={"notify": "true"}, json={"name": "ada", "email": "e"}
        )
    assert response.json() == {"id": 5, "name": "ada", "notify": True, "method": "POST"}


# -- startup errors ---------------------------------------------------------


def test_a_collection_path_parameter_is_refused() -> None:
    @Route("/bad")
    class BadController:
        @Get("/{ids}")
        async def index(self, ids: list[int]) -> str:
            return str(ids)

    with pytest.raises(RouteConfigurationError, match="cannot be a collection"):
        App(controllers=[BadController])


def test_a_wide_union_is_refused() -> None:
    @Route("/bad")
    class BadController:
        @Get
        async def index(self, value: int | str) -> str:
            return str(value)

    with pytest.raises(RouteConfigurationError, match="unions of several types"):
        App(controllers=[BadController])
