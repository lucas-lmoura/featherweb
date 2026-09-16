"""Reading type hints.

Everything here runs while routes are being registered, never while serving, so
the expensive standard library modules it needs (``typing``'s resolution,
``dataclasses``, ``inspect``) are imported inside the functions and stay off the
package's import path.
"""

from __future__ import annotations

import types
from collections.abc import Callable
from typing import Any, Final, Literal, Union, cast, get_args, get_origin

__all__ = [
    "MISSING",
    "FieldSpec",
    "collection_of",
    "enum_type",
    "is_body_hint",
    "is_model",
    "literal_values",
    "mapping_of",
    "model_fields",
    "model_validate_of",
    "resolve_hints",
    "unwrap_annotated",
    "unwrap_optional",
]


class _Missing:
    """Sentinel for "no value at all", which ``None`` cannot express."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING"

    def __bool__(self) -> bool:
        return False


MISSING: Final = _Missing()


class FieldSpec:
    """One field of a dataclass or ``TypedDict``."""

    __slots__ = ("default", "default_factory", "hint", "name", "required")

    def __init__(
        self,
        name: str,
        hint: Any,
        *,
        required: bool,
        default: Any = MISSING,
        default_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.name = name
        self.hint = hint
        self.required = required
        self.default = default
        self.default_factory = default_factory

    def __repr__(self) -> str:
        return f"FieldSpec({self.name}: {self.hint}, required={self.required})"


def resolve_hints(target: Any) -> dict[str, Any]:
    """Annotations of a function or class, with forward references resolved.

    Unresolvable annotations fall back to their raw form instead of blowing up
    the whole registration.
    """
    from typing import get_type_hints

    try:
        return get_type_hints(target, include_extras=True)
    except Exception:
        return dict(getattr(target, "__annotations__", {}))


def unwrap_annotated(hint: Any) -> tuple[Any, tuple[Any, ...]]:
    """Split ``Annotated[T, ...]`` into ``T`` and its metadata."""
    metadata = getattr(hint, "__metadata__", None)
    if metadata is None:
        return hint, ()
    return hint.__origin__, tuple(metadata)


def unwrap_optional(hint: Any) -> tuple[Any, bool]:
    """Split ``T | None`` into ``T`` and whether ``None`` was allowed."""
    origin = get_origin(hint)
    if origin is not Union and origin is not types.UnionType:
        return hint, False
    args = get_args(hint)
    present = [arg for arg in args if arg is not type(None)]
    optional = len(present) != len(args)
    if len(present) == 1:
        return present[0], optional
    if not present:
        return type(None), True
    return hint, optional  # a wider union: left for the caller to reject


def collection_of(hint: Any) -> tuple[Any, Any] | None:
    """``(container, item type)`` for ``list[T]``, ``set[T]`` or ``tuple[T, ...]``."""
    origin = get_origin(hint)
    if origin in (list, set, frozenset):
        args = get_args(hint)
        return origin, args[0] if args else Any
    if origin is tuple:
        args = get_args(hint)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple, args[0]
    if hint in (list, set, frozenset, tuple):
        return hint, Any
    return None


def mapping_of(hint: Any) -> tuple[Any, Any] | None:
    """``(key type, value type)`` for ``dict[K, V]`` and friends."""
    from collections.abc import Mapping, MutableMapping

    origin = get_origin(hint)
    if origin in (dict, Mapping, MutableMapping):
        args = get_args(hint)
        return (args[0], args[1]) if len(args) == 2 else (Any, Any)
    if hint is dict:
        return Any, Any
    return None


def literal_values(hint: Any) -> tuple[Any, ...] | None:
    """The allowed values of a ``Literal[...]``."""
    if get_origin(hint) is Literal:
        return get_args(hint)
    return None


def enum_type(hint: Any) -> Any | None:
    """``hint`` itself when it is an ``Enum`` subclass."""
    from enum import Enum

    if isinstance(hint, type) and issubclass(hint, Enum):
        return hint
    return None


def model_validate_of(hint: Any) -> Callable[[Any], Any] | None:
    """A pydantic-style ``model_validate``, found by duck typing."""
    validate = getattr(hint, "model_validate", None)
    return validate if callable(validate) else None


def model_fields(hint: Any) -> list[FieldSpec] | None:
    """Fields of a dataclass or ``TypedDict``, or ``None`` for anything else."""
    if not isinstance(hint, type):
        return None
    if hasattr(hint, "__dataclass_fields__"):
        return _dataclass_fields(hint)
    if hasattr(hint, "__required_keys__") and issubclass(hint, dict):
        return _typed_dict_fields(cast(type, hint))
    return None


def is_model(hint: Any) -> bool:
    """True for a dataclass, a ``TypedDict`` or a pydantic-style model."""
    return model_fields(hint) is not None or model_validate_of(hint) is not None


def is_body_hint(hint: Any) -> bool:
    """True for what is read from the request body unless told otherwise.

    Models, collections of models and mappings: none of them can come from a
    query string, so the body is the only place they could be.
    """
    hint, _ = unwrap_optional(hint)
    if is_model(hint):
        return True
    collection = collection_of(hint)
    if collection is not None:
        return is_body_hint(collection[1])
    return mapping_of(hint) is not None


def _dataclass_fields(hint: type) -> list[FieldSpec]:
    import dataclasses

    hints = resolve_hints(hint)
    specs: list[FieldSpec] = []
    for field in dataclasses.fields(hint):
        has_default = field.default is not dataclasses.MISSING
        factory = None if field.default_factory is dataclasses.MISSING else field.default_factory
        specs.append(
            FieldSpec(
                field.name,
                hints.get(field.name, field.type),
                required=not has_default and factory is None,
                default=field.default if has_default else MISSING,
                default_factory=factory,
            )
        )
    return specs


def _typed_dict_fields(hint: type) -> list[FieldSpec]:
    required: frozenset[str] = getattr(hint, "__required_keys__", frozenset())
    return [
        FieldSpec(name, field_hint, required=name in required)
        for name, field_hint in resolve_hints(hint).items()
    ]
