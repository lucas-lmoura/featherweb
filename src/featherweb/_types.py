"""Minimal ASGI 3 type aliases.

Kept private: the framework speaks ASGI internally, but the public API of
featherweb is the controller layer, not these dicts.
"""

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

__all__ = ["ASGIApp", "Message", "Receive", "Scope", "Send"]

type Scope = MutableMapping[str, Any]
type Message = MutableMapping[str, Any]
type Receive = Callable[[], Awaitable[Message]]
type Send = Callable[[Message], Awaitable[None]]
type ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]
