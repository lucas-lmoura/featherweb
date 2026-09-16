"""The ``featherweb`` command line, also reachable as ``python -m featherweb``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from . import __version__

__all__ = ["main"]

_LOG_LEVELS = ("critical", "error", "warning", "info", "debug")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="featherweb",
        description="Serve a featherweb application.",
    )
    parser.add_argument("--version", action="version", version=f"featherweb {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="serve an ASGI application")
    run.add_argument("target", help="what to serve, as module:attribute (e.g. myapp.app:app)")
    run.add_argument("--host", default="127.0.0.1", help="interface to bind (default: %(default)s)")
    run.add_argument("--port", type=int, default=8000, help="port to bind (default: %(default)s)")
    run.add_argument(
        "--log-level",
        default="info",
        choices=_LOG_LEVELS,
        help="verbosity of the server log (default: %(default)s)",
    )
    return parser


def load(target: str) -> Any:
    """Import ``module:attribute`` and return the attribute.

    Without a colon the attribute defaults to ``app``. The current directory is
    importable, so ``featherweb run myapp.app:app`` works from a project root.
    """
    import importlib

    module_name, separator, attribute = target.partition(":")
    if not separator:
        attribute = "app"
    if not module_name:
        raise ValueError(f"{target!r} is not a valid target; use module:attribute")
    if "" not in sys.path and sys.path[:1] != [""]:
        sys.path.insert(0, "")
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attribute)
    except AttributeError:
        raise AttributeError(f"{module_name!r} has no attribute {attribute!r}") from None


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    import logging

    arguments = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(arguments.log_level).upper()),
        format="%(levelname)s: %(message)s",
    )
    try:
        app = load(str(arguments.target))
    except (ImportError, AttributeError, ValueError) as exc:
        print(f"featherweb: {exc}", file=sys.stderr)
        return 2

    from .server.runner import run

    run(app, host=str(arguments.host), port=int(arguments.port))
    return 0
