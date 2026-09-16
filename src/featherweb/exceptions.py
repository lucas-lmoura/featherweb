"""Errors, and the decorators that turn them into responses."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final, TypeVar, overload

from .controllers import require_class

__all__ = ["ControllerAdvice", "ExceptionHandler", "FieldError", "HTTPError", "ValidationError"]

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
    #: Rendered as the ``detail`` member of the response; usually a string,
    #: but a structured value when the error has more to say.
    detail: Any = "internal server error"

    def __init__(
        self,
        status: int | None = None,
        detail: Any = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if status is not None:
            self.status = status
        if detail is not None:
            self.detail = detail
        self.headers: dict[str, str] = dict(headers) if headers else {}
        super().__init__(f"{self.status} {self.detail}")


class FieldError:
    """One thing that is wrong with the request, and where it is."""

    __slots__ = ("field", "location", "message")

    def __init__(self, location: str, field: str, message: str) -> None:
        #: Where the value was looked for: path, query, header, cookie, body or form.
        self.location = location
        #: The field itself, dotted for anything nested in the body.
        self.field = field
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"location": self.location, "field": self.field, "message": self.message}

    def __repr__(self) -> str:
        return f"FieldError({self.location}:{self.field}: {self.message})"


class ValidationError(HTTPError):
    """The request did not match what the handler declared.

    It is an :class:`HTTPError`, so it renders as ``{"detail": [...]}`` with
    status 422 and can be caught by an ``@ExceptionHandler`` like any other.
    """

    status = 422

    def __init__(self, errors: list[FieldError]) -> None:
        self.errors = errors
        super().__init__(detail=[error.as_dict() for error in errors])


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
