"""Detection of optional accelerators.

featherweb runs on the standard library alone; anything found here is a bonus
and must never be required.
"""

import asyncio
import importlib
from collections.abc import Callable
from typing import cast

__all__ = ["loop_factory"]

_LOOP_MODULES = ("uvloop", "winloop")


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
