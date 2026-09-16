"""The outgoing response: body, status, headers and cookies."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, MutableMapping
from typing import Any, ClassVar, Final, Literal

__all__ = ["MutableHeaders", "RedirectResponse", "Response"]

#: Statuses that must not carry a body, and therefore no Content-Length either.
_BODYLESS: Final = frozenset({204, 304})
_EPOCH: Final = "Thu, 01 Jan 1970 00:00:00 GMT"


class MutableHeaders(MutableMapping[str, str]):
    """Case-insensitive headers that keep repeated fields."""

    __slots__ = ("_items",)

    def __init__(self, initial: Mapping[str, str] | None = None) -> None:
        self._items: list[tuple[str, str]] = []
        if initial:
            for name, value in initial.items():
                self._items.append((name.lower(), value))

    def __getitem__(self, key: str) -> str:
        key = key.lower()
        for name, value in self._items:
            if name == key:
                return value
        raise KeyError(key)

    def __setitem__(self, key: str, value: str) -> None:
        """Replace every value for ``key`` with this one."""
        key = key.lower()
        items: list[tuple[str, str]] = []
        replaced = False
        for name, existing in self._items:
            if name != key:
                items.append((name, existing))
            elif not replaced:
                items.append((key, value))
                replaced = True
        if not replaced:
            items.append((key, value))
        self._items = items

    def __delitem__(self, key: str) -> None:
        key = key.lower()
        remaining = [item for item in self._items if item[0] != key]
        if len(remaining) == len(self._items):
            raise KeyError(key)
        self._items = remaining

    def append(self, name: str, value: str) -> None:
        """Add a value without replacing the ones already there."""
        self._items.append((name.lower(), value))

    def getlist(self, key: str) -> list[str]:
        key = key.lower()
        return [value for name, value in self._items if name == key]

    def __iter__(self) -> Iterator[str]:
        seen: set[str] = set()
        for name, _ in self._items:
            if name not in seen:
                seen.add(name)
                yield name

    def __len__(self) -> int:
        return len({name for name, _ in self._items})

    def raw(self) -> list[tuple[bytes, bytes]]:
        """The ASGI form: a list of lowercase byte pairs."""
        return [(name.encode("latin-1"), value.encode("latin-1")) for name, value in self._items]

    def __repr__(self) -> str:
        return f"MutableHeaders({self._items!r})"


class Response[BodyT]:
    """A response with a typed body, plus the status, headers and cookies.

    The body is rendered once, on the way out: ``bytes`` and ``str`` are sent as
    they are, and anything else is serialized as JSON.
    """

    __slots__ = ("_body", "_cookies", "content", "headers", "media_type", "status")

    #: Media type used when the caller does not pass one; subclasses override it.
    default_media_type: ClassVar[str | None] = None

    def __init__(
        self,
        content: BodyT,
        *,
        status: int = 200,
        headers: Mapping[str, str] | None = None,
        media_type: str | None = None,
    ) -> None:
        self.content = content
        self.status = status
        self.headers = MutableHeaders(headers)
        self.media_type = media_type if media_type is not None else type(self).default_media_type
        self._cookies: list[str] = []
        self._body: bytes | None = None

    def render(self) -> bytes:
        """Serialize the body, remembering the result."""
        if self._body is None:
            body, media_type = _render(self.content, self.media_type)
            self._body = body
            self.media_type = media_type
        return self._body

    def set_body(self, body: bytes) -> None:
        """Replace the rendered body, for middlewares that re-encode it."""
        self._body = body
        self.headers["content-length"] = str(len(body))

    def raw_headers(self) -> list[tuple[bytes, bytes]]:
        """Headers in ASGI form, with content type, length and cookies filled in."""
        body = self.render()
        headers = self.headers.raw()
        present = {name for name, _ in headers}
        if self.media_type and b"content-type" not in present:
            headers.append((b"content-type", self.media_type.encode("latin-1")))
        has_body = self.status >= 200 and self.status not in _BODYLESS
        if has_body and b"content-length" not in present:
            headers.append((b"content-length", str(len(body)).encode("latin-1")))
        headers.extend((b"set-cookie", cookie.encode("latin-1")) for cookie in self._cookies)
        return headers

    def set_cookie(
        self,
        name: str,
        value: str = "",
        *,
        max_age: int | None = None,
        expires: str | None = None,
        path: str = "/",
        domain: str | None = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: Literal["lax", "strict", "none"] | None = "lax",
    ) -> None:
        """Queue a ``Set-Cookie`` header."""
        from urllib.parse import quote

        parts = [f"{name}={quote(value)}"]
        if max_age is not None:
            parts.append(f"Max-Age={max_age}")
        if expires is not None:
            parts.append(f"Expires={expires}")
        if path:
            parts.append(f"Path={path}")
        if domain:
            parts.append(f"Domain={domain}")
        if secure:
            parts.append("Secure")
        if httponly:
            parts.append("HttpOnly")
        if samesite is not None:
            if samesite not in ("lax", "strict", "none"):
                raise ValueError(f"invalid samesite value {samesite!r}")
            if samesite == "none" and not secure:
                raise ValueError("samesite='none' requires secure=True")
            parts.append(f"SameSite={samesite.capitalize()}")
        self._cookies.append("; ".join(parts))

    def delete_cookie(self, name: str, *, path: str = "/", domain: str | None = None) -> None:
        """Queue a ``Set-Cookie`` that expires the cookie immediately."""
        self.set_cookie(name, "", max_age=0, expires=_EPOCH, path=path, domain=domain)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.status}>"


class RedirectResponse(Response[None]):
    """Send the client somewhere else."""

    __slots__ = ()

    def __init__(
        self,
        url: str,
        *,
        status: int = 307,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(None, status=status, headers=headers)
        from urllib.parse import quote

        self.headers["location"] = quote(url, safe=":/%#?=@[]!$&'()*+,;~")


def _render(content: Any, media_type: str | None) -> tuple[bytes, str | None]:
    if content is None:
        return b"", media_type
    if isinstance(content, bytes):
        return content, media_type or "application/octet-stream"
    if isinstance(content, bytearray):
        return bytes(content), media_type or "application/octet-stream"
    if isinstance(content, memoryview):
        return content.tobytes(), media_type or "application/octet-stream"
    if isinstance(content, str):
        return content.encode("utf-8"), media_type or "text/plain; charset=utf-8"

    from ._compat import json_dumps

    return json_dumps(content), media_type or "application/json"
