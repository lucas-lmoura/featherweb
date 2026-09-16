"""Validators compiled from a type hint.

A validator takes whatever came off the wire — a string from a query, a value
from a parsed JSON body — and returns it as the declared type, or complains.
Compilation happens once, when the route is registered.

Coercion is deliberate but narrow: strings become numbers, enums and UUIDs
because query strings have no types, and nothing else is guessed. ``True`` is
not an integer here, even though Python says it is.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Final, cast

from ._introspect import (
    MISSING,
    FieldSpec,
    collection_of,
    enum_type,
    literal_values,
    mapping_of,
    model_fields,
    model_validate_of,
    unwrap_annotated,
    unwrap_optional,
)

__all__ = ["Invalid", "Validator", "compile_validator"]

type Validator = Callable[[Any], Any]
#: One problem: the path down to the offending field, and what is wrong with it.
type Problem = tuple[tuple[str, ...], str]

_TRUE: Final = frozenset({"true", "1", "yes", "on", "t"})
_FALSE: Final = frozenset({"false", "0", "no", "off", "f"})


class Invalid(Exception):
    """Carries every problem found while validating one value."""

    def __init__(self, problems: list[Problem] | str) -> None:
        if isinstance(problems, str):
            problems = [((), problems)]
        self.problems = problems
        super().__init__("; ".join(message for _, message in problems))

    def under(self, name: str) -> Invalid:
        """The same problems, seen from one level up."""
        return Invalid([((name, *path), message) for path, message in self.problems])


class UnsupportedType(TypeError):
    """A hint the framework does not know how to validate."""


def compile_validator(hint: Any, *, where: str = "") -> Validator:
    """Build the validator for ``hint``; raises ``UnsupportedType`` if it cannot."""
    hint, _ = unwrap_annotated(hint)
    if hint is None:
        hint = type(None)  # an unresolved annotation may still be the bare None
    inner, optional = unwrap_optional(hint)
    if _is_union(inner):
        raise UnsupportedType(f"{where}: unions of several types are not supported ({hint})")
    validate = _compile(inner, where)
    if not optional:
        return validate

    def validate_optional(value: Any) -> Any:
        return None if value is None else validate(value)

    return validate_optional


def _is_union(hint: Any) -> bool:
    import types
    from typing import Union, get_origin

    origin = get_origin(hint)
    return origin is Union or origin is types.UnionType


def _compile(hint: Any, where: str) -> Validator:
    if hint is Any or hint is object or hint is MISSING:
        return _identity
    if hint is type(None):
        return _none
    if hint is str:
        return _as_str
    if hint is bool:
        return _as_bool
    if hint is int:
        return _as_int
    if hint is float:
        return _as_float
    if hint is bytes:
        return _as_bytes

    scalar = _scalar_from_string(hint)
    if scalar is not None:
        return scalar

    allowed = literal_values(hint)
    if allowed is not None:
        return _literal(allowed)

    enum = enum_type(hint)
    if enum is not None:
        return _enum(enum)

    collection = collection_of(hint)
    if collection is not None:
        container, item = collection
        return _collection(container, compile_validator(item, where=where))

    mapping = mapping_of(hint)
    if mapping is not None:
        _, value_hint = mapping
        return _mapping(compile_validator(value_hint, where=where))

    fields = model_fields(hint)
    if fields is not None:
        return _model(hint, fields, where)

    validate_model = model_validate_of(hint)
    if validate_model is not None:
        return _external_model(validate_model)

    raise UnsupportedType(
        f"{where}: cannot read {_name(hint)} from a request. Use a dataclass, a TypedDict, "
        f"a pydantic model or one of str, int, float, bool, Enum, Literal, UUID, list[...]."
    )


# -- scalars ----------------------------------------------------------------


def _identity(value: Any) -> Any:
    return value


def _none(value: Any) -> Any:
    if value is None:
        return None
    raise Invalid("expected null")


def _as_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    raise Invalid("expected a string")


def _as_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value.encode("utf-8")
    raise Invalid("expected bytes")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
    raise Invalid("expected a boolean")


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        raise Invalid("expected an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise Invalid("expected an integer")
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            pass
    raise Invalid("expected an integer")


def _as_float(value: Any) -> float:
    if isinstance(value, bool):
        raise Invalid("expected a number")
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            pass
    raise Invalid("expected a number")


def _scalar_from_string(hint: Any) -> Validator | None:
    """Validators for the standard library types that parse from text."""
    from datetime import date, datetime, time
    from decimal import Decimal, InvalidOperation
    from uuid import UUID

    if hint is UUID:
        return _parsed(UUID, UUID, "expected a UUID", (ValueError, AttributeError, TypeError))
    if hint is datetime:
        return _parsed(
            datetime, datetime.fromisoformat, "expected an ISO 8601 date and time", (ValueError,)
        )
    if hint is date:
        return _parsed(date, date.fromisoformat, "expected an ISO 8601 date", (ValueError,))
    if hint is time:
        return _parsed(time, time.fromisoformat, "expected an ISO 8601 time", (ValueError,))
    if hint is Decimal:
        return _parsed(
            Decimal, Decimal, "expected a decimal number", (InvalidOperation, ValueError, TypeError)
        )
    return None


def _parsed(
    target: type,
    parse: Callable[[str], Any],
    message: str,
    failures: tuple[type[BaseException], ...],
) -> Validator:
    def validate(value: Any) -> Any:
        if isinstance(value, target):
            return value
        if isinstance(value, str):
            try:
                return parse(value.strip())
            except failures:
                raise Invalid(message) from None
        raise Invalid(message)

    return validate


def _literal(allowed: tuple[Any, ...]) -> Validator:
    rendered = ", ".join(repr(option) for option in allowed)
    coercions = [compile_validator(type(option)) for option in allowed]

    def validate(value: Any) -> Any:
        for option, coerce in zip(allowed, coercions, strict=True):
            if value == option:
                return option
            try:
                if coerce(value) == option:
                    return option
            except Invalid:
                continue
        raise Invalid(f"expected one of {rendered}")

    return validate


def _enum(enum: Any) -> Validator:
    members: list[Any] = list(enum)
    rendered = ", ".join(repr(member.value) for member in members)
    coercions = [compile_validator(type(member.value)) for member in members]

    def validate(value: Any) -> Any:
        if isinstance(value, enum):
            return value
        for member, coerce in zip(members, coercions, strict=True):
            if value == member.value:
                return member
            try:
                if coerce(value) == member.value:
                    return member
            except Invalid:
                continue
        raise Invalid(f"expected one of {rendered}")

    return validate


# -- containers -------------------------------------------------------------


def _collection(container: Any, item: Validator) -> Validator:
    def validate(value: Any) -> Any:
        if isinstance(value, str | bytes) or not hasattr(value, "__iter__"):
            values = [value]
        else:
            values = list(value)
        result: list[Any] = []
        problems: list[Problem] = []
        for index, element in enumerate(values):
            try:
                result.append(item(element))
            except Invalid as exc:
                problems.extend(exc.under(str(index)).problems)
        if problems:
            raise Invalid(problems)
        return container(result)

    return validate


def _mapping(value_validator: Validator) -> Validator:
    def validate(value: Any) -> Any:
        if not isinstance(value, Mapping):
            raise Invalid("expected an object")
        given = cast(Mapping[Any, Any], value)
        result: dict[Any, Any] = {}
        problems: list[Problem] = []
        for key, element in given.items():
            try:
                result[key] = value_validator(element)
            except Invalid as exc:
                problems.extend(exc.under(str(key)).problems)
        if problems:
            raise Invalid(problems)
        return result

    return validate


# -- models -----------------------------------------------------------------


def _model(hint: Any, fields: list[FieldSpec], where: str) -> Validator:
    compiled = [(spec, compile_validator(spec.hint, where=where)) for spec in fields]
    is_typed_dict = isinstance(hint, type) and issubclass(hint, dict)

    construct = cast(Callable[..., Any], hint)

    def build(arguments: dict[str, Any]) -> Any:
        # A TypedDict is just the dict itself; anything else is constructed.
        return arguments if is_typed_dict else construct(**arguments)

    def validate(value: Any) -> Any:
        if not isinstance(value, Mapping):
            raise Invalid("expected an object")
        given = cast(Mapping[str, Any], value)
        arguments: dict[str, Any] = {}
        problems: list[Problem] = []
        for spec, validate_field in compiled:
            if spec.name in given:
                try:
                    arguments[spec.name] = validate_field(given[spec.name])
                except Invalid as exc:
                    problems.extend(exc.under(spec.name).problems)
            elif spec.required:
                problems.append(((spec.name,), "field required"))
            # Anything else keeps the default the model itself declares.
        if problems:
            raise Invalid(problems)
        return build(arguments)

    return validate


def _external_model(validate_model: Callable[[Any], Any]) -> Validator:
    """Delegate to a pydantic-style model, translating its errors into ours."""

    def validate(value: Any) -> Any:
        try:
            return validate_model(value)
        except Exception as exc:
            raise Invalid(_external_problems(exc)) from None

    return validate


def _external_problems(exc: Exception) -> list[Problem]:
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return [((), str(exc))]
    problems: list[Problem] = []
    for raw in cast(Iterable[Any], errors()):
        if not isinstance(raw, Mapping):
            continue
        error = cast(Mapping[str, Any], raw)
        location = tuple(str(part) for part in error.get("loc", ()))
        problems.append((location, str(error.get("msg", "invalid value"))))
    return problems or [((), str(exc))]


def _name(hint: Any) -> str:
    return getattr(hint, "__name__", None) or str(hint)
