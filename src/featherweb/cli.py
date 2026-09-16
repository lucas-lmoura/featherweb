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
        "--workers",
        type=int,
        default=1,
        help="worker processes sharing the port (default: %(default)s)",
    )
    run.add_argument("--ssl-certfile", help="PEM certificate; serves HTTPS when given")
    run.add_argument("--ssl-keyfile", help="private key for --ssl-certfile")
    run.add_argument("--ssl-password", help="passphrase for an encrypted --ssl-keyfile")
    run.add_argument(
        "--log-level",
        default="info",
        choices=_LOG_LEVELS,
        help="verbosity of the server log (default: %(default)s)",
    )
    return parser


def ssl_context(arguments: Any) -> Any:
    """Build an SSL context from the command line, or ``None`` for plain HTTP."""
    certfile = getattr(arguments, "ssl_certfile", None)
    if not certfile:
        return None
    import ssl

    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(
        certfile, getattr(arguments, "ssl_keyfile", None), getattr(arguments, "ssl_password", None)
    )
    return context


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
    options: dict[str, Any] = {"host": str(arguments.host), "port": int(arguments.port)}
    try:
        options["ssl_context"] = ssl_context(arguments)
    except (OSError, ValueError) as exc:
        print(f"featherweb: cannot load the TLS certificate ({exc})", file=sys.stderr)
        return 2

    workers = int(getattr(arguments, "workers", 1))
    if workers > 1:
        return _run_workers(str(arguments.target), workers, options)

    try:
        app = load(str(arguments.target))
    except (ImportError, AttributeError, ValueError) as exc:
        print(f"featherweb: {exc}", file=sys.stderr)
        return 2

    from .server.runner import run

    run(app, **options)
    return 0


def _run_workers(target: str, workers: int, options: dict[str, Any]) -> int:
    """Serve from several processes, where the platform allows it."""
    import logging

    from .server.workers import Supervisor, supports_reuse_port

    if options.get("ssl_context") is not None:
        # An ssl.SSLContext cannot be pickled across to a spawned worker.
        print(
            "featherweb: --workers cannot be combined with TLS yet; "
            "terminate TLS in front of the server instead",
            file=sys.stderr,
        )
        return 2
    if not supports_reuse_port():
        logging.getLogger("featherweb.server").warning(
            "this platform cannot share a port between processes; serving with one worker"
        )
        workers = 1
    if workers == 1:
        try:
            app = load(target)
        except (ImportError, AttributeError, ValueError) as exc:
            print(f"featherweb: {exc}", file=sys.stderr)
            return 2
        from .server.runner import run

        run(app, **options)
        return 0
    Supervisor(target, workers=workers, **options).run()
    return 0
