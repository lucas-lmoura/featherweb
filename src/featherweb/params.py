"""Turning a request into the arguments a handler asked for.

The work happens once, when the route is registered: a handler's signature is
inspected there and compiled into a small list of instructions, so serving a
request is a loop over a tuple instead of a walk over annotations.

This phase binds path parameters, framework types and defaults. Inference of
query strings and request bodies from type hints comes with the typed input
layer, and plugs into the same compilation step.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import Enum, auto
from typing import Any

from .request import Request

__all__ = ["Binder", "RouteConfigurationError", "compile_binder"]


class RouteConfigurationError(TypeError):
    """A handler declares something the framework cannot provide."""


class _Source(Enum):
    PATH = auto()
    REQUEST = auto()
    EXCEPTION = auto()


class Binder:
    """Compiled recipe for a handler's arguments."""

    __slots__ = ("_instructions",)

    def __init__(self, instructions: tuple[tuple[str, _Source], ...]) -> None:
        self._instructions = instructions

    @property
    def empty(self) -> bool:
        return not self._instructions

    def build(
        self,
        request: Request,
        path_params: Mapping[str, Any],
        exception: BaseException | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {}
        for name, source in self._instructions:
            if source is _Source.PATH:
                arguments[name] = path_params[name]
            elif source is _Source.REQUEST:
                arguments[name] = request
            else:
                arguments[name] = exception
        return arguments

    def __repr__(self) -> str:
        bound = ", ".join(f"{name}={source.name.lower()}" for name, source in self._instructions)
        return f"Binder({bound})"


def compile_binder(
    handler: Callable[..., Any],
    *,
    path_params: tuple[str, ...] = (),
    exception_type: type[BaseException] | None = None,
    where: str = "",
) -> Binder:
    """Work out where each parameter of ``handler`` comes from.

    ``handler`` is the bound method, so ``self`` is already out of the picture.
    Anything the framework cannot supply is an error here, at startup, rather
    than a surprise on the first request.
    """
    import inspect  # deferred: registration only, never on the request path

    hints = _type_hints(handler)
    instructions: list[tuple[str, _Source]] = []
    wants_exception = exception_type is not None
    for name, parameter in inspect.signature(handler).parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        hint = hints.get(name, parameter.annotation)
        if name in path_params:
            instructions.append((name, _Source.PATH))
        elif _is_request(hint):
            instructions.append((name, _Source.REQUEST))
        elif wants_exception:
            # An exception handler gets the exception in its first free parameter.
            instructions.append((name, _Source.EXCEPTION))
            wants_exception = False
        elif parameter.default is not parameter.empty:
            continue  # the handler already says what to do without us
        else:
            raise RouteConfigurationError(
                f"{where or getattr(handler, '__qualname__', handler)}: cannot provide "
                f"parameter {name!r}. Declare it as a path parameter, annotate it with a "
                f"framework type such as Request, or give it a default."
            )
    return Binder(tuple(instructions))


def _type_hints(handler: Callable[..., Any]) -> dict[str, Any]:
    from typing import get_type_hints

    try:
        return get_type_hints(handler)
    except Exception:  # unresolvable forward reference: fall back to the raw strings
        return dict(getattr(handler, "__annotations__", {}))


def _is_request(hint: Any) -> bool:
    return hint is Request or (isinstance(hint, str) and hint.split(".")[-1] == "Request")
