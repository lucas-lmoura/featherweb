"""The route table: static paths in a dict, dynamic ones in a segment tree.

Everything here happens at registration time. A lookup walks at most one node
per path segment and allocates only when a dynamic segment matches.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, Final

__all__ = ["CONVERTERS", "RouteConflictError", "Router", "path_param_names"]

_PARAM_RE: Final = re.compile(r"^\{([a-zA-Z_][a-zA-Z0-9_]*)(?::([a-zA-Z_]+))?\}$")


def _to_uuid(value: str) -> Any:
    from uuid import UUID  # deferred: most applications never use this converter

    return UUID(value)


#: Converters usable as ``{name:converter}``; a failure means "no match".
CONVERTERS: Final[dict[str, Callable[[str], Any]]] = {
    "str": str,
    "int": int,
    "float": float,
    "uuid": _to_uuid,
    "path": str,  # greedy, handled separately: it swallows the remaining segments
}


class RouteConflictError(ValueError):
    """Two routes claim the same method and path."""


def normalize_path(path: str) -> str:
    """Give a path a leading slash and no trailing one, so ``/a`` == ``/a/``."""
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") or "/"


def _split(path: str) -> list[str]:
    return [segment for segment in path.split("/") if segment]


def path_param_names(path: str) -> tuple[str, ...]:
    """Names captured by ``path``, in order."""
    names: list[str] = []
    for segment in _split(path):
        match = _PARAM_RE.match(segment)
        if match is not None:
            names.append(match.group(1))
    return tuple(names)


class _Node[ValueT]:
    __slots__ = ("catch_all", "dynamic", "handlers", "literal")

    def __init__(self) -> None:
        self.literal: dict[str, _Node[ValueT]] = {}
        self.dynamic: list[tuple[str, Callable[[str], Any], _Node[ValueT]]] = []
        self.catch_all: tuple[str, _Node[ValueT]] | None = None
        self.handlers: dict[str, ValueT] = {}


class Router[ValueT]:
    """Maps a method and a path to whatever the application registered."""

    __slots__ = ("_root", "_static")

    def __init__(self) -> None:
        self._static: dict[str, dict[str, ValueT]] = {}
        self._root: _Node[ValueT] = _Node()

    def add(self, method: str, path: str, value: ValueT) -> None:
        """Register ``value`` for ``method`` on ``path``; conflicts raise."""
        path = normalize_path(path)
        dynamic = "{" in path
        handlers = self._tree_handlers(path) if dynamic else self._static.setdefault(path, {})
        if method in handlers:
            raise RouteConflictError(f"{method} {path} is already registered")
        handlers[method] = value

    def _tree_handlers(self, path: str) -> dict[str, ValueT]:
        """Walk (creating as needed) to the node for ``path`` and return its handlers."""
        node: _Node[ValueT] = self._root
        segments = _split(path)
        for index, segment in enumerate(segments):
            match = _PARAM_RE.match(segment)
            if match is None:
                if "{" in segment or "}" in segment:
                    raise ValueError(f"malformed path parameter in {path!r}: {segment!r}")
                fresh: _Node[ValueT] = _Node()
                node = node.literal.setdefault(segment, fresh)
                continue
            name, converter_name = match.group(1), match.group(2) or "str"
            converter = CONVERTERS.get(converter_name)
            if converter is None:
                known = ", ".join(sorted(CONVERTERS))
                raise ValueError(
                    f"unknown converter {converter_name!r} in {path!r}; use one of {known}"
                )
            if converter_name == "path":
                if index != len(segments) - 1:
                    raise ValueError(f"the 'path' converter must be the last segment of {path!r}")
                if node.catch_all is None:
                    catch_all: _Node[ValueT] = _Node()
                    node.catch_all = (name, catch_all)
                node = node.catch_all[1]
                continue
            for existing_name, _, existing_child in node.dynamic:
                if existing_name == name:
                    node = existing_child
                    break
            else:
                child: _Node[ValueT] = _Node()
                node.dynamic.append((name, converter, child))
                node = child
        return node.handlers

    def resolve(self, path: str) -> tuple[Mapping[str, ValueT], dict[str, Any]] | None:
        """Return the handlers registered on ``path`` and the captured parameters.

        ``None`` means no route matched; an empty method mapping never happens,
        so the caller can turn a method miss straight into a 405.
        """
        handlers = self._static.get(normalize_path(path))
        if handlers:
            return handlers, {}
        return self._match(self._root, _split(path), 0, {})

    def _match(
        self,
        node: _Node[ValueT],
        segments: list[str],
        index: int,
        params: dict[str, Any],
    ) -> tuple[Mapping[str, ValueT], dict[str, Any]] | None:
        if index == len(segments):
            return (node.handlers, params) if node.handlers else None
        segment = segments[index]

        child = node.literal.get(segment)
        if child is not None:
            matched = self._match(child, segments, index + 1, params)
            if matched is not None:
                return matched

        for name, converter, dynamic_child in node.dynamic:
            try:
                value = converter(segment)
            except (ValueError, TypeError):
                continue  # this converter says no; another branch may still match
            matched = self._match(dynamic_child, segments, index + 1, {**params, name: value})
            if matched is not None:
                return matched

        if node.catch_all is not None:
            name, catch_all_node = node.catch_all
            if catch_all_node.handlers:
                return catch_all_node.handlers, {**params, name: "/".join(segments[index:])}
        return None
