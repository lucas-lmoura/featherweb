"""A small load generator, for when there is no real one installed.

``oha``, ``hey`` and ``wrk`` are all better than this: they are native, they do
not fight a GIL, and they measure latency properly. Use one of them when you
can — :mod:`measure` prefers them and only falls back to this.

What this is for is making the comparison *runnable anywhere*. It drives every
stack through exactly the same client, so while the absolute numbers are lower
than a native tool would report, the ratio between two servers measured this way
is still worth something. Numbers from here should never be quoted as the
framework's throughput, only as one stack against another on one machine.

The client speaks the little of HTTP/1.1 it needs: keep-alive, a fixed request,
and responses read by ``Content-Length``. Anything unexpected on the wire ends
that connection rather than being parsed.
"""

from __future__ import annotations

import asyncio
import time
from typing import Final

__all__ = ["Result", "measure"]

#: Read size per recv; big enough for the benchmark's small responses.
_CHUNK: Final = 65536


class Result:
    """What one run measured."""

    __slots__ = ("duration", "errors", "latencies", "requests")

    def __init__(self, requests: int, errors: int, duration: float, latencies: list[float]) -> None:
        self.requests = requests
        self.errors = errors
        self.duration = duration
        self.latencies = latencies

    @property
    def rate(self) -> float:
        return self.requests / self.duration if self.duration > 0 else 0.0

    def percentile(self, fraction: float) -> float:
        """Latency in milliseconds at ``fraction`` of the distribution."""
        if not self.latencies:
            return 0.0
        ordered = sorted(self.latencies)
        index = min(int(fraction * len(ordered)), len(ordered) - 1)
        return ordered[index] * 1000

    def __repr__(self) -> str:
        return f"Result({self.requests} requests, {self.rate:.0f}/s, {self.errors} errors)"


async def _one_connection(
    host: str, port: int, request: bytes, deadline: float, latencies: list[float]
) -> tuple[int, int]:
    """Drive one keep-alive connection until the deadline; returns (done, errors)."""
    done = errors = 0
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except OSError:
        return 0, 1
    try:
        while time.monotonic() < deadline:
            started = time.monotonic()
            writer.write(request)
            await writer.drain()
            try:
                if not await _read_response(reader):
                    errors += 1
                    break
            except (OSError, asyncio.IncompleteReadError):
                errors += 1
                break
            latencies.append(time.monotonic() - started)
            done += 1
    finally:
        writer.close()
        with _quiet():
            await writer.wait_closed()
    return done, errors


async def _read_response(reader: asyncio.StreamReader) -> bool:
    """Read one response, using Content-Length; ``False`` if the peer went away."""
    head = await reader.readuntil(b"\r\n\r\n")
    if not head:
        return False
    length = 0
    for line in head.split(b"\r\n"):
        name, separator, value = line.partition(b":")
        if separator and name.strip().lower() == b"content-length":
            length = int(value.strip())
            break
    else:
        if b"transfer-encoding: chunked" in head.lower():
            # Not needed by the benchmark routes, and guessing would skew results.
            raise OSError("chunked responses are not measured here")
    if length:
        await reader.readexactly(length)
    return True


class _quiet:
    """Closing a connection that is already gone is not an error worth raising."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, kind: object, value: object, traceback: object) -> bool:
        return isinstance(value, OSError | asyncio.IncompleteReadError)


async def measure(
    url: str, *, seconds: float = 5.0, connections: int = 32, warmup: float = 0.5
) -> Result:
    """Hammer ``url`` with ``connections`` keep-alive clients for ``seconds``."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80
    path = parts.path or "/"
    request = (
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        f"Accept: */*\r\nConnection: keep-alive\r\n\r\n"
    ).encode()

    if warmup > 0:  # let the server finish whatever it does on the first request
        await _one_connection(host, port, request, time.monotonic() + warmup, [])

    latencies: list[float] = []
    started = time.monotonic()
    deadline = started + seconds
    pairs = await asyncio.gather(
        *(_one_connection(host, port, request, deadline, latencies) for _ in range(connections))
    )
    duration = time.monotonic() - started
    return Result(
        requests=sum(done for done, _ in pairs),
        errors=sum(errors for _, errors in pairs),
        duration=duration,
        latencies=latencies,
    )


def run(url: str, *, seconds: float = 5.0, connections: int = 32) -> Result:
    """:func:`measure`, from synchronous code."""
    return asyncio.run(measure(url, seconds=seconds, connections=connections))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="a small HTTP load generator")
    parser.add_argument("url")
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--connections", type=int, default=32)
    options = parser.parse_args()
    result = run(options.url, seconds=options.seconds, connections=options.connections)
    print(f"{result.rate:,.0f} req/s over {result.duration:.1f}s, {result.errors} errors")
    print(f"latency p50 {result.percentile(0.5):.2f} ms, p99 {result.percentile(0.99):.2f} ms")
