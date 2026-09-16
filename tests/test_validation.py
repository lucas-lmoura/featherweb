"""Unit tests for the validators compiled from type hints.

They see the values as they arrive: strings from a query, already-typed values
from a JSON body.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Literal
from uuid import UUID

import pytest

from featherweb.validation import Invalid, UnsupportedType, compile_validator


def valid(hint: Any, value: Any) -> Any:
    return compile_validator(hint)(value)


def invalid(hint: Any, value: Any) -> list[tuple[tuple[str, ...], str]]:
    with pytest.raises(Invalid) as info:
        compile_validator(hint)(value)
    return info.value.problems


class Priority(Enum):
    LOW = 1
    HIGH = 2


# -- scalars ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("hint", "given", "expected"),
    [
        (str, "x", "x"),
        (str, b"x", "x"),
        (int, "42", 42),
        (int, " 42 ", 42),
        (int, 42, 42),
        (int, 42.0, 42),
        (float, "1.5", 1.5),
        (float, 2, 2.0),
        (bool, "true", True),
        (bool, "OFF", False),
        (bool, True, True),
        (bytes, "abc", b"abc"),
        (bytes, b"abc", b"abc"),
    ],
)
def test_scalar_coercion(hint: Any, given: Any, expected: Any) -> None:
    assert valid(hint, given) == expected


@pytest.mark.parametrize(
    ("hint", "given", "message"),
    [
        (str, 1, "expected a string"),
        (int, "x", "expected an integer"),
        (int, True, "expected an integer"),
        (int, 1.5, "expected an integer"),
        (float, "x", "expected a number"),
        (float, False, "expected a number"),
        (bool, "maybe", "expected a boolean"),
        (bool, 1, "expected a boolean"),
        (bytes, 1, "expected bytes"),
        (None, "x", "expected null"),
    ],
)
def test_scalar_rejection(hint: Any, given: Any, message: str) -> None:
    assert invalid(hint, given) == [((), message)]


def test_none_is_accepted_for_none() -> None:
    assert valid(None, None) is None


def test_any_is_left_alone() -> None:
    marker = object()
    assert valid(Any, marker) is marker


def test_uuid() -> None:
    text = "12345678-1234-5678-1234-567812345678"
    assert valid(UUID, text) == UUID(text)
    assert valid(UUID, UUID(text)) == UUID(text)
    assert invalid(UUID, "nope") == [((), "expected a UUID")]


def test_dates_and_times() -> None:
    assert valid(datetime, "2026-09-15T12:30:00") == datetime(2026, 9, 15, 12, 30)
    assert valid(date, "2026-09-15") == date(2026, 9, 15)
    assert valid(time, "12:30") == time(12, 30)
    assert valid(date, date(2026, 9, 15)) == date(2026, 9, 15)
    assert invalid(date, "yesterday") == [((), "expected an ISO 8601 date")]
    assert invalid(datetime, 5) == [((), "expected an ISO 8601 date and time")]


def test_decimal() -> None:
    assert valid(Decimal, "1.50") == Decimal("1.50")
    assert invalid(Decimal, "many") == [((), "expected a decimal number")]


# -- enums and literals -----------------------------------------------------


def test_enum_by_value() -> None:
    assert valid(Priority, 2) is Priority.HIGH
    assert valid(Priority, "2") is Priority.HIGH  # query strings have no types
    assert valid(Priority, Priority.LOW) is Priority.LOW
    assert invalid(Priority, "9") == [((), "expected one of 1, 2")]


def test_literal() -> None:
    assert valid(Literal["fast", "slow"], "fast") == "fast"
    assert valid(Literal[1, 2], "2") == 2
    assert invalid(Literal["fast"], "quick") == [((), "expected one of 'fast'")]


# -- optionals and collections ----------------------------------------------


def test_optional() -> None:
    assert valid(int | None, None) is None
    assert valid(int | None, "3") == 3


def test_annotated_is_unwrapped() -> None:
    assert valid(Annotated[int, "ignored"], "3") == 3


def test_list() -> None:
    assert valid(list[int], ["1", "2"]) == [1, 2]
    assert valid(list[str], "single") == ["single"]  # one query value is still a list


def test_set_and_tuple() -> None:
    assert valid(set[int], ["1", "2", "2"]) == {1, 2}
    assert valid(tuple[int, ...], ["1", "2"]) == (1, 2)


def test_list_reports_the_index() -> None:
    assert invalid(list[int], ["1", "x", "y"]) == [
        (("1",), "expected an integer"),
        (("2",), "expected an integer"),
    ]


def test_dict() -> None:
    assert valid(dict[str, int], {"a": "1"}) == {"a": 1}
    assert invalid(dict[str, int], {"a": "x"}) == [(("a",), "expected an integer")]
    assert invalid(dict[str, int], "nope") == [((), "expected an object")]


# -- models -----------------------------------------------------------------


@dataclass
class Inner:
    value: int


@dataclass
class Outer:
    name: str
    inner: Inner
    tag: str = "none"


def test_nested_model() -> None:
    result = valid(Outer, {"name": "a", "inner": {"value": "3"}})
    assert result == Outer("a", Inner(3))
    assert result.tag == "none"


def test_nested_problems_carry_the_full_path() -> None:
    problems = invalid(Outer, {"inner": {"value": "x"}})
    assert sorted(problems) == [
        (("inner", "value"), "expected an integer"),
        (("name",), "field required"),
    ]


def test_a_model_needs_an_object() -> None:
    assert invalid(Outer, [1]) == [((), "expected an object")]


def test_list_of_models_reports_index_and_field() -> None:
    assert invalid(list[Inner], [{"value": 1}, {}]) == [(("1", "value"), "field required")]


# -- duck-typed models ------------------------------------------------------


class Model:
    """Shaped like pydantic: validated through ``model_validate``."""

    def __init__(self, ok: bool) -> None:
        self.ok = ok

    @classmethod
    def model_validate(cls, payload: Any) -> Model:
        del payload
        raise _PydanticStyleError()


class _PydanticStyleError(Exception):
    def errors(self) -> list[dict[str, Any]]:
        return [{"loc": ("address", "city"), "msg": "Input should be a valid string"}]


class PlainModel:
    @classmethod
    def model_validate(cls, payload: Any) -> PlainModel:
        del payload
        raise ValueError("no good")


def test_model_validate_errors_are_translated() -> None:
    assert invalid(Model, {}) == [(("address", "city"), "Input should be a valid string")]


def test_a_model_without_errors_still_reports_something() -> None:
    assert invalid(PlainModel, {}) == [((), "no good")]


# -- unsupported ------------------------------------------------------------


def test_an_unknown_type_is_refused_at_compile_time() -> None:
    class Database:
        pass

    with pytest.raises(UnsupportedType, match="cannot read Database"):
        compile_validator(Database, where="Ctrl.index")


def test_a_wide_union_is_refused_at_compile_time() -> None:
    with pytest.raises(UnsupportedType, match="unions of several types"):
        compile_validator(int | str)


def test_invalid_can_be_nested_by_hand() -> None:
    problems = Invalid("broken").under("field").problems
    assert problems == [(("field",), "broken")]
    assert str(Invalid("broken")) == "broken"
