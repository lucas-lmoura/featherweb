"""The applications the benchmarks serve.

The same four routes in featherweb and in Starlette, so the comparison is
between the frameworks rather than between two different things that happen to
answer HTTP. Starlette is optional: if it is not installed, only featherweb is
built and the comparison simply has one column.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

HERE = Path(__file__).parent
#: Served by the "static" route; written on first use so the repo stays small.
SAMPLE = HERE / "_sample.bin"
SAMPLE_SIZE = 64 * 1024

#: The JSON body both frameworks return, so neither gets an easier payload.
PAYLOAD: dict[str, Any] = {
    "id": 42,
    "name": "featherweb",
    "tags": ["async", "stdlib", "typed"],
    "nested": {"a": 1, "b": 2, "c": [1, 2, 3]},
}


def ensure_sample() -> Path:
    if not SAMPLE.exists():
        SAMPLE.write_bytes(b"x" * SAMPLE_SIZE)
    return SAMPLE


def featherweb_app() -> Any:
    from featherweb import App, FileResponse, Get, Route

    sample = ensure_sample()

    @Route
    class Bench:
        @Get("/plaintext")
        async def plaintext(self) -> str:
            return "Hello, World!"

        @Get("/json")
        async def json(self) -> dict[str, Any]:
            return PAYLOAD

        @Get("/user/{user_id:int}")
        async def user(self, user_id: int) -> dict[str, int]:
            return {"id": user_id}

        @Get("/file")
        async def file(self) -> FileResponse:
            return FileResponse(sample, media_type="application/octet-stream")

    return App(controllers=[Bench])


def starlette_app() -> Any:
    """The same routes under Starlette, or ``None`` when it is not installed."""
    try:
        from starlette.applications import Starlette
        from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
        from starlette.routing import Route as StarletteRoute
    except ImportError:
        return None

    sample = ensure_sample()

    async def plaintext(request: Any) -> Any:
        del request
        return PlainTextResponse("Hello, World!")

    async def json_route(request: Any) -> Any:
        del request
        return JSONResponse(PAYLOAD)

    async def user(request: Any) -> Any:
        return JSONResponse({"id": int(request.path_params["user_id"])})

    async def file(request: Any) -> Any:
        del request
        return FileResponse(sample, media_type="application/octet-stream")

    return Starlette(
        routes=[
            StarletteRoute("/plaintext", plaintext),
            StarletteRoute("/json", json_route),
            StarletteRoute("/user/{user_id:int}", user),
            StarletteRoute("/file", file),
        ]
    )


#: What ``featherweb run`` and ``uvicorn`` load when pointed at this module.
app = featherweb_app()
#: The comparison, when Starlette is installed; ``None`` otherwise.
starlette = starlette_app()
