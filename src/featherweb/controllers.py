"""The decorators that turn a plain class into a controller.

They only attach metadata and hand the original object back, so type checkers
keep seeing the class and the methods you wrote. Nothing is registered here:
:meth:`featherweb.App.scan` collects the marks at startup.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar, Final, TypeVar, overload

__all__ = [
    "Delete",
    "Get",
    "Head",
    "Options",
    "Patch",
    "Post",
    "Put",
    "Route",
    "RouteMark",
]

#: Attribute where the verb decorators record what they matched.
ROUTES_ATTR: Final = "__featherweb_routes__"
#: Attribute where ``@Route`` records the controller's path prefix.
PREFIX_ATTR: Final = "__featherweb_prefix__"

_ClassT = TypeVar("_ClassT", bound=type)
_HandlerT = TypeVar("_HandlerT", bound=Callable[..., Any])


class RouteMark:
    """One verb decoration: everything the router needs except the prefix."""

    __slots__ = ("method", "path", "status")

    def __init__(self, method: str, path: str, status: int) -> None:
        self.method = method
        self.path = path
        self.status = status

    def __repr__(self) -> str:
        return f"RouteMark({self.method} {self.path!r} -> {self.status})"


class Route:
    """Give a controller class its path prefix.

    Works bare or with a prefix::

        @Route("/users")
        class UserController: ...
    """

    __slots__ = ()

    @overload
    def __new__(cls, prefix: _ClassT, /) -> _ClassT: ...
    @overload
    def __new__(cls, prefix: str = "", /) -> Callable[[_ClassT], _ClassT]: ...

    def __new__(cls, prefix: Any = "", /) -> Any:
        if isinstance(prefix, type):
            return _set_prefix(prefix, "")

        def decorate(target: _ClassT) -> _ClassT:
            return _set_prefix(target, prefix)

        return decorate


def require_class(target: object, decorator: str) -> None:
    """Guard the decorators against being used on a function."""
    if not isinstance(target, type):
        raise TypeError(f"@{decorator} can only be used on a class")


def _set_prefix[ClassT: type](target: ClassT, prefix: str) -> ClassT:
    require_class(target, "Route")
    setattr(target, PREFIX_ATTR, prefix)
    return target


class _Verb:
    """Base for the HTTP verb decorators.

    ``@Get`` maps the controller prefix itself; ``@Get("/{id:int}")`` appends to
    it; ``status`` sets the response status for the successful case.
    """

    __slots__ = ()
    method: ClassVar[str] = ""

    @overload
    def __new__(cls, path: _HandlerT, /) -> _HandlerT: ...
    @overload
    def __new__(
        cls, path: str = "", /, *, status: int = 200
    ) -> Callable[[_HandlerT], _HandlerT]: ...

    def __new__(cls, path: Any = "", /, *, status: int = 200) -> Any:
        if callable(path):
            return _add_route(path, cls.method, "", status)

        def decorate(handler: _HandlerT) -> _HandlerT:
            return _add_route(handler, cls.method, path, status)

        return decorate


def _add_route[HandlerT: Callable[..., Any]](
    handler: HandlerT, method: str, path: str, status: int
) -> HandlerT:
    if not callable(handler):
        raise TypeError(f"@{method.title()} can only be used on a method")
    marks: list[RouteMark] = list(getattr(handler, ROUTES_ATTR, ()))
    marks.append(RouteMark(method, path, status))
    setattr(handler, ROUTES_ATTR, marks)
    return handler


class Get(_Verb):
    """Answer ``GET`` on this path."""

    __slots__ = ()
    method = "GET"


class Post(_Verb):
    """Answer ``POST`` on this path."""

    __slots__ = ()
    method = "POST"


class Put(_Verb):
    """Answer ``PUT`` on this path."""

    __slots__ = ()
    method = "PUT"


class Patch(_Verb):
    """Answer ``PATCH`` on this path."""

    __slots__ = ()
    method = "PATCH"


class Delete(_Verb):
    """Answer ``DELETE`` on this path."""

    __slots__ = ()
    method = "DELETE"


class Options(_Verb):
    """Answer ``OPTIONS`` on this path."""

    __slots__ = ()
    method = "OPTIONS"


class Head(_Verb):
    """Answer ``HEAD`` on this path.

    Declaring it is optional: a ``GET`` route already answers ``HEAD``.
    """

    __slots__ = ()
    method = "HEAD"
