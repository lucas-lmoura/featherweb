"""Serving a directory of files.

Mounted under a prefix, so it never touches the router's own routes::

    app.mount("/static", StaticFiles(directory="assets", max_age=3600))

The path that arrives is attacker-controlled, so it is treated as such: the
segments are checked one by one before anything touches the filesystem, and the
resolved path has to still be inside the directory afterwards. Everything about
caching — ``ETag``, ``Last-Modified``, 304 and ``Range`` — comes from
:class:`~featherweb.response.FileResponse`.

This module is imported only when an application asks for it, so it stays off
the package's import path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .exceptions import HTTPError
from .request import Request
from .response import FileResponse, Response

__all__ = ["StaticFiles"]

#: Never valid inside a single path segment of a URL we are about to open.
_FORBIDDEN = ("\\", "\0", ":")


class StaticFiles:
    """Answer requests for the files under one directory."""

    __slots__ = ("cache_control", "directory", "index")

    def __init__(
        self,
        directory: str | Path,
        *,
        index: str | None = None,
        max_age: int | None = None,
    ) -> None:
        #: Resolved once, at startup: every request is checked against it.
        self.directory = Path(directory).resolve()
        #: Served for a request that names the directory itself, e.g. "index.html".
        self.index = index
        self.cache_control = None if max_age is None else f"public, max-age={max_age}"

    async def __call__(self, request: Request, relative: str) -> Response[Any]:
        """Serve ``relative``, the part of the path below the mount point."""
        if request.method not in ("GET", "HEAD"):
            raise HTTPError(
                405, f"method {request.method} is not allowed here", headers={"allow": "GET, HEAD"}
            )
        path = self._locate(relative)
        response = FileResponse(path, request=request)
        if self.cache_control is not None:
            response.headers.setdefault("cache-control", self.cache_control)
        return response

    def _locate(self, relative: str) -> Path:
        """The file ``relative`` names, or a 404 if it names anything else.

        A traversal is refused rather than clamped: there is no useful file at
        the other end of one, so there is nothing to be gained by guessing.
        """
        candidate = self.directory
        for segment in relative.split("/"):
            if not segment or segment == ".":
                continue
            if segment == ".." or any(char in segment for char in _FORBIDDEN):
                raise HTTPError(404, "not found")
            candidate = candidate / segment
        try:
            # resolve() also collapses any symlink, so one pointing out of the
            # directory fails the containment check below like a ".." would.
            resolved = candidate.resolve()
        except OSError:
            raise HTTPError(404, "not found") from None
        if resolved != self.directory and not resolved.is_relative_to(self.directory):
            raise HTTPError(404, "not found")
        if self.index is not None and resolved.is_dir():
            resolved = resolved / self.index
        return resolved

    def __repr__(self) -> str:
        return f"StaticFiles({str(self.directory)!r})"
