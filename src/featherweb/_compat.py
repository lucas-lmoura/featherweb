"""Detection of optional accelerators.

featherweb runs on the standard library alone; anything found here is a bonus
and must never be required. Detection is deferred to first use so that
``import featherweb`` does not pay for it.
"""

import asyncio
import importlib
from collections.abc import Callable
from typing import Any, cast

__all__ = ["json_dumps", "json_loads", "loop_factory"]

_LOOP_MODULES = ("uvloop", "winloop")

_dumps: Callable[[Any], bytes] | None = None
_loads: Callable[[bytes], Any] | None = None


def loop_factory() -> Callable[[], asyncio.AbstractEventLoop]:
    """Return the fastest available event loop factory.

    ``uvloop`` (Linux, macOS) or ``winloop`` (Windows) when installed,
    ``asyncio.new_event_loop`` otherwise.
    """
    for name in _LOOP_MODULES:
        try:
            module = importlib.import_module(name)
        except ImportError:
            continue
        factory = getattr(module, "new_event_loop", None)
        if callable(factory):
            return cast(Callable[[], asyncio.AbstractEventLoop], factory)
    return asyncio.new_event_loop


def json_dumps(value: Any) -> bytes:
    """Serialize to compact JSON bytes, through orjson when it is installed."""
    global _dumps
    if _dumps is None:
        _dumps = _pick_dumps()
    return _dumps(value)


def json_loads(data: bytes | str) -> Any:
    """Parse JSON from bytes or text; malformed input raises ``ValueError``."""
    global _loads
    if _loads is None:
        _loads = _pick_loads()
    return _loads(data.encode("utf-8") if isinstance(data, str) else data)


def _orjson() -> Any | None:
    try:
        return importlib.import_module("orjson")
    except ImportError:
        return None


def _pick_dumps() -> Callable[[Any], bytes]:
    orjson = _orjson()
    if orjson is not None:
        return cast(Callable[[Any], bytes], orjson.dumps)
    import json

    encode = json.JSONEncoder(ensure_ascii=False, separators=(",", ":")).encode

    def dumps(value: Any) -> bytes:
        return encode(value).encode("utf-8")

    return dumps


def _pick_loads() -> Callable[[bytes], Any]:
    orjson = _orjson()
    if orjson is not None:
        return cast(Callable[[bytes], Any], orjson.loads)
    import json

    return cast(Callable[[bytes], Any], json.loads)
