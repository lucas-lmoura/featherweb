"""Running the server: event loop, signals and graceful shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket
import ssl
import threading
from collections.abc import Generator
from typing import Any, Final, cast

from .._compat import loop_factory
from .._types import ASGIApp
from .protocol import HttpProtocol, ServerConfig, ServerState

__all__ = ["Server", "run", "serve"]

logger: Final = logging.getLogger("featherweb.server")

_SHUTDOWN_SIGNALS: Final = (signal.SIGINT, signal.SIGTERM)


class Server:
    """An HTTP server bound to one address, serving a single ASGI application."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        host: str = "127.0.0.1",
        port: int = 8000,
        config: ServerConfig | None = None,
        ssl_context: ssl.SSLContext | None = None,
        backlog: int = 2048,
        reuse_port: bool = False,
        shutdown_timeout: float = 10.0,
    ) -> None:
        self.app = app
        self.host = host
        self.port = port
        self.config = config if config is not None else ServerConfig()
        self.ssl_context = ssl_context
        self.backlog = backlog
        self.reuse_port = reuse_port
        self.shutdown_timeout = shutdown_timeout
        self.state = ServerState()
        self._server: asyncio.Server | None = None
        self._bound_port: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown_event: asyncio.Event | None = None
        self._shutdown_done = False

    # -- lifecycle ------------------------------------------------------

    async def startup(self) -> None:
        """Bind the socket and start accepting connections."""
        if self._server is not None:
            raise RuntimeError("the server is already running")
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._shutdown_event = asyncio.Event()
        self._server = await loop.create_server(
            lambda: HttpProtocol(self.app, self.config, state=self.state),
            host=self.host,
            port=self.port,
            ssl=self.ssl_context,
            backlog=self.backlog,
            reuse_port=self.reuse_port or None,
            start_serving=True,
        )
        self._bound_port = self._read_bound_port()
        logger.info("featherweb listening on %s", self.url)

    async def serve(self) -> None:
        """Serve until a shutdown signal arrives, then shut down gracefully."""
        with self._signal_handlers():
            await self.startup()
            event = self._shutdown_event
            assert event is not None
            with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
                await event.wait()
        await self.shutdown()

    def request_shutdown(self) -> None:
        """Ask the server to stop; safe to call from a signal handler."""
        self.state.should_exit = True
        event = self._shutdown_event
        loop = self._loop
        if event is not None and loop is not None and not event.is_set():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(event.set)

    async def shutdown(self) -> None:
        """Stop accepting, let running requests finish, then close everything."""
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self.state.should_exit = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for connection in list(self.state.connections):
            connection.shutdown()
        if not self.state.connections:
            self.state.drained.set()

        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(self.shutdown_timeout):
                await self.state.drained.wait()

        for connection in list(self.state.connections):
            logger.warning("closing connection that outlived the shutdown timeout")
            connection.abort()
        pending = list(self.state.tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        logger.info("featherweb stopped")

    # -- introspection --------------------------------------------------

    @property
    def sockets(self) -> tuple[socket.socket, ...]:
        server = self._server
        return () if server is None else tuple(server.sockets)

    @property
    def bound_port(self) -> int:
        """The port actually bound, which matters when ``port=0`` was requested.

        It keeps its value after shutdown, so it can still be logged.
        """
        return self.port if self._bound_port is None else self._bound_port

    def _read_bound_port(self) -> int | None:
        for sock in self.sockets:
            address = sock.getsockname()
            if isinstance(address, tuple):
                parts = cast(tuple[Any, ...], address)
                if len(parts) >= 2:
                    return int(parts[1])
        return None

    @property
    def url(self) -> str:
        scheme = "https" if self.ssl_context is not None else "http"
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{scheme}://{host}:{self.bound_port}"

    # -- signals --------------------------------------------------------

    @contextlib.contextmanager
    def _signal_handlers(self) -> Generator[None]:
        if threading.current_thread() is not threading.main_thread():
            yield  # signals can only be handled in the main thread
            return
        loop = asyncio.get_running_loop()
        installed: list[tuple[int, Any]] = []
        for sig in _SHUTDOWN_SIGNALS:
            try:
                loop.add_signal_handler(sig, self.request_shutdown)
                installed.append((sig, None))
            except NotImplementedError:  # Windows
                previous = signal.signal(sig, self._handle_signal)
                installed.append((sig, previous))
        try:
            yield
        finally:
            for sig, previous in installed:
                if previous is None:
                    with contextlib.suppress(NotImplementedError, RuntimeError):
                        loop.remove_signal_handler(sig)
                else:
                    signal.signal(sig, previous)

    def _handle_signal(self, signum: int, frame: object) -> None:
        del signum, frame
        self.request_shutdown()


async def serve(app: ASGIApp, **kwargs: Any) -> None:
    """Run ``app`` in the current event loop until shutdown."""
    await Server(app, **kwargs).serve()


def run(app: ASGIApp, **kwargs: Any) -> None:
    """Run ``app`` in a fresh event loop; this is what ``App.run()`` calls."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    server = Server(app, **kwargs)
    with asyncio.Runner(loop_factory=loop_factory()) as runner:
        try:
            runner.run(server.serve())
        except KeyboardInterrupt:
            runner.run(server.shutdown())
