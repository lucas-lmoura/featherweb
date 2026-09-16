"""Errors, and the decorators that turn them into responses."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final, TypeVar, overload

from .controllers import require_class

__all__ = ["ControllerAdvice", "ExceptionHandler", "HTTPError"]

#: Attribute where ``@ExceptionHandler`` records the exception types it answers for.
HANDLES_ATTR: Final = "__featherweb_handles__"
#: Attribute marking a class as a source of global exception handlers.
ADVICE_ATTR: Final = "__featherweb_advice__"

_ClassT = TypeVar("_ClassT", bound=type)
_HandlerT = TypeVar("_HandlerT", bound=Callable[..., Any])


class HTTPError(Exception):
    """An error meant for the client, rendered as ``{"detail": ...}`` by default.

    Raise it directly::

        raise HTTPError(404, "user not found")

    or give a subclass its own status and detail::

        class UserNotFound(HTTPError):
            status = 404
            detail = "user not found"
    """

    status: int = 500
    detail: str = "internal server error"

    def __init__(
        self,
        status: int | None = None,
        detail: str | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if status is not None:
            self.status = status
        if detail is not None:
            self.detail = detail
        self.headers: dict[str, str] = dict(headers) if headers else {}
        super().__init__(f"{self.status} {self.detail}")


def ExceptionHandler(
    *exception_types: type[BaseException],
) -> Callable[[_HandlerT], _HandlerT]:
    """Mark a method as the handler for one or more exception types.

    On a controller it only covers that controller; on a ``@ControllerAdvice``
    class it covers the whole application::

        @ExceptionHandler(UserNotFound)
        async def not_found(self, exc: UserNotFound) -> Response[ErrorOut]: ...
    """
    if not exception_types:
        raise TypeError("@ExceptionHandler requires at least one exception type")
    for exception_type in exception_types:
        if not _is_exception_type(exception_type):
            raise TypeError(f"{exception_type!r} is not an exception type")

    def decorate(handler: _HandlerT) -> _HandlerT:
        existing: tuple[type[BaseException], ...] = getattr(handler, HANDLES_ATTR, ())
        setattr(handler, HANDLES_ATTR, existing + exception_types)
        return handler

    return decorate


class ControllerAdvice:
    """Mark a class as holding exception handlers for the whole application.

    Works bare or called, and the class is picked up by ``app.scan(...)``::

        @ControllerAdvice
        class GlobalErrors:
            @ExceptionHandler(PermissionError)
            async def forbidden(self, exc: PermissionError) -> Response[ErrorOut]: ...
    """

    __slots__ = ()

    @overload
    def __new__[ClassT: type](cls, target: ClassT, /) -> ClassT: ...
    @overload
    def __new__[ClassT: type](cls, target: None = None, /) -> Callable[[ClassT], ClassT]: ...

    def __new__(cls, target: Any = None, /) -> Any:
        if target is None:
            return _mark_advice  # used as @ControllerAdvice()
        return _mark_advice(target)


def _mark_advice[ClassT: type](target: ClassT) -> ClassT:
    require_class(target, "ControllerAdvice")
    setattr(target, ADVICE_ATTR, True)
    return target


def _is_exception_type(value: object) -> bool:
    return isinstance(value, type) and issubclass(value, BaseException)
