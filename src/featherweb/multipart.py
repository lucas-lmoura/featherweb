"""Streaming ``multipart/form-data``.

The body is consumed as it arrives and never held whole: a part is written out
as its bytes come in, so a 100 MB upload costs a buffer and a temporary file
rather than 100 MB of memory. Small parts stay in memory; a part that grows past
the spool threshold rolls over to disk on its own.

The parser is a state machine over one growing ``bytearray``. The delimiter that
ends a part is ``CRLF + "--" + boundary``, so while scanning a body the last few
bytes of the buffer are held back: they may turn out to be the beginning of that
delimiter once the next chunk arrives.

This module is imported only when a request actually carries a multipart body,
so it stays off the package's import path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Final

from .exceptions import HTTPError

__all__ = ["MultipartError", "MultipartLimits", "UploadFile", "parse_multipart"]

_CRLF: Final = b"\r\n"
_DASH: Final = b"--"
#: Headers of one part, which are small by construction.
_MAX_PART_HEADERS: Final = 8 * 1024


class MultipartError(HTTPError):
    """The body does not parse as ``multipart/form-data``."""

    status = 400


class MultipartLimits:
    """What a multipart body is allowed to cost."""

    __slots__ = ("max_field_size", "max_file_size", "max_parts", "spool_max_size")

    def __init__(
        self,
        *,
        max_parts: int = 1000,
        max_field_size: int = 1024 * 1024,
        max_file_size: int | None = None,
        spool_max_size: int = 1024 * 1024,
    ) -> None:
        #: How many parts the body may contain at all.
        self.max_parts = max_parts
        #: Cap on one text field, which is held in memory.
        self.max_field_size = max_field_size
        #: Cap on one uploaded file; ``None`` leaves it to the server's own limits.
        self.max_file_size = max_file_size
        #: A file bigger than this stops being memory and becomes a temporary file.
        self.spool_max_size = spool_max_size


DEFAULT_LIMITS: Final = MultipartLimits()


class UploadFile:
    """One uploaded file, spooled to disk once it gets big enough."""

    __slots__ = ("_file", "_on_disk", "content_type", "filename", "headers", "size")

    def __init__(
        self,
        filename: str,
        *,
        content_type: str = "application/octet-stream",
        headers: dict[str, str] | None = None,
        spool_max_size: int = DEFAULT_LIMITS.spool_max_size,
    ) -> None:
        from tempfile import SpooledTemporaryFile

        self.filename = filename
        self.content_type = content_type
        self.headers = headers or {}
        #: Bytes written so far.
        self.size = 0
        # Not a context manager: the file outlives this call and close() owns it.
        self._file: Any = SpooledTemporaryFile(max_size=spool_max_size)  # noqa: SIM115
        self._on_disk = spool_max_size <= 0

    async def write(self, data: bytes) -> None:
        """Append to the file, in a thread once it lives on disk."""
        if not data:
            return
        self.size += len(data)
        await self._run(self._file.write, data)

    async def read(self, size: int = -1) -> bytes:
        """Read from the current position; the whole rest of it by default."""
        return await self._run(self._file.read, size)

    async def seek(self, offset: int, whence: int = 0) -> int:
        return await self._run(self._file.seek, offset, whence)

    async def close(self) -> None:
        """Drop the file, deleting the temporary one if there is any."""
        await self._run(self._file.close)

    @property
    def spooled_to_disk(self) -> bool:
        """Whether this file stopped fitting in memory."""
        return self._on_disk

    async def _run(self, call: Any, *arguments: Any) -> Any:
        # In memory the call is a buffer copy, so a thread would cost more than
        # it saves; once the spool has rolled over it is real disk I/O.
        if not self._on_disk:
            self._on_disk = getattr(self._file, "_rolled", False)
            return call(*arguments)
        import asyncio

        return await asyncio.to_thread(call, *arguments)

    def __repr__(self) -> str:
        return f"UploadFile({self.filename!r}, {self.size} bytes)"


def boundary_of(content_type: str | None) -> bytes:
    """The ``boundary`` parameter of a multipart content type."""
    if not content_type:
        raise MultipartError(400, "a multipart body needs a Content-Type")
    media_type, _, parameters = content_type.partition(";")
    if media_type.strip().lower() != "multipart/form-data":
        raise MultipartError(400, "expected a multipart/form-data body")
    for parameter in parameters.split(";"):
        name, _, value = parameter.partition("=")
        if name.strip().lower() != "boundary":
            continue
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) > 1:
            value = value[1:-1]
        if not value:
            break
        # RFC 2046: at most 70 characters, all from a restricted set.
        if len(value) > 70:
            raise MultipartError(400, "the multipart boundary is too long")
        return value.encode("latin-1")
    raise MultipartError(400, "the multipart Content-Type has no boundary")


class _Part:
    """The headers of the part currently being read."""

    __slots__ = ("content_type", "filename", "headers", "name")

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers
        disposition = headers.get("content-disposition")
        if disposition is None:
            raise MultipartError(400, "a multipart part needs a Content-Disposition")
        parameters = _disposition_parameters(disposition)
        name = parameters.get("name")
        if name is None:
            raise MultipartError(400, "a multipart part needs a name")
        self.name = name
        self.filename = parameters.get("filename")
        self.content_type = headers.get("content-type", "application/octet-stream")


async def parse_multipart(
    stream: AsyncIterator[bytes],
    content_type: str | None,
    *,
    limits: MultipartLimits = DEFAULT_LIMITS,
) -> tuple[list[tuple[str, str]], list[tuple[str, UploadFile]]]:
    """Read ``stream`` as a multipart body into its text fields and its files.

    The caller owns the files that come back and is responsible for closing
    them.
    """
    boundary = boundary_of(content_type)
    fields: list[tuple[str, str]] = []
    files: list[tuple[str, UploadFile]] = []
    reader = _Reader(boundary, limits, fields, files)
    try:
        async for chunk in stream:
            reader.feed(chunk)
            # Per chunk, not at the end: queued writes must not become the
            # buffer the whole point of streaming was to avoid.
            await reader.drain()
        reader.finish()
        for _, file in files:
            # Hand each file back ready to read, not at the end of what we wrote.
            await file.seek(0)
    except BaseException:
        # Including the part that was still being written: it has a temporary
        # file of its own and has not been handed over yet.
        for file in (*(upload for _, upload in files), reader.in_flight):
            if file is not None:
                await file.close()
        raise
    return fields, files


class _Reader:
    """The state machine, minus the awaiting, so the states stay readable."""

    __slots__ = (
        "_at_boundary",
        "_boundary",
        "_buffer",
        "_delimiter",
        "_fields",
        "_files",
        "_finished",
        "_limits",
        "_part",
        "_parts_seen",
        "_pending",
        "_started",
        "_target",
        "_text",
    )

    def __init__(
        self,
        boundary: bytes,
        limits: MultipartLimits,
        fields: list[tuple[str, str]],
        files: list[tuple[str, UploadFile]],
    ) -> None:
        self._boundary = boundary
        #: What ends a part, once one has started.
        self._delimiter = _CRLF + _DASH + boundary
        self._limits = limits
        self._fields = fields
        self._files = files
        self._buffer = bytearray()
        self._started = False
        #: Sitting just past a boundary, waiting for the CRLF or the final "--".
        self._at_boundary = False
        self._finished = False
        self._parts_seen = 0
        self._part: _Part | None = None
        self._target: UploadFile | None = None
        self._text: bytearray | None = None
        #: Writes waiting for the caller to await them.
        self._pending: list[tuple[UploadFile, bytes]] = []

    def feed(self, chunk: bytes) -> None:
        if self._finished or not chunk:
            return
        self._buffer += chunk
        while self._step():
            pass

    def finish(self) -> None:
        if not self._finished:
            raise MultipartError(400, "the multipart body ended in the middle of a part")

    @property
    def in_flight(self) -> UploadFile | None:
        """The file being written right now, if a part is open on one."""
        return self._target

    async def drain(self) -> None:
        """Perform the writes the parser queued up."""
        for file, data in self._pending:
            await file.write(data)
        self._pending.clear()

    # -- states ---------------------------------------------------------

    def _step(self) -> bool:
        """Make what progress the buffer allows; ``True`` if there may be more."""
        if self._finished:
            return False
        if not self._started:
            return self._find_first_boundary()
        if self._at_boundary:
            # A state of its own: the two bytes after a boundary can arrive in
            # a later chunk than the boundary itself.
            return self._consume_boundary_tail()
        if self._part is None:
            return self._read_headers()
        return self._read_body()

    def _find_first_boundary(self) -> bool:
        opening = _DASH + self._boundary
        index = self._buffer.find(opening)
        if index == -1:
            self._trim_preamble(len(opening))
            return False
        del self._buffer[: index + len(opening)]
        self._started = True
        self._at_boundary = True
        return True

    def _trim_preamble(self, keep: int) -> None:
        """Drop the preamble, keeping enough for a boundary split across chunks."""
        excess = len(self._buffer) - keep
        if excess > 0:
            del self._buffer[:excess]

    def _consume_boundary_tail(self) -> bool:
        """Read what follows a boundary: ``CRLF`` for a part, ``--`` for the end."""
        if self._buffer.startswith(_DASH):
            self._finished = True
            return False
        if self._buffer.startswith(_CRLF):
            del self._buffer[:2]
            self._at_boundary = False
            self._parts_seen += 1
            if self._parts_seen > self._limits.max_parts:
                raise MultipartError(400, "too many parts in the multipart body")
            return True
        if len(self._buffer) < 2:
            return False  # the tail is still on its way
        raise MultipartError(400, "malformed multipart boundary")

    def _read_headers(self) -> bool:
        end = self._buffer.find(_CRLF + _CRLF)
        if end == -1:
            if len(self._buffer) > _MAX_PART_HEADERS:
                raise MultipartError(400, "the headers of a multipart part are too long")
            return False
        raw = bytes(self._buffer[:end])
        del self._buffer[: end + 4]
        self._part = _Part(_parse_headers(raw))
        self._open_target(self._part)
        return True

    def _open_target(self, part: _Part) -> None:
        if part.filename is None:
            self._text = bytearray()
            self._target = None
            return
        self._text = None
        self._target = UploadFile(
            part.filename,
            content_type=part.content_type,
            headers=part.headers,
            spool_max_size=self._limits.spool_max_size,
        )

    def _read_body(self) -> bool:
        index = self._buffer.find(self._delimiter)
        if index == -1:
            # Hold back what could still turn into the delimiter.
            keep = len(self._delimiter) - 1
            available = len(self._buffer) - keep
            if available > 0:
                self._emit(bytes(self._buffer[:available]))
                del self._buffer[:available]
            return False
        self._emit(bytes(self._buffer[:index]))
        del self._buffer[: index + len(self._delimiter)]
        self._close_part()
        self._at_boundary = True
        return True

    def _emit(self, data: bytes) -> None:
        if not data:
            return
        if self._target is not None:
            written = self._target.size + sum(
                len(pending) for file, pending in self._pending if file is self._target
            )
            limit = self._limits.max_file_size
            if limit is not None and written + len(data) > limit:
                raise MultipartError(413, "an uploaded file is too large")
            self._pending.append((self._target, data))
            return
        text = self._text
        if text is None:  # pragma: no cover - a part is always one or the other
            return
        if len(text) + len(data) > self._limits.max_field_size:
            raise MultipartError(413, "a multipart field is too large")
        text += data

    def _close_part(self) -> None:
        part = self._part
        if part is None:  # pragma: no cover - only reachable out of order
            return
        if self._target is not None:
            self._files.append((part.name, self._target))
        elif self._text is not None:
            self._fields.append((part.name, self._text.decode("utf-8", "replace")))
        self._part = None
        self._target = None
        self._text = None


def _parse_headers(raw: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in raw.split(_CRLF):
        if not line:
            continue
        if line[:1] in (b" ", b"\t"):
            raise MultipartError(400, "folded headers are not allowed in a multipart part")
        name, separator, value = line.partition(b":")
        if not separator:
            raise MultipartError(400, "malformed header in a multipart part")
        headers[name.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    return headers


def _disposition_parameters(disposition: str) -> dict[str, str]:
    """The ``name`` and ``filename`` of a ``Content-Disposition``."""
    parameters: dict[str, str] = {}
    for parameter in _split_parameters(disposition)[1:]:
        key, separator, value = parameter.partition("=")
        if not separator:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key.endswith("*"):
            parameters[key[:-1]] = _decode_extended(value)
        elif value.startswith('"') and value.endswith('"') and len(value) > 1:
            parameters.setdefault(key, value[1:-1].replace('\\"', '"'))
        else:
            parameters.setdefault(key, value)
    return parameters


def _split_parameters(value: str) -> list[str]:
    """Split on ``;``, leaving the ones inside a quoted string alone."""
    parts: list[str] = []
    current: list[str] = []
    quoted = False
    escaped = False
    for char in value:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\" and quoted:
            current.append(char)
            escaped = True
        elif char == '"':
            quoted = not quoted
            current.append(char)
        elif char == ";" and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _decode_extended(value: str) -> str:
    """RFC 5987 ``filename*=utf-8''name.txt``."""
    from urllib.parse import unquote

    charset, _, rest = value.partition("'")
    _, _, encoded = rest.partition("'")
    if not encoded:
        return value
    return unquote(encoded, encoding=charset or "utf-8", errors="replace")
