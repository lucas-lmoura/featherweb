"""Serving from more than one process.

The interesting half of this only runs where ``SO_REUSEPORT`` exists, so on
Windows most of it skips and the fallback is what gets checked instead. That is
the intended behaviour there, not a gap in the tests.
"""

from __future__ import annotations

import socket
import sys
import time
import urllib.error
import urllib.request

import pytest

from featherweb.cli import build_parser, main
from featherweb.server.workers import Supervisor, supports_reuse_port

needs_reuse_port = pytest.mark.skipif(
    not supports_reuse_port(), reason="this platform cannot share a port between processes"
)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def wait_for(url: str, *, timeout: float = 20.0) -> str:
    """The body once the server answers, or a failure once patience runs out."""
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                return response.read().decode()
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last = exc
            time.sleep(0.2)
    raise AssertionError(f"{url} never answered: {last}")


# -- configuration ----------------------------------------------------------


def test_the_cli_accepts_workers_and_tls() -> None:
    arguments = build_parser().parse_args(
        ["run", "app:app", "--workers", "3", "--ssl-certfile", "cert.pem"]
    )
    assert arguments.workers == 3
    assert arguments.ssl_certfile == "cert.pem"


def test_one_worker_is_the_default() -> None:
    assert build_parser().parse_args(["run", "app:app"]).workers == 1


def test_a_supervisor_needs_at_least_one_worker() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        Supervisor("app:app", workers=0)


def test_the_supervisor_says_what_it_runs() -> None:
    assert "workers=2" in repr(Supervisor("app:app", workers=2))


def test_reuse_port_is_reported_honestly() -> None:
    """Windows has no SO_REUSEPORT, and SO_REUSEADDR there steals rather than shares."""
    expected = hasattr(socket, "SO_REUSEPORT") and sys.platform != "win32"
    assert supports_reuse_port() is expected


def test_a_certificate_that_will_not_load_is_reported(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["run", "benchmarks.apps:app", "--ssl-certfile", "missing.pem"])
    assert code == 2
    assert "certificate" in capsys.readouterr().err.lower()


def test_workers_with_tls_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    """An ssl.SSLContext cannot be pickled across to a spawned worker."""
    from featherweb.cli import _run_workers  # pyright: ignore[reportPrivateUsage]

    code = _run_workers("benchmarks.apps:app", 2, {"ssl_context": object()})
    assert code == 2
    assert "tls" in capsys.readouterr().err.lower()


@pytest.mark.skipif(supports_reuse_port(), reason="only where ports cannot be shared")
def test_without_reuse_port_it_falls_back_to_one_worker() -> None:
    """The fallback has to serve, not refuse: one worker is still a server."""
    from featherweb.server.workers import supports_reuse_port as check

    assert check() is False


# -- actually running several processes -------------------------------------


@needs_reuse_port
def test_several_workers_share_one_port() -> None:
    port = free_port()
    supervisor = Supervisor("benchmarks.apps:app", workers=2, host="127.0.0.1", port=port)
    import threading

    thread = threading.Thread(target=supervisor.run, daemon=True)
    thread.start()
    try:
        assert wait_for(f"http://127.0.0.1:{port}/plaintext") == "Hello, World!"
        # Several requests, to give the kernel a chance to use both workers.
        for _ in range(10):
            assert wait_for(f"http://127.0.0.1:{port}/plaintext") == "Hello, World!"
    finally:
        supervisor.stop()
        thread.join(timeout=20)


@needs_reuse_port
def test_stopping_the_supervisor_stops_every_worker() -> None:
    port = free_port()
    supervisor = Supervisor("benchmarks.apps:app", workers=2, host="127.0.0.1", port=port)
    import threading

    thread = threading.Thread(target=supervisor.run, daemon=True)
    thread.start()
    wait_for(f"http://127.0.0.1:{port}/plaintext")
    supervisor.stop()
    thread.join(timeout=20)
    deadline = time.time() + 10
    while time.time() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.3)
            if probe.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(0.2)
    raise AssertionError("the port was still being served after stop()")
