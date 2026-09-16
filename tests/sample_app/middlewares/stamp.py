"""A middleware that scan() has to find."""

from typing import Any

from featherweb import Middleware, Next, Request, Response


@Middleware(order=5)
class Stamp:
    async def __call__(self, request: Request, call_next: Next) -> Response[Any]:
        response = await call_next(request)
        response.headers["x-stamp"] = "scanned"
        return response
