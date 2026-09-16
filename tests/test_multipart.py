"""Reading ``multipart/form-data`` without holding the body in memory.

The parser is fed in deliberately awkward ways — one byte at a time, with the
boundary split across chunks — because that is what a socket does and it is
where a buffer-based parser goes wrong.
"""

from __future__ import annotations

import tracemalloc
from collections.abc import AsyncIterator
from typing import Annotated, Any

import pytest

from featherweb import App, File, Form, Post, Request, Route, UploadFile
from featherweb.multipart import (
    MultipartError,
    MultipartLimits,
    boundary_of,
    parse_multipart,
)
from featherweb.testing import TestClient, TestResponse

BOUNDARY = "----test42"
CONTENT_TYPE = f"multipart/form-data; boundary={BOUNDARY}"


def part(
    name: str, value: str, filename: str | None = None, media_type: str | None = None
) -> bytes:
    head = f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"'
    if filename is not None:
        head += f'; filename="{filename}"'
    head += "\r\n"
    if media_type is not None:
        head += f"Content-Type: {media_type}\r\n"
    return (head + "\r\n" + value + "\r\n").encode()


def closing() -> bytes:
    return f"--{BOUNDARY}--\r\n".encode()


async def feed(body: bytes, size: int | None = None) -> AsyncIterator[bytes]:
    """Hand the body over in chunks of ``size``, or all at once."""
    if size is None:
        yield body
        return
    for start in range(0, len(body), size):
        yield body[start : start + size]


async def parse(body: bytes, size: int | None = None, **kwargs: Any) -> Any:
    return await parse_multipart(feed(body, size), CONTENT_TYPE, **kwargs)


# -- the content type -------------------------------------------------------


def test_the_boundary_is_read_from_the_content_type() -> None:
    assert boundary_of("multipart/form-data; boundary=abc") == b"abc"


def test_a_quoted_boundary_is_unquoted() -> None:
    assert boundary_of('multipart/form-data; boundary="a b"') == b"a b"


@pytest.mark.parametrize(
    "content_type",
    [
        None,
        "",
        "application/json",
        "multipart/form-data",
        "multipart/form-data; boundary=",
        "multipart/form-data; boundary=" + "x" * 71,
    ],
)
def test_a_content_type_we_cannot_use_is_400(content_type: str | None) -> None:
    with pytest.raises(MultipartError) as info:
        boundary_of(content_type)
    assert info.value.status == 400


# -- parsing ----------------------------------------------------------------


async def test_a_text_field_is_read() -> None:
    fields, files = await parse(part("who", "ada") + closing())
    assert fields == [("who", "ada")]
    assert files == []


async def test_repeated_fields_keep_every_value() -> None:
    fields, _ = await parse(part("a", "1") + part("a", "2") + closing())
    assert fields == [("a", "1"), ("a", "2")]


async def test_a_file_keeps_its_name_and_type() -> None:
    _, files = await parse(part("doc", "hello", "a.txt", "text/plain") + closing())
    name, upload = files[0]
    assert (name, upload.filename, upload.content_type, upload.size) == (
        "doc",
        "a.txt",
        "text/plain",
        5,
    )
    assert await upload.read() == b"hello"
    await upload.close()


async def test_a_file_without_a_type_is_octet_stream() -> None:
    _, files = await parse(part("doc", "x", "a.bin") + closing())
    assert files[0][1].content_type == "application/octet-stream"
    await files[0][1].close()


async def test_fields_and_files_can_be_mixed() -> None:
    body = part("who", "ada") + part("doc", "hello", "a.txt") + part("age", "36") + closing()
    fields, files = await parse(body)
    assert fields == [("who", "ada"), ("age", "36")]
    assert [name for name, _ in files] == ["doc"]
    await files[0][1].close()


async def test_an_empty_file_is_still_a_file() -> None:
    _, files = await parse(part("doc", "", "empty.txt") + closing())
    assert files[0][1].size == 0
    await files[0][1].close()


async def test_a_preamble_is_ignored() -> None:
    body = b"ignore me\r\n" + part("who", "ada") + closing()
    fields, _ = await parse(body)
    assert fields == [("who", "ada")]


async def test_an_epilogue_is_ignored() -> None:
    fields, _ = await parse(part("who", "ada") + closing() + b"trailing rubbish")
    assert fields == [("who", "ada")]


@pytest.mark.parametrize("size", [1, 2, 3, 7, 16, 64, 4096])
async def test_the_body_parses_whatever_the_chunks_look_like(size: int) -> None:
    """A boundary split across chunks is the case a buffered parser gets wrong."""
    body = part("who", "ada") + part("doc", "hello world", "a.txt") + closing()
    fields, files = await parse(body, size)
    assert fields == [("who", "ada")]
    assert await files[0][1].read() == b"hello world"
    await files[0][1].close()


async def test_content_that_looks_like_a_boundary_is_not_one() -> None:
    value = f"--{BOUNDARY} but not really\r\nstill the same part"
    fields, _ = await parse(part("who", value) + closing())
    assert fields == [("who", value)]


async def test_an_extended_filename_is_decoded() -> None:
    body = (
        f"--{BOUNDARY}\r\n"
        "Content-Disposition: form-data; name=\"doc\"; filename*=utf-8''r%C3%A9sum%C3%A9.txt\r\n"
        "\r\nx\r\n"
    ).encode() + closing()
    _, files = await parse_multipart(feed(body), CONTENT_TYPE)
    assert files[0][1].filename == "résumé.txt"
    await files[0][1].close()


# -- malformed bodies -------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b"nothing that resembles a multipart body",
        part("who", "ada"),  # never closed
    ],
)
async def test_a_truncated_body_is_400(body: bytes) -> None:
    with pytest.raises(MultipartError):
        await parse(body)


async def test_a_part_without_a_disposition_is_400() -> None:
    body = f"--{BOUNDARY}\r\nContent-Type: text/plain\r\n\r\nx\r\n".encode() + closing()
    with pytest.raises(MultipartError, match="Content-Disposition"):
        await parse_multipart(feed(body), CONTENT_TYPE)


async def test_a_part_without_a_name_is_400() -> None:
    body = f"--{BOUNDARY}\r\nContent-Disposition: form-data\r\n\r\nx\r\n".encode() + closing()
    with pytest.raises(MultipartError, match="name"):
        await parse_multipart(feed(body), CONTENT_TYPE)


async def test_a_folded_header_is_refused() -> None:
    body = (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="a"\r\n\tcontinued\r\n\r\nx\r\n'
    ).encode() + closing()
    with pytest.raises(MultipartError, match="folded"):
        await parse_multipart(feed(body), CONTENT_TYPE)


async def test_a_header_without_a_colon_is_refused() -> None:
    body = (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="a"\r\nrubbish\r\n\r\nx\r\n'
    ).encode() + closing()
    with pytest.raises(MultipartError, match="malformed header"):
        await parse_multipart(feed(body), CONTENT_TYPE)


# -- limits -----------------------------------------------------------------


async def test_too_many_parts_is_refused() -> None:
    body = b"".join(part(f"f{index}", "x") for index in range(5)) + closing()
    with pytest.raises(MultipartError, match="too many parts"):
        await parse(body, limits=MultipartLimits(max_parts=3))


async def test_an_oversized_field_is_413() -> None:
    with pytest.raises(MultipartError) as info:
        await parse(part("a", "x" * 100) + closing(), limits=MultipartLimits(max_field_size=10))
    assert info.value.status == 413


async def test_an_oversized_file_is_413() -> None:
    with pytest.raises(MultipartError) as info:
        await parse(
            part("a", "x" * 100, "a.bin") + closing(),
            limits=MultipartLimits(max_file_size=10),
        )
    assert info.value.status == 413


async def test_oversized_part_headers_are_refused() -> None:
    padding = "x" * 9000
    body = (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="a"; note="{padding}"\r\n'
    ).encode()
    with pytest.raises(MultipartError, match="too long"):
        await parse_multipart(feed(body), CONTENT_TYPE)


async def test_a_file_spools_to_disk_once_it_grows() -> None:
    body = part("a", "x" * 5000, "a.bin") + closing()
    _, files = await parse(body, limits=MultipartLimits(spool_max_size=1024))
    upload = files[0][1]
    assert upload.spooled_to_disk
    assert len(await upload.read()) == 5000
    await upload.close()


async def test_a_small_file_stays_in_memory() -> None:
    _, files = await parse(part("a", "x" * 10, "a.bin") + closing())
    assert not files[0][1].spooled_to_disk
    await files[0][1].close()


async def test_a_large_upload_does_not_buffer_the_body() -> None:
    """The phase-4 target: 100 MB through the parser without holding it."""
    total = 100 * 1024 * 1024
    block = b"x" * (64 * 1024)

    async def body() -> AsyncIterator[bytes]:
        yield (
            f"--{BOUNDARY}\r\n"
            'Content-Disposition: form-data; name="payload"; filename="big.bin"\r\n\r\n'
        ).encode()
        sent = 0
        while sent < total:
            yield block
            sent += len(block)
        yield f"\r\n--{BOUNDARY}--\r\n".encode()

    tracemalloc.start()
    try:
        _, files = await parse_multipart(body(), CONTENT_TYPE)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    upload = files[0][1]
    try:
        assert upload.size == total
        assert upload.spooled_to_disk
        # Well under the body: what is held is a buffer, not the upload.
        assert peak < 16 * 1024 * 1024, f"peak was {peak} bytes"
    finally:
        await upload.close()


# -- through a handler ------------------------------------------------------


@Route("/u")
class UploadController:
    @Post("/one")
    async def one(self, avatar: UploadFile) -> dict[str, Any]:
        return {"name": avatar.filename, "body": (await avatar.read()).decode()}

    @Post("/mixed")
    async def mixed(
        self, who: Annotated[str, Form()], doc: Annotated[UploadFile, File()]
    ) -> dict[str, Any]:
        return {"who": who, "doc": doc.filename}

    @Post("/many")
    async def many(self, docs: list[UploadFile]) -> dict[str, Any]:
        return {"names": [doc.filename for doc in docs]}

    @Post("/optional")
    async def optional(self, avatar: UploadFile | None = None) -> dict[str, Any]:
        return {"name": avatar.filename if avatar is not None else None}

    @Post("/raw")
    async def raw(self, request: Request) -> dict[str, Any]:
        form = await request.form()
        return {
            "fields": form.multi_items(),
            "files": [[name, file.filename] for name, file in form.files],
        }


async def post(path: str, body: bytes) -> TestResponse:
    async with TestClient(App(controllers=[UploadController])) as client:
        return await client.post(path, content=body, headers={"content-type": CONTENT_TYPE})


async def test_an_upload_parameter_is_inferred_from_its_type() -> None:
    response = await post("/u/one", part("avatar", "hello", "a.png") + closing())
    assert response.json() == {"name": "a.png", "body": "hello"}


async def test_a_form_field_and_a_file_together() -> None:
    body = part("who", "ada") + part("doc", "x", "d.pdf") + closing()
    assert (await post("/u/mixed", body)).json() == {"who": "ada", "doc": "d.pdf"}


async def test_a_list_of_uploads() -> None:
    body = part("docs", "1", "one.txt") + part("docs", "2", "two.txt") + closing()
    assert (await post("/u/many", body)).json() == {"names": ["one.txt", "two.txt"]}


async def test_a_missing_optional_upload_is_none() -> None:
    assert (await post("/u/optional", closing())).json() == {"name": None}


async def test_a_missing_required_upload_is_422() -> None:
    response = await post("/u/one", closing())
    assert response.status == 422
    assert response.json()["detail"] == [
        {"location": "file", "field": "avatar", "message": "field required"}
    ]


async def test_the_whole_form_is_available_from_the_request() -> None:
    body = part("a", "1") + part("a", "2") + part("f", "x", "f.bin") + closing()
    assert (await post("/u/raw", body)).json() == {
        "fields": [["a", "1"], ["a", "2"]],
        "files": [["f", "f.bin"]],
    }


async def test_a_malformed_body_reaches_the_client_as_400() -> None:
    response = await post("/u/one", b"not a multipart body at all")
    assert response.status == 400


def test_a_file_parameter_has_to_be_an_upload() -> None:
    from featherweb.params import RouteConfigurationError

    @Route("/bad")
    class BadController:
        @Post
        async def index(self, doc: Annotated[str, File()]) -> str:
            return doc

    with pytest.raises(RouteConfigurationError, match="annotated UploadFile"):
        App(controllers=[BadController])
