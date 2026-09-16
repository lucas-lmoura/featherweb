"""A logger that costs nothing until something is logged.

``logging`` is one of the more expensive modules in the standard library to
import — it pulls ``traceback``, ``linecache`` and ``tokenize`` in behind it,
and on a normal machine that is about as much time as the rest of featherweb
put together. An application that only declares routes never writes a line, so
the cost is deferred to the first one, which in practice is when the server
starts rather than when the package is imported.

This module deliberately imports nothing at the top level: it sits on the
package's import path, so anything it pulled in would defeat the point.
"""

from __future__ import annotations

from typing import Any

__all__ = ["get_logger"]


class LazyLogger:
    """Stands in for a ``logging.Logger`` until one is needed."""

    __slots__ = ("_logger", "_name")

    def __init__(self, name: str) -> None:
        self._name = name
        self._logger: Any = None

    def __getattr__(self, attribute: str) -> Any:
        # Reached only for names that are not slots, which is every logging
        # method, so the real logger is built on the first call and kept.
        import logging

        if self._logger is None:
            self._logger = logging.getLogger(self._name)
        return getattr(self._logger, attribute)

    def __repr__(self) -> str:
        state = "unused" if self._logger is None else "active"
        return f"<LazyLogger {self._name!r} ({state})>"


def get_logger(name: str) -> Any:
    """``logging.getLogger(name)``, but without importing ``logging`` yet."""
    return LazyLogger(name)
