"""The application: an ASGI callable that routes requests to controllers.

Everything expensive happens while controllers are being registered — paths are
joined, parameter binders compiled, exception handlers indexed. Serving a
request is a route lookup, a dict of arguments and a call.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from types import ModuleType
from typing import Any, Final, cast

from ._types import Message, Receive, Scope, Send
from .controllers import PREFIX_ATTR, ROUTES_ATTR, RouteMark
from .exceptions import ADVICE_ATTR, HANDLES_ATTR, HTTPError
from .middleware import ORDER_ATTR, MiddlewareCallable, Next, build_chain
from .params import Binder, compile_binder
from .request import DEFAULT_MAX_BODY_SIZE, ClientDisconnected, Request
from .response import Response
from .routing import Router, normalize_path, path_param_names
from .serialization import Serializer, compile_serializer, resolve_return_hint, unwrap_response

__all__ = ["App"]

logger: Final = logging.getLogger("featherweb")

type Hook = Callable[[], Any]


class _ErrorHandler:
    """A bound ``@ExceptionHandler`` method and how to call it."""

    __slots__ = ("binder", "handler", "name", "serializer")

    def __init__(
        self,
        handler: Callable[..., Awaitable[Any]],
        binder: Binder,
        name: str,
        serializer: Serializer | None,
    ) -> None:
        self.handler = handler
        self.binder = binder
        self.name = name
        self.serializer = serializer


class _Endpoint:
    """A bound controller method, ready to be called."""

    __slots__ = ("binder", "errors", "handler", "name", "serializer", "status")

    def __init__(
        self,
        handler: Callable[..., Awaitable[Any]],
        binder: Binder,
        status: int,
        name: str,
        errors: Mapping[type[BaseException], _ErrorHandler],
        serializer: Serializer | None,
    ) -> None:
        self.handler = handler
        self.binder = binder
        self.status = status
        self.name = name
        #: Exception handlers declared on the owning controller.
        self.errors = errors
        #: Compiled from the return annotation; ``None`` means "send it as it is".
        self.serializer = serializer


class App:
    """An ASGI application built from controllers.

    It runs on the bundled server through :meth:`run`, and on any other ASGI
    server — uvicorn, hypercorn, granian — because it is just an ASGI callable.
    """

    def __init__(
        self,
        *,
        controllers: Iterable[Any] = (),
        middlewares: Iterable[Any] = (),
        on_startup: Iterable[Hook] = (),
        on_shutdown: Iterable[Hook] = (),
        debug: bool = False,
        max_body_size: int = DEFAULT_MAX_BODY_SIZE,
    ) -> None:
        self.debug = debug
        self.max_body_size = max_body_size
        self._router: Router[_Endpoint] = Router()
        self._middlewares: list[MiddlewareCallable] = []
        self._errors: dict[type[BaseException], _ErrorHandler] = {}
        self._startup_hooks: list[Hook] = list(on_startup)
        self._shutdown_hooks: list[Hook] = list(on_shutdown)
        self._chain: Next | None = None
        self.register(*controllers, *middlewares)

    # -- registration ---------------------------------------------------

    def register(self, *targets: Any) -> None:
        """Register controllers, middlewares and ``@ControllerAdvice`` classes.

        Classes are instantiated once, without arguments; instances are taken
        as they are, which is how a configured middleware gets in.
        """
        for target in targets:
            self._register(target)

    def scan(self, package: str) -> None:
        """Import ``package`` recursively and register everything marked in it."""
        import pkgutil

        root = self._import(package, package)
        modules = [root]
        path = getattr(root, "__path__", None)
        if path is not None:
            modules.extend(
                self._import(info.name, package)
                for info in pkgutil.walk_packages(path, prefix=f"{package}.")
            )
        for module in modules:
            for member in list(vars(module).values()):
                if (
                    isinstance(member, type)
                    and member.__module__ == module.__name__
                    and _is_marked(member)
                ):
                    self.register(member)

    @staticmethod
    def _import(name: str, package: str) -> ModuleType:
        import importlib

        try:
            return importlib.import_module(name)
        except ImportError as exc:
            raise ImportError(f"scanning {package!r}: cannot import {name!r} ({exc})") from exc

    def _register(self, target: Any) -> None:
        cls: type = target if isinstance(target, type) else type(target)
        is_controller = hasattr(cls, PREFIX_ATTR)
        is_advice = bool(getattr(cls, ADVICE_ATTR, False))
        is_middleware = hasattr(cls, ORDER_ATTR)
        if not (is_controller or is_advice or is_middleware):
            raise TypeError(
                f"{cls.__qualname__} is not registrable: mark it with @Route, "
                f"@Middleware or @ControllerAdvice"
            )
        instance = cls() if isinstance(target, type) else target
        if is_middleware:
            if not callable(instance):
                raise TypeError(f"middleware {cls.__qualname__} is not callable")
            self._middlewares.append(cast(MiddlewareCallable, instance))
            self._chain = None  # rebuilt on the next request
        if is_advice:
            self._collect_errors(instance, self._errors)
        if is_controller:
            self._add_controller(instance)

    def _add_controller(self, controller: object) -> None:
        cls = type(controller)
        prefix = str(getattr(cls, PREFIX_ATTR, ""))
        errors: dict[type[BaseException], _ErrorHandler] = {}
        self._collect_errors(controller, errors)
        for name, member in _members(cls):
            marks: list[RouteMark] = list(getattr(member, ROUTES_ATTR, ()))
            if not marks:
                continue
            bound: Callable[..., Awaitable[Any]] = getattr(controller, name)
            where = f"{cls.__qualname__}.{name}"
            serializer = _serializer_for(bound)
            for mark in marks:
                path = _join(prefix, mark.path)
                binder = compile_binder(bound, path_params=path_param_names(path), where=where)
                endpoint = _Endpoint(bound, binder, mark.status, where, errors, serializer)
                self._router.add(mark.method, path, endpoint)

    def _collect_errors(
        self, instance: object, into: dict[type[BaseException], _ErrorHandler]
    ) -> None:
        cls = type(instance)
        for name, member in _members(cls):
            handles: tuple[type[BaseException], ...] = getattr(member, HANDLES_ATTR, ())
            if not handles:
                continue
            bound: Callable[..., Awaitable[Any]] = getattr(instance, name)
            where = f"{cls.__qualname__}.{name}"
            binder = compile_binder(bound, exception_type=handles[0], where=where)
            handler = _ErrorHandler(bound, binder, where, _serializer_for(bound))
            for exception_type in handles:
                if exception_type in into:
                    raise TypeError(
                        f"{exception_type.__name__} already has a handler "
                        f"({into[exception_type].name}); {where} would shadow it"
                    )
                into[exception_type] = handler

    # -- lifespan hooks -------------------------------------------------

    def on_startup(self, hook: Hook) -> Hook:
        """Run ``hook`` when the server starts; may be sync or async."""
        self._startup_hooks.append(hook)
        return hook

    def on_shutdown(self, hook: Hook) -> Hook:
        """Run ``hook`` when the server stops; may be sync or async."""
        self._shutdown_hooks.append(hook)
        return hook

    # -- ASGI -----------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope_type = scope["type"]
        if scope_type == "http":
            await self._handle_http(scope, receive, send)
        elif scope_type == "lifespan":
            await self._handle_lifespan(receive, send)
        elif scope_type == "websocket":
            await send({"type": "websocket.close", "code": 1001})
        else:
            raise RuntimeError(f"unsupported ASGI scope type {scope_type!r}")

    async def _handle_http(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive, max_body_size=self.max_body_size)
        chain = self._chain
        if chain is None:
            chain = self._chain = build_chain(self._middlewares, self._dispatch)
        try:
            response = await chain(request)
        except Exception as exc:  # raised by a middleware, or by our own dispatch
            response = await self._error_response(exc, request, None)
        body = response.render()
        await send(
            {
                "type": "http.response.start",
                "status": response.status,
                "headers": response.raw_headers(),
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _handle_lifespan(self, receive: Receive, send: Send) -> None:
        while True:
            message: Message = await receive()
            message_type = message["type"]
            if message_type == "lifespan.startup":
                failure = await _run_hooks(self._startup_hooks)
                if failure is not None:
                    await send({"type": "lifespan.startup.failed", "message": failure})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message_type == "lifespan.shutdown":
                failure = await _run_hooks(self._shutdown_hooks)
                if failure is not None:
                    await send({"type": "lifespan.shutdown.failed", "message": failure})
                    return
                await send({"type": "lifespan.shutdown.complete"})
                return

    # -- request handling -----------------------------------------------

    async def _dispatch(self, request: Request) -> Response[Any]:
        endpoint: _Endpoint | None = None
        try:
            endpoint, params = self._resolve(request)
            request.path_params = params
            arguments = await endpoint.binder.build(request, params)
            result = await endpoint.handler(**arguments)
            return _make_response(result, endpoint.status, endpoint.serializer)
        except Exception as exc:
            errors = endpoint.errors if endpoint is not None else None
            return await self._error_response(exc, request, errors)

    def _resolve(self, request: Request) -> tuple[_Endpoint, Mapping[str, Any]]:
        resolved = self._router.resolve(_route_path(request))
        if resolved is None:
            raise HTTPError(404, "not found")
        handlers, params = resolved
        method = request.method
        endpoint = handlers.get(method)
        if endpoint is None and method == "HEAD":
            endpoint = handlers.get("GET")  # a GET route answers HEAD for free
        if endpoint is None:
            allowed = set(handlers)
            if "GET" in allowed:
                allowed.add("HEAD")
            raise HTTPError(
                405,
                f"method {method} is not allowed here",
                headers={"allow": ", ".join(sorted(allowed))},
            )
        return endpoint, params

    async def _error_response(
        self,
        exc: Exception,
        request: Request,
        local: Mapping[type[BaseException], _ErrorHandler] | None,
    ) -> Response[Any]:
        handler = self._find_handler(type(exc), local)
        if handler is not None:
            try:
                arguments = await handler.binder.build(request, {}, exc)
                result = await handler.handler(**arguments)
            except Exception:
                logger.exception("exception handler %s failed", handler.name)
            else:
                status = exc.status if isinstance(exc, HTTPError) else 500
                return _make_response(result, status, handler.serializer)
        return self._fallback(exc, request)

    def _find_handler(
        self,
        exception_type: type[BaseException],
        local: Mapping[type[BaseException], _ErrorHandler] | None,
    ) -> _ErrorHandler | None:
        """Local handlers win over global ones; within each, the closest match wins."""
        for mapping in (local, self._errors):
            if not mapping:
                continue
            for klass in exception_type.__mro__:
                handler = mapping.get(klass)
                if handler is not None:
                    return handler
        return None

    def _fallback(self, exc: Exception, request: Request) -> Response[Any]:
        if isinstance(exc, HTTPError):
            return Response({"detail": exc.detail}, status=exc.status, headers=exc.headers)
        if isinstance(exc, ClientDisconnected):
            logger.debug("client went away during %s %s", request.method, request.path)
            return Response(None, status=400)
        logger.error(
            "unhandled exception while serving %s %s",
            request.method,
            request.path,
            exc_info=exc,
        )
        detail = _traceback(exc) if self.debug else "internal server error"
        return Response({"detail": detail}, status=500)

    # -- running --------------------------------------------------------

    def run(self, **kwargs: Any) -> None:
        """Serve this application on the bundled server until interrupted."""
        from .server.runner import run

        run(self, **kwargs)

    def __repr__(self) -> str:
        return f"<App {len(self._middlewares)} middlewares>"


def _serializer_for(handler: Callable[..., Any]) -> Serializer | None:
    """Compile the return annotation, looking through ``Response[T]``."""
    hint = resolve_return_hint(handler)
    inner = unwrap_response(hint)
    return compile_serializer(inner if inner is not None else hint)


def _make_response(result: Any, status: int, serializer: Serializer | None) -> Response[Any]:
    """Turn what the handler returned into a response, the way its annotation says."""
    if isinstance(result, Response):
        response = cast(Response[Any], result)
        if not response.has_explicit_status:
            response.status = status  # the one the verb (or the error) declared
        if serializer is not None and response.content is not None:
            response.content = serializer(response.content)
        return response
    if result is None:
        return Response(None, status=204 if status == 200 else status)
    if serializer is not None:
        result = serializer(result)
    return Response(result, status=status)


def _route_path(request: Request) -> str:
    path = request.path
    root = str(request.scope.get("root_path") or "")
    if root and path.startswith(root):
        return path[len(root) :] or "/"
    return path


def _join(prefix: str, path: str) -> str:
    if not path:
        return normalize_path(prefix)
    return normalize_path(f"{prefix.rstrip('/')}/{path.lstrip('/')}")


def _members(cls: type) -> Iterator[tuple[str, Any]]:
    """Every attribute defined on ``cls`` or inherited, without touching properties."""
    seen: set[str] = set()
    for klass in cls.__mro__:
        for name, member in vars(klass).items():
            if name not in seen:
                seen.add(name)
                yield name, member


def _is_marked(cls: type) -> bool:
    return (
        hasattr(cls, PREFIX_ATTR)
        or hasattr(cls, ORDER_ATTR)
        or bool(getattr(cls, ADVICE_ATTR, False))
    )


async def _run_hooks(hooks: Iterable[Hook]) -> str | None:
    """Run lifespan hooks; the message of the first failure stops the rest."""
    for hook in hooks:
        try:
            result = hook()
            if isinstance(result, Awaitable):
                await result
        except Exception as exc:
            logger.exception("lifespan hook %r failed", getattr(hook, "__name__", hook))
            return f"{type(exc).__name__}: {exc}"
    return None


def _traceback(exc: BaseException) -> str:
    import traceback

    return "".join(traceback.format_exception(exc))
