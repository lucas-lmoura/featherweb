"""featherweb: lightweight async web framework with zero required dependencies."""

from typing import TYPE_CHECKING, Any

from .app import App
from .controllers import Delete, Get, Head, Options, Patch, Post, Put, Route, Ws
from .exceptions import (
    ControllerAdvice,
    ExceptionHandler,
    FieldError,
    HTTPError,
    ValidationError,
)
from .middleware import CORS, GZip, Middleware, Next
from .params import Body, Cookie, File, Form, Header, Query
from .request import FormData, Headers, QueryParams, Request
from .response import FileResponse, RedirectResponse, Response, StreamingResponse

if TYPE_CHECKING:  # the names __getattr__ hands out, for the type checker
    from .multipart import UploadFile
    from .staticfiles import StaticFiles
    from .websocket import WebSocket, WebSocketDisconnect

__version__ = "0.1.0"


def __getattr__(name: str) -> Any:
    """Hand out the on-demand modules without importing them up front.

    ``StaticFiles`` and ``UploadFile`` live in modules most applications never
    touch, so they are fetched on first use rather than at ``import featherweb``.
    """
    if name == "StaticFiles":
        from .staticfiles import StaticFiles

        return StaticFiles
    if name == "UploadFile":
        from .multipart import UploadFile

        return UploadFile
    if name in ("WebSocket", "WebSocketDisconnect"):
        from . import websocket

        return getattr(websocket, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CORS",
    "App",
    "Body",
    "ControllerAdvice",
    "Cookie",
    "Delete",
    "ExceptionHandler",
    "FieldError",
    "File",
    "FileResponse",
    "Form",
    "FormData",
    "GZip",
    "Get",
    "HTTPError",
    "Head",
    "Header",
    "Headers",
    "Middleware",
    "Next",
    "Options",
    "Patch",
    "Post",
    "Put",
    "Query",
    "QueryParams",
    "RedirectResponse",
    "Request",
    "Response",
    "Route",
    "StaticFiles",
    "StreamingResponse",
    "UploadFile",
    "ValidationError",
    "WebSocket",
    "WebSocketDisconnect",
    "Ws",
    "__version__",
]
