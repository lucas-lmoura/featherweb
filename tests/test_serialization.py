"""What the return annotation does to the response.

These cover section 5.4 of the plan: the annotation decides the shape, fields
are read by attribute so any object serves, and anything the annotation does
not mention is left out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, TypedDict, cast
from uuid import UUID

import pytest

from featherweb import App, Get, Post, Response, Route
from featherweb.serialization import SerializationError, compile_serializer, unwrap_response
from featherweb.testing import TestClient, TestResponse


class Role(Enum):
    ADMIN = "admin"
    GUEST = "guest"


@dataclass
class Address:
    city: str
    country: str = "pt"


@dataclass
class UserOut:
    id: int
    name: str
    role: Role
    address: Address | None = None


class PointOut(TypedDict):
    x: int
    y: int


class Row:
    """Whatever the database handed back: more fields than the response declares."""

    def __init__(self, id: int, name: str, role: Role, secret: str = "hidden") -> None:
        self.id = id
        self.name = name
        self.role = role
        self.address = Address("lisbon")
        self.secret = secret


async def call(controller: type, method: str, path: str, **kwargs: Any) -> TestResponse:
    async with TestClient(App(controllers=[controller])) as client:
        return await client.request(method, path, **kwargs)


# -- compiled serializers ---------------------------------------------------


def test_primitives_are_passed_through() -> None:
    for hint in (str, int, float, bool, bytes, None, Any):
        assert compile_serializer(hint) is None


def test_dataclass_serializer() -> None:
    serialize = compile_serializer(UserOut)
    assert serialize is not None
    assert serialize(UserOut(1, "ada", Role.ADMIN)) == {
        "id": 1,
        "name": "ada",
        "role": "admin",
        "address": None,
    }


def test_fields_are_read_from_any_object() -> None:
    serialize = compile_serializer(UserOut)
    assert serialize is not None
    assert serialize(Row(2, "grace", Role.GUEST)) == {
        "id": 2,
        "name": "grace",
        "role": "guest",
        "address": {"city": "lisbon", "country": "pt"},
    }


def test_fields_can_be_read_from_a_mapping() -> None:
    serialize = compile_serializer(PointOut)
    assert serialize is not None
    assert serialize({"x": 1, "y": 2, "z": 3}) == {"x": 1, "y": 2}


def test_a_missing_field_is_an_error() -> None:
    serialize = compile_serializer(PointOut)
    assert serialize is not None
    with pytest.raises(SerializationError, match="no field 'y'"):
        serialize({"x": 1})


def test_list_serializer() -> None:
    serialize = compile_serializer(list[UserOut])
    assert serialize is not None
    assert serialize([Row(1, "a", Role.ADMIN)]) == [
        {"id": 1, "name": "a", "role": "admin", "address": {"city": "lisbon", "country": "pt"}}
    ]


def test_dict_serializer() -> None:
    serialize = compile_serializer(dict[str, Role])
    assert serialize is not None
    assert serialize({"one": Role.ADMIN}) == {"one": "admin"}


def test_scalars_that_json_cannot_hold() -> None:
    @dataclass
    class Stamped:
        id: UUID
        at: datetime
        day: date
        amount: Decimal

    serialize = compile_serializer(Stamped)
    assert serialize is not None
    value = Stamped(
        UUID("12345678-1234-5678-1234-567812345678"),
        datetime(2026, 9, 15, 12, 30),
        date(2026, 9, 15),
        Decimal("1.50"),
    )
    assert serialize(value) == {
        "id": "12345678-1234-5678-1234-567812345678",
        "at": "2026-09-15T12:30:00",
        "day": "2026-09-15",
        "amount": "1.50",
    }


def test_optional_serializer() -> None:
    serialize = compile_serializer(UserOut | None)
    assert serialize is not None
    assert serialize(None) is None


def test_wrong_shape_is_an_error() -> None:
    serialize = compile_serializer(list[UserOut])
    assert serialize is not None
    with pytest.raises(SerializationError, match="expected a sequence"):
        serialize(UserOut(1, "a", Role.ADMIN))


def test_unwrap_response() -> None:
    assert unwrap_response(Response[UserOut]) is UserOut
    assert unwrap_response(UserOut) is None


def test_a_duck_typed_model_is_dumped() -> None:
    class FakeModel:
        def model_dump(self, mode: str = "python") -> dict[str, Any]:
            del mode
            return {"name": "ada"}

    serialize = compile_serializer(FakeModel)
    assert serialize is not None
    assert serialize(FakeModel()) == {"name": "ada"}


# -- through a handler ------------------------------------------------------


@Route("/out")
class OutController:
    @Get("/user")
    async def user(self) -> UserOut:
        return Row(1, "ada", Role.ADMIN)  # type: ignore[return-value]

    @Get("/users")
    async def users(self) -> list[UserOut]:
        return [Row(1, "ada", Role.ADMIN), Row(2, "grace", Role.GUEST)]  # type: ignore[list-item]

    @Get("/point")
    async def point(self) -> PointOut:
        return {"x": 1, "y": 2}

    @Get("/text")
    async def text(self) -> str:
        return "plain"

    @Get("/raw")
    async def raw(self) -> bytes:
        return b"\x01\x02"

    @Get("/nothing")
    async def nothing(self) -> None:
        return None

    @Post("/created", status=201)
    async def created(self) -> UserOut:
        return UserOut(3, "hopper", Role.ADMIN)

    @Get("/wrapped")
    async def wrapped(self) -> Response[UserOut]:
        # Any object with the declared fields serves, hence the cast.
        response: Response[UserOut] = Response(cast(UserOut, Row(4, "lovelace", Role.GUEST)))
        response.headers["x-wrapped"] = "yes"
        return response

    @Get("/broken")
    async def broken(self) -> UserOut:
        return {"id": 1}  # type: ignore[return-value]

    @Get("/unannotated")
    async def unannotated(self):  # type: ignore[no-untyped-def]
        return {"anything": True}


async def test_declared_fields_only() -> None:
    response = await call(OutController, "GET", "/out/user")
    assert response.json() == {
        "id": 1,
        "name": "ada",
        "role": "admin",
        "address": {"city": "lisbon", "country": "pt"},
    }
    assert "secret" not in response.text


async def test_list_of_models() -> None:
    response = await call(OutController, "GET", "/out/users")
    assert [user["name"] for user in response.json()] == ["ada", "grace"]


async def test_typed_dict_return() -> None:
    assert (await call(OutController, "GET", "/out/point")).json() == {"x": 1, "y": 2}


async def test_str_and_bytes_returns() -> None:
    text = await call(OutController, "GET", "/out/text")
    assert text.headers["content-type"] == "text/plain; charset=utf-8"
    assert text.text == "plain"
    raw = await call(OutController, "GET", "/out/raw")
    assert raw.headers["content-type"] == "application/octet-stream"
    assert raw.body == b"\x01\x02"


async def test_none_return_is_204() -> None:
    assert (await call(OutController, "GET", "/out/nothing")).status == 204


async def test_status_from_the_verb_is_kept() -> None:
    response = await call(OutController, "POST", "/out/created")
    assert response.status == 201
    assert response.json()["name"] == "hopper"


async def test_response_of_t_is_serialized_too() -> None:
    response = await call(OutController, "GET", "/out/wrapped")
    assert response.headers["x-wrapped"] == "yes"
    assert response.json() == {
        "id": 4,
        "name": "lovelace",
        "role": "guest",
        "address": {"city": "lisbon", "country": "pt"},
    }


async def test_a_return_that_does_not_match_is_a_500() -> None:
    response = await call(OutController, "GET", "/out/broken")
    assert response.status == 500


async def test_without_an_annotation_the_value_is_sent_as_is() -> None:
    assert (await call(OutController, "GET", "/out/unannotated")).json() == {"anything": True}
