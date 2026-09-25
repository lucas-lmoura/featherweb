"""featherweb against the other Python web frameworks.

    python benchmarks/compare.py              # import cost and install size
    python benchmarks/compare.py --all        # also throughput, which takes a while

Two things are compared, because they answer different questions.

**Import cost** is what you pay before serving a single request: a CLI that
imports the framework, a serverless cold start, a test suite that imports it a
thousand times. It is measured with ``-X importtime``, best of several runs,
because the noise on a desktop machine is all upward.

**Throughput** is what you pay per request. It needs every framework behind the
*same* server — uvicorn — or the comparison measures the server instead of the
framework. featherweb's own server is shown separately for that reason.

Frameworks that are not installed are skipped rather than guessed at; install
them with ``uv sync --group bench``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent

#: Each framework's distribution name, and what to import to load it.
FRAMEWORKS: list[tuple[str, str]] = [
    ("featherweb", "import featherweb"),
    ("starlette", "import starlette.applications"),
    ("fastapi", "import fastapi"),
    ("litestar", "import litestar"),
    ("blacksheep", "import blacksheep"),
    ("falcon", "import falcon.asgi"),
    ("quart", "import quart"),
    ("aiohttp", "import aiohttp.web"),
    ("sanic", "import sanic"),
    ("django", "import django"),
    ("flask", "import flask"),
]

#: Routes the throughput run exercises, in every framework that offers them.
ROUTES = ("/plaintext", "/json", "/user/7")


def run(*command: str, timeout: float = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=False, cwd=ROOT
    )


# -- import cost -------------------------------------------------------------


def installed(statement: str) -> bool:
    return run(sys.executable, "-c", statement).returncode == 0


def total_import_ms(statement: str, runs: int = 7) -> float | None:
    """Every module this run imports, added up; best of ``runs``.

    Summing the whole run rather than reading one module's line is the only
    honest way to compare frameworks: ``import starlette.applications`` does
    most of its work under a name that is not ``starlette``, and reading that
    one line reports a tenth of the real cost. What the interpreter imports on
    its own at startup (``encodings``, ``site``...) is subtracted, so the number
    is what the statement adds.
    """
    if statement != "pass":
        baseline = total_import_ms("pass", runs)
        if baseline is None:
            return None
    else:
        baseline = 0.0
    samples: list[float] = []
    for _ in range(runs):
        result = run(sys.executable, "-X", "importtime", "-c", statement)
        if result.returncode != 0:
            return None
        total = 0
        for line in result.stderr.splitlines():
            parts = line.split("|")
            if len(parts) == 3 and parts[0].strip().startswith("import time"):
                try:
                    total += int(parts[0].split(":")[1].strip())  # self time, not cumulative
                except (ValueError, IndexError):
                    continue
        samples.append(total / 1000)
    return min(samples) - baseline if samples else None


def module_count(statement: str) -> int | None:
    """How many modules end up in ``sys.modules``, over a bare interpreter."""
    code = f"import sys; before = len(sys.modules); {statement}; print(len(sys.modules) - before)"
    result = run(sys.executable, "-c", code)
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def dependency_count(distribution: str) -> int | None:
    """How many distributions come along with this one, transitively, extras aside."""
    import importlib.metadata as metadata
    import re

    seen: set[str] = set()
    queue = [distribution]
    while queue:
        name = queue.pop().lower().replace("_", "-")
        if name in seen:
            continue
        seen.add(name)
        try:
            requires = metadata.requires(name) or []
        except metadata.PackageNotFoundError:
            if name == distribution:
                return None
            continue
        queue.extend(
            re.split(r"[\s;\[<>=!~(]", raw, maxsplit=1)[0]
            for raw in requires
            if "extra ==" not in raw
        )
    return len(seen) - 1


def report_imports() -> list[dict[str, Any]]:
    print("\n-- import cost (best of 7, lower is better) --")
    print(f"  {'framework':<14}{'import':>10}  {'modules':>8}  {'deps':>5}")
    rows: list[dict[str, Any]] = []
    for name, statement in FRAMEWORKS:
        if not installed(statement):
            continue
        cost = total_import_ms(statement)
        modules = module_count(statement)
        deps = dependency_count(name)
        rows.append(
            {"framework": name, "import_ms": cost, "modules": modules, "dependencies": deps}
        )
        shown = f"{cost:8.1f} ms" if cost is not None else "        --"
        print(
            f"  {name:<14}{shown}  {modules if modules is not None else '?':>8}  "
            f"{deps if deps is not None else '?':>5}"
        )
    if rows:
        print("\n  every module the import pulls in, dependencies and standard library")
        print("  alike, over what a bare interpreter loads anyway")
    return rows


# -- throughput --------------------------------------------------------------


def report_throughput(*, seconds: int, connections: int) -> dict[str, Any]:
    print(f"\n-- throughput ({seconds}s, {connections} connections, higher is better) --")
    print("  every framework under the same uvicorn, so this measures the framework")

    from apps import STACKS

    sys.path.insert(0, str(ROOT / "benchmarks"))
    from load import run as drive

    results: dict[str, dict[str, float | None]] = {}
    header = f"  {'framework':<24}" + "".join(f"{route:>13}" for route in ROUTES)
    print(header)
    for label, target, port in STACKS:
        command = (
            [
                sys.executable,
                "-m",
                "featherweb",
                "run",
                target,
                "--port",
                str(port),
                "--log-level",
                "error",
            ]
            if label.endswith("(own server)")
            else [
                sys.executable,
                "-m",
                "uvicorn",
                target,
                "--port",
                str(port),
                "--log-level",
                "error",
            ]
        )
        process = _serve(command, port)
        if process is None:
            print(f"  {label:<24}   could not start")
            continue
        try:
            row: dict[str, float | None] = {}
            cells = ""
            for route in ROUTES:
                result = drive(
                    f"http://127.0.0.1:{port}{route}", seconds=seconds, connections=connections
                )
                row[route] = result.rate
                cells += f"{result.rate:>13,.0f}"
            results[label] = row
            print(f"  {label:<24}{cells}")
        finally:
            process.terminate()
            process.wait(timeout=10)
    return results


def _serve(command: list[str], port: int) -> subprocess.Popen[bytes] | None:
    import socket
    import time

    process = subprocess.Popen(
        command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.3)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return process
        if process.poll() is not None:
            return None
        time.sleep(0.2)
    process.terminate()
    return None


# -- driver ------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="also measure throughput")
    parser.add_argument("--seconds", type=int, default=5)
    parser.add_argument("--connections", type=int, default=32)
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()

    print("featherweb against the others")
    print(f"  {sys.implementation.name} {sys.version.split()[0]} on {sys.platform}")

    collected: dict[str, Any] = {"imports": report_imports()}
    if arguments.all:
        collected["throughput"] = report_throughput(
            seconds=arguments.seconds, connections=arguments.connections
        )
    if arguments.json:
        print("\n" + json.dumps(collected, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    raise SystemExit(main())
