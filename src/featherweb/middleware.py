"""Middlewares: classes that wrap the whole request.

A middleware receives the request and the rest of the chain, and returns a
response. The lowest ``order`` runs first, so it is the outermost layer::

    @Middleware(order=10)
    class Timing:
        async def __call__(self, request: Request, call_next: Next) -> Response[Any]:
            response = await call_next(request)
            ...
            return response
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any, Final, Protocol, TypeVar, overload

from .controllers import require_class
from .request import Request
from .response import Response

__all__ = ["CORS", "GZip", "Middleware", "MiddlewareCallable", "Next"]

#: Attribute where ``@Middleware`` records the ordering.
ORDER_ATTR: Final = "__featherweb_order__"

type Next = Callable[[Request], Awaitable[Response[Any]]]

_ClassT = TypeVar("_ClassT", bound=type)

_DEFAULT_METHODS: Final = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")


class MiddlewareCallable(Protocol):
    """What a middleware instance has to look like."""

    async def __call__(self, request: Request, call_next: Next) -> Response[Any]: ...


class Middleware:
    """Mark a class as a middleware, optionally giving it an order."""

    __slots__ = ()

    @overload
    def __new__[ClassT: type](cls, target: ClassT, /) -> ClassT: ...
    @overload
    def __new__[ClassT: type](
        cls, target: None = None, /, *, order: int = 0
    ) -> Callable[[ClassT], ClassT]: ...

    def __new__(cls, target: Any = None, /, *, order: int = 0) -> Any:
        if target is not None:
            return _set_order(target, order)

        def decorate(decorated: _ClassT) -> _ClassT:
            return _set_order(decorated, order)

        return decorate


def _set_order[ClassT: type](target: ClassT, order: int) -> ClassT:
    require_class(target, "Middleware")
    setattr(target, ORDER_ATTR, order)
    return target


@Middleware(order=-1000)
class CORS:
    """Answer preflight requests and add the cross-origin headers.

    Configured, so it is passed as an instance::

        App(middlewares=[CORS(allow_origins=["https://example.com"])])
    """

    def __init__(
        self,
        *,
        allow_origins: Iterable[str] = ("*",),
        allow_methods: Iterable[str] = _DEFAULT_METHODS,
        allow_headers: Iterable[str] = (),
        allow_credentials: bool = False,
        expose_headers: Iterable[str] = (),
        max_age: int = 600,
    ) -> None:
        self.allow_origins = frozenset(allow_origins)
        self.allow_methods = ", ".join(sorted({method.upper() for method in allow_methods}))
        self.allow_headers = ", ".join(allow_headers)
        self.allow_credentials = allow_credentials
        self.expose_headers = ", ".join(expose_headers)
        self.max_age = max_age
        if allow_credentials and "*" in self.allow_origins:
            raise ValueError("allow_credentials cannot be combined with the '*' origin")

    def _allowed(self, origin: str) -> str | None:
        if "*" in self.allow_origins:
            return "*"
        return origin if origin in self.allow_origins else None

    async def __call__(self, request: Request, call_next: Next) -> Response[Any]:
        origin = request.headers.get("origin")
        if origin is None:
            return await call_next(request)
        allowed = self._allowed(origin)
        if request.method == "OPTIONS" and "access-control-request-method" in request.headers:
            response: Response[Any] = Response(None, status=204)
            if allowed is not None:
                self._decorate(response, allowed)
                response.headers["access-control-allow-methods"] = self.allow_methods
                requested = request.headers.get("access-control-request-headers")
                headers = self.allow_headers or requested
                if headers:
                    response.headers["access-control-allow-headers"] = headers
                response.headers["access-control-max-age"] = str(self.max_age)
            return response
        response = await call_next(request)
        if allowed is not None:
            self._decorate(response, allowed)
            if self.expose_headers:
                response.headers["access-control-expose-headers"] = self.expose_headers
        return response

    def _decorate(self, response: Response[Any], allowed: str) -> None:
        response.headers["access-control-allow-origin"] = allowed
        if self.allow_credentials:
            response.headers["access-control-allow-credentials"] = "true"
        if allowed != "*":
            _add_vary(response, "Origin")


@Middleware(order=-900)
class GZip:
    """Compress responses the client is willing to receive compressed."""

    def __init__(self, *, minimum_size: int = 500, level: int = 6) -> None:
        self.minimum_size = minimum_size
        self.level = level

    async def __call__(self, request: Request, call_next: Next) -> Response[Any]:
        response = await call_next(request)
        accepted = request.headers.get("accept-encoding", "")
        if "gzip" not in accepted.lower():
            return response
        _add_vary(response, "Accept-Encoding")
        if "content-encoding" in response.headers or response.status in (204, 304):
            return response
        body = response.render()
        if len(body) < self.minimum_size:
            return response
        response.set_body(_gzip(body, self.level))
        response.headers["content-encoding"] = "gzip"
        return response


def _gzip(body: bytes, level: int) -> bytes:
    import zlib

    compressor = zlib.compressobj(level, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    return compressor.compress(body) + compressor.flush()


def _add_vary(response: Response[Any], value: str) -> None:
    existing = response.headers.get("vary")
    if not existing:
        response.headers["vary"] = value
    elif value.lower() not in existing.lower():
        response.headers["vary"] = f"{existing}, {value}"


def sort_key(middleware: MiddlewareCallable) -> int:
    """Ordering used by the application: the lowest order is the outermost layer."""
    return int(getattr(type(middleware), ORDER_ATTR, 0))


def build_chain(middlewares: Sequence[MiddlewareCallable], endpoint: Next) -> Next:
    """Wrap ``endpoint`` in the middlewares, lowest order outermost."""
    chain = endpoint
    for middleware in sorted(middlewares, key=sort_key, reverse=True):
        chain = _Layer(middleware, chain)
    return chain


class _Layer:
    """One middleware bound to the rest of the chain."""

    __slots__ = ("_middleware", "_next")

    def __init__(self, middleware: MiddlewareCallable, next_layer: Next) -> None:
        self._middleware = middleware
        self._next = next_layer

    async def __call__(self, request: Request) -> Response[Any]:
        return await self._middleware(request, self._next)
