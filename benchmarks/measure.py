"""The measurements section 2 of the plan asks to record.

Four of the five targets are checked here; the fifth, zero required runtime
dependencies, is asserted by the test suite instead.

    python benchmarks/measure.py            # import time, memory, source size
    python benchmarks/measure.py --all      # also the throughput comparison

Throughput needs a load generator. ``oha`` is what the plan names, because it
works on Windows too; ``hey`` and ``wrk`` are accepted as well. Without one of
them installed, that section says so and is skipped rather than guessed at.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
SOURCE = ROOT / "src"
#: Every target from section 2 that a number can be put against.
#: The import budget is the package's own cost, measured with typing already
#: loaded. A generic class pulls typing in by itself — ``class Response[BodyT]``
#: is enough — so the bare-interpreter number is recorded rather than aimed at.
#: Section 2 of PLAN.md has the measurements behind both.
IMPORT_BUDGET_MS = 12.0
LINE_BUDGET = 6000
MEMORY_BUDGET_MB = 20.0

#: Load generators understood here, in the order they are looked for.
LOAD_GENERATORS = ("oha", "hey", "wrk")


def run(*command: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=False, cwd=ROOT
    )


# -- import time -------------------------------------------------------------


def import_time_ms(statement: str = "import featherweb", runs: int = 7) -> float:
    """The cumulative cost of ``import featherweb``, as -X importtime reports it.

    The best of several runs, not the mean: the noise on a desktop machine is
    all upward, so the minimum is the closest thing to the real cost.
    """
    samples: list[float] = []
    for _ in range(runs):
        result = run(sys.executable, "-X", "importtime", "-c", statement)
        for line in result.stderr.splitlines():
            parts = line.split("|")
            if len(parts) == 3 and parts[2].strip() == "featherweb":
                samples.append(int(parts[1].strip()) / 1000)
    if not samples:
        raise RuntimeError("could not read an import time from -X importtime")
    return min(samples)


def report_import_time() -> dict[str, Any]:
    bare = import_time_ms()
    warm = import_time_ms("import typing; import featherweb")
    lazy = check_lazy_modules()
    print(f"\n-- import time (the package's own cost, target < {IMPORT_BUDGET_MS:.0f} ms) --")
    print(f"  with typing already loaded{warm:6.1f} ms   {verdict(warm < IMPORT_BUDGET_MS)}")
    print(f"  bare interpreter          {bare:6.1f} ms   recorded, not a target")
    print(f"  kept off the import path  {', '.join(lazy) if lazy else 'none!'}")
    return {"bare_ms": bare, "warm_ms": warm, "deferred": lazy}


def check_lazy_modules() -> list[str]:
    """Which of the heavy modules really stay out of ``import featherweb``."""
    watched = ("multipart", "staticfiles", "websocket", "auth", "logging")
    code = (
        "import sys; import featherweb; "
        "print(','.join(name for name in "
        f"{watched!r} if not any(module == name or module.endswith('.' + name) "
        "for module in sys.modules))"
        ")"
    )
    result = run(sys.executable, "-c", code)
    return [name for name in result.stdout.strip().split(",") if name]


# -- memory ------------------------------------------------------------------


def idle_memory_mb() -> float | None:
    """Resident memory of a process holding a built application and doing nothing."""
    code = """
import sys

import featherweb
from featherweb import App, Get, Route


@Route("/bench")
class C:
    @Get("/{n:int}")
    async def show(self, n: int) -> dict[str, int]:
        return {"n": n}


app = App(controllers=[C])


def resident_mb() -> float:
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes as w

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", w.DWORD), ("PageFaultCount", w.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = w.HANDLE
        kernel32.K32GetProcessMemoryInfo.argtypes = [
            w.HANDLE, ctypes.POINTER(Counters), w.DWORD
        ]
        kernel32.K32GetProcessMemoryInfo.restype = w.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not kernel32.K32GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise OSError("GetProcessMemoryInfo failed")
        return counters.WorkingSetSize / 1024 / 1024
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes, macOS bytes.
    return peak / 1024 / 1024 if sys.platform == "darwin" else peak / 1024


print(resident_mb())
"""
    result = run(sys.executable, "-c", code)
    try:
        return float(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def report_memory() -> dict[str, Any]:
    used = idle_memory_mb()
    print(f"\n-- idle memory (target < {MEMORY_BUDGET_MB:.0f} MB) --")
    if used is None:
        print("  could not be measured on this platform")
        return {"idle_mb": None}
    print(f"  one process, app built    {used:6.1f} MB   {verdict(used < MEMORY_BUDGET_MB)}")
    return {"idle_mb": used}


# -- source size -------------------------------------------------------------


def source_lines() -> dict[str, int]:
    """Lines of the package, split so the target is measured against code alone."""
    import ast

    counts = {"total": 0, "blank": 0, "comment": 0, "docstring": 0, "code": 0}
    for path in sorted(SOURCE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        counts["total"] += len(lines)
        documented: set[int] = set()
        for node in ast.walk(ast.parse(text)):
            body = getattr(node, "body", None)
            if not body or not isinstance(
                node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
            ):
                continue
            first = body[0]
            if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
                continue
            if isinstance(first.value.value, str):
                documented.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped:
                counts["blank"] += 1
            elif number in documented:
                counts["docstring"] += 1
            elif stripped.startswith("#"):
                counts["comment"] += 1
            else:
                counts["code"] += 1
    return counts


def report_source() -> dict[str, Any]:
    counts = source_lines()
    print(f"\n-- source size (a ceiling against bloat, target < {LINE_BUDGET} lines) --")
    within = verdict(counts["code"] <= LINE_BUDGET)
    print(f"  code                      {counts['code']:6}     {within}")
    print(f"  docstrings                {counts['docstring']:6}")
    print(f"  comments                  {counts['comment']:6}")
    print(f"  blank                     {counts['blank']:6}")
    print(f"  total                     {counts['total']:6}")
    return counts


# -- throughput --------------------------------------------------------------


def find_load_generator() -> str | None:
    for name in LOAD_GENERATORS:
        if shutil.which(name):
            return name
    return None


def serve(command: list[str], port: int) -> subprocess.Popen[bytes]:
    process = subprocess.Popen(
        command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        if reachable(port):
            return process
        if process.poll() is not None:
            raise RuntimeError(f"server exited early: {' '.join(command)}")
        time.sleep(0.2)
    process.terminate()
    raise RuntimeError(f"server did not come up: {' '.join(command)}")


def reachable(port: int) -> bool:
    import socket

    with socket.socket() as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def load(tool: str, url: str, *, seconds: int, connections: int) -> float | None:
    """Requests per second, as the chosen tool reports them."""
    if tool == "builtin":
        from load import run as run_load

        result = run_load(url, seconds=seconds, connections=connections)
        return result.rate if not result.errors or result.requests else None
    if tool == "oha":
        result = run("oha", "--no-tui", "-j", "-z", f"{seconds}s", "-c", str(connections), url)
        try:
            return float(json.loads(result.stdout)["summary"]["requestsPerSec"])
        except (ValueError, KeyError):
            return None
    if tool == "hey":
        result = run("hey", "-z", f"{seconds}s", "-c", str(connections), url)
        for line in result.stdout.splitlines():
            if "Requests/sec" in line:
                return float(line.split(":")[1])
        return None
    result = run("wrk", f"-d{seconds}s", "-c", str(connections), "-t2", url)
    for line in result.stdout.splitlines():
        if "Requests/sec" in line:
            return float(line.split(":")[1])
    return None


def report_throughput(*, seconds: int, connections: int) -> dict[str, Any]:
    print(f"\n-- throughput ({seconds}s, {connections} connections) --")
    tool = find_load_generator()
    if tool is None:
        tool = "builtin"
        print(f"  no native load generator ({', '.join(LOAD_GENERATORS)}); using the Python one")
        print("  it drives every stack the same way, so compare the columns, not the numbers")

    stacks = [
        (
            "featherweb (own server)",
            [
                sys.executable,
                "-m",
                "featherweb",
                "run",
                "benchmarks.apps:app",
                "--port",
                "8801",
                "--log-level",
                "error",
            ],
            8801,
        ),
        (
            "featherweb + uvicorn",
            [
                sys.executable,
                "-m",
                "uvicorn",
                "benchmarks.apps:app",
                "--port",
                "8802",
                "--log-level",
                "error",
            ],
            8802,
        ),
    ]
    from apps import starlette_app

    if starlette_app() is not None:
        stacks.append(
            (
                "starlette + uvicorn",
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "benchmarks.apps:starlette",
                    "--port",
                    "8803",
                    "--log-level",
                    "error",
                ],
                8803,
            )
        )
    else:
        print("  starlette is not installed, so there is nothing to compare against")

    results: dict[str, dict[str, float | None]] = {}
    for label, command, port in stacks:
        try:
            process = serve(command, port)
        except RuntimeError as exc:
            print(f"  {label:26} could not start ({exc})")
            continue
        try:
            row: dict[str, float | None] = {}
            for route in ("/plaintext", "/json", "/user/7"):
                rate = load(
                    tool,
                    f"http://127.0.0.1:{port}{route}",
                    seconds=seconds,
                    connections=connections,
                )
                row[route] = rate
                shown = f"{rate:10,.0f} req/s" if rate else "   unmeasured"
                print(f"  {label:26} {route:12} {shown}")
            results[label] = row
        finally:
            process.terminate()
            process.wait(timeout=10)
    return {"tool": tool, "results": results}


# -- driver ------------------------------------------------------------------


def verdict(passed: bool) -> str:
    return "ok" if passed else "OVER"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="also measure throughput")
    parser.add_argument("--seconds", type=int, default=5, help="load duration per route")
    parser.add_argument("--connections", type=int, default=32, help="concurrent connections")
    parser.add_argument("--json", action="store_true", help="print the numbers as JSON too")
    arguments = parser.parse_args()

    print("featherweb — the measurable targets of section 2")
    print(f"  {sys.implementation.name} {sys.version.split()[0]} on {sys.platform}")

    collected: dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "import": report_import_time(),
        "memory": report_memory(),
        "source": report_source(),
    }
    if arguments.all:
        collected["throughput"] = report_throughput(
            seconds=arguments.seconds, connections=arguments.connections
        )
    if arguments.json:
        print("\n" + json.dumps(collected, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
