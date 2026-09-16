"""Running several worker processes behind one port.

Each worker binds the *same* port with ``SO_REUSEPORT``, so the kernel spreads
incoming connections across them. There is no parent socket being passed down
and no accept loop in the supervisor: every worker is an ordinary server that
happens to share a port, which means one dying does not take the others with
it.

``SO_REUSEPORT`` does not exist on Windows, and the option Windows does have
(``SO_REUSEADDR``) lets a second process *steal* the port rather than share it,
which would be worse than useless here. So on Windows this falls back to a
single worker and says so, which is what section 6 of the plan calls for.

Workers are addressed by import string rather than by object: the application
is built again inside each process, because a live ``App`` — with its compiled
binders and bound methods — is not something that survives being pickled.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
from typing import Any, Final

from .._logging import get_logger

__all__ = ["Supervisor", "supports_reuse_port"]

logger: Final = get_logger("featherweb.server")

#: How long a worker is given to stop before it is killed.
_TERMINATE_TIMEOUT: Final = 10.0


def supports_reuse_port() -> bool:
    """Whether this platform can share a listening port between processes."""
    return hasattr(socket, "SO_REUSEPORT") and sys.platform != "win32"


class Supervisor:
    """Starts ``workers`` processes and keeps them running until asked to stop."""

    __slots__ = ("_context", "_options", "_processes", "_stopping", "target", "workers")

    def __init__(self, target: str, *, workers: int, **options: Any) -> None:
        import multiprocessing

        if workers < 1:
            raise ValueError("workers must be at least 1")
        self.target = target
        self.workers = workers
        self._options = options
        # "spawn" everywhere: fork inherits whatever the parent had open, and
        # what the parent has here is an import of the user's application.
        self._context = multiprocessing.get_context("spawn")
        self._processes: list[Any] = []
        self._stopping = False

    def run(self) -> None:
        """Start the workers and wait; returns once they have all stopped."""
        self._install_signal_handlers()
        for _ in range(self.workers):
            self._start_one()
        logger.info(
            "featherweb running %d workers on http://%s:%s",
            self.workers,
            self._options.get("host", "127.0.0.1"),
            self._options.get("port", 8000),
        )
        try:
            self._supervise()
        finally:
            self.stop()

    def _start_one(self) -> Any:
        process = self._context.Process(
            target=_serve_one,
            args=(self.target, self._options),
            daemon=False,
        )
        process.start()
        self._processes.append(process)
        return process

    def _supervise(self) -> None:
        """Wait on the workers, replacing any that dies unexpectedly."""
        while not self._stopping:
            for index, process in enumerate(list(self._processes)):
                process.join(timeout=0.5)
                if self._stopping or process.is_alive():
                    continue
                logger.warning(
                    "worker %s exited (%s); starting another", process.pid, process.exitcode
                )
                self._processes[index] = self._context.Process(
                    target=_serve_one, args=(self.target, self._options), daemon=False
                )
                self._processes[index].start()

    def stop(self) -> None:
        """Ask every worker to stop, then insist if one will not."""
        self._stopping = True
        for process in self._processes:
            if process.is_alive():
                process.terminate()  # SIGTERM, which the worker shuts down on
        for process in self._processes:
            process.join(timeout=_TERMINATE_TIMEOUT)
            if process.is_alive():
                logger.warning("worker %s did not stop in time; killing it", process.pid)
                process.kill()
                process.join()
        self._processes.clear()

    def _install_signal_handlers(self) -> None:
        def handle(signum: int, frame: object) -> None:
            del signum, frame
            self._stopping = True

        for name in (signal.SIGINT, signal.SIGTERM):
            with _ignore_errors():
                signal.signal(name, handle)

    def __repr__(self) -> str:
        return f"Supervisor({self.target!r}, workers={self.workers})"


def _serve_one(target: str, options: dict[str, Any]) -> None:
    """The entry point of a worker process: build the app and serve it."""
    from ..cli import load
    from .runner import run

    # Every worker binds the shared port itself; this is what makes them share.
    options = {**options, "reuse_port": True}
    try:
        application = load(target)
    except (ImportError, AttributeError, ValueError) as exc:
        print(f"featherweb worker {os.getpid()}: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    run(application, **options)


class _ignore_errors:
    """Signal handling is not available on every thread or platform."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, kind: Any, value: Any, traceback: Any) -> bool:
        return kind is not None and issubclass(kind, (ValueError, OSError, AttributeError))
