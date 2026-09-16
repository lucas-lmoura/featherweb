"""Serializers compiled from a handler's return annotation.

The annotation decides the shape of the response: a dataclass or ``TypedDict``
becomes a JSON object with exactly its declared fields, ``list[T]`` becomes an
array of those, and the primitives are left alone.

Fields are read by attribute, falling back to item access, so the handler can
return an ORM row, a dict or anything else that carries the right names — and
whatever it carries beyond the declared fields is simply left out.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from ._introspect import (
    collection_of,
    enum_type,
    literal_values,
    mapping_of,
    model_fields,
    resolve_hints,
    unwrap_annotated,
    unwrap_optional,
)

__all__ = [
    "SerializationError",
    "Serializer",
    "compile_serializer",
    "resolve_return_hint",
    "unwrap_response",
]

type Serializer = Callable[[Any], Any]

_PASSTHROUGH: frozenset[Any] = frozenset({str, int, float, bool, bytes, None, type(None)})


class SerializationError(TypeError):
    """The handler returned something its annotation does not describe."""


def compile_serializer(hint: Any) -> Serializer | None:
    """Build the serializer for ``hint``.

    ``None`` means "nothing to do": the value is already in a shape the
    response can render, so it is passed through untouched.
    """
    hint, _ = unwrap_annotated(hint)
    if hint is None or hint is Any:
        return None
    inner, optional = unwrap_optional(hint)
    serialize = _compile(inner)
    if serialize is None or not optional:
        return serialize

    def serialize_optional(value: Any) -> Any:
        return None if value is None else serialize(value)

    return serialize_optional


def unwrap_response(hint: Any) -> Any | None:
    """For a ``Response[T]`` annotation, the ``T`` whose serializer applies."""
    from typing import get_args, get_origin

    from .response import Response

    hint, _ = unwrap_annotated(hint)
    origin = get_origin(hint)
    if origin is not None and isinstance(origin, type) and issubclass(origin, Response):
        args = get_args(hint)
        return args[0] if args else None
    return None


def resolve_return_hint(handler: Callable[..., Any]) -> Any:
    """The return annotation of ``handler``, resolved, or ``None`` if absent."""
    return resolve_hints(handler).get("return")


def _compile(hint: Any) -> Serializer | None:
    if hint in _PASSTHROUGH:
        return None

    if enum_type(hint) is not None:
        return _enum

    if literal_values(hint) is not None:
        return None

    collection = collection_of(hint)
    if collection is not None:
        _, item = collection
        return _sequence(compile_serializer(item))

    mapping = mapping_of(hint)
    if mapping is not None:
        _, value_hint = mapping
        return _mapping(compile_serializer(value_hint))

    fields = model_fields(hint)
    if fields is not None:
        return _model([(spec.name, compile_serializer(spec.hint)) for spec in fields])

    scalar = _known_scalar(hint)
    if scalar is not None:
        return scalar

    if isinstance(hint, type) and hasattr(hint, "model_dump"):
        return _model_dump

    # An unknown type: keep the value as it is and let the JSON encoder decide.
    return None


def _known_scalar(hint: Any) -> Serializer | None:
    from datetime import date, datetime, time
    from decimal import Decimal
    from uuid import UUID

    if hint is UUID:
        return str
    if hint in (datetime, date, time):
        return _isoformat
    if hint is Decimal:
        return str
    return None


def _isoformat(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _enum(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _model_dump(value: Any) -> Any:
    dump = getattr(value, "model_dump", None)
    return dump(mode="json") if callable(dump) else value


def _sequence(item: Serializer | None) -> Serializer:
    if item is None:
        return _as_list

    def serialize(value: Any) -> Any:
        return [item(element) for element in _iterable(value)]

    return serialize


def _as_list(value: Any) -> Any:
    return list(_iterable(value))


def _iterable(value: Any) -> Any:
    if isinstance(value, str | bytes) or not hasattr(value, "__iter__"):
        raise SerializationError(f"expected a sequence to serialize, got {type(value).__name__}")
    return value


def _mapping(value_serializer: Serializer | None) -> Serializer:
    if value_serializer is None:
        return _as_dict

    def serialize(value: Any) -> Any:
        return {key: value_serializer(item) for key, item in _items(value)}

    return serialize


def _as_dict(value: Any) -> Any:
    return dict(_items(value))


def _items(value: Any) -> Any:
    if isinstance(value, Mapping):
        return cast(Mapping[Any, Any], value).items()
    raise SerializationError(f"expected a mapping to serialize, got {type(value).__name__}")


def _model(fields: list[tuple[str, Serializer | None]]) -> Serializer:
    def serialize(value: Any) -> Any:
        return {name: _field(value, name, serializer) for name, serializer in fields}

    return serialize


def _field(value: Any, name: str, serializer: Serializer | None) -> Any:
    field: Any
    if isinstance(value, Mapping):
        try:
            field = cast(Mapping[str, Any], value)[name]
        except KeyError:
            raise SerializationError(f"the returned object has no field {name!r}") from None
    else:
        try:
            field = getattr(value, name)
        except AttributeError:
            raise SerializationError(f"the returned object has no field {name!r}") from None
    if field is None or serializer is None:
        return field
    return serializer(field)
