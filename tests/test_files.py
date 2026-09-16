"""Streamed bodies and files served from disk.

These cover the response half of phase 4: a body produced a chunk at a time, and
a file with the validators, the 304 and the ranges a cache expects.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from featherweb import App, FileResponse, Get, Request, Route, StreamingResponse
from featherweb.exceptions import HTTPError
from featherweb.response import _parse_range  # pyright: ignore[reportPrivateUsage]
from featherweb.testing import TestClient, TestResponse

CONTENT = b"0123456789" * 10  # 100 bytes, so offsets are easy to read


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    path = tmp_path / "sample.txt"
    path.write_bytes(CONTENT)
    return path


async def call(controller: type, path: str, **kwargs: Any) -> TestResponse:
    async with TestClient(App(controllers=[controller])) as client:
        return await client.get(path, **kwargs)


# -- streaming --------------------------------------------------------------


@Route("/s")
class StreamController:
    @Get("/async")
    async def from_async(self) -> StreamingResponse:
        async def chunks() -> AsyncIterator[bytes]:
            for index in range(3):
                yield f"{index};".encode()

        return StreamingResponse(chunks(), media_type="text/plain")

    @Get("/sync")
    async def from_sync(self) -> StreamingResponse:
        def chunks() -> Iterator[str]:
            yield from ("a", "b", "c")

        return StreamingResponse(chunks())

    @Get("/status", status=201)
    async def with_status(self) -> StreamingResponse:
        return StreamingResponse([b"made"])


async def test_an_async_source_is_streamed() -> None:
    response = await call(StreamController, "/s/async")
    assert response.status == 200
    assert response.text == "0;1;2;"
    assert response.headers["content-type"] == "text/plain"


async def test_a_sync_source_is_streamed_and_text_is_encoded() -> None:
    assert (await call(StreamController, "/s/sync")).text == "abc"


async def test_a_streamed_body_has_no_content_length() -> None:
    """Without a length the server frames it as chunked, which is the point."""
    response = await call(StreamController, "/s/async")
    assert "content-length" not in response.headers


async def test_a_streamed_response_still_takes_the_declared_status() -> None:
    assert (await call(StreamController, "/s/status")).status == 201


async def test_gzip_leaves_a_streamed_body_alone() -> None:
    from featherweb import GZip

    app = App(controllers=[StreamController], middlewares=[GZip(minimum_size=1)])
    async with TestClient(app) as client:
        response = await client.get("/s/async", headers={"accept-encoding": "gzip"})
    assert response.text == "0;1;2;"
    assert "content-encoding" not in response.headers


# -- files ------------------------------------------------------------------


def file_controller(path: Path) -> type:
    @Route("/f")
    class FileController:
        @Get("/plain")
        async def plain(self) -> FileResponse:
            return FileResponse(path)

        @Get("/conditional")
        async def conditional(self, request: Request) -> FileResponse:
            return FileResponse(path, request=request)

        @Get("/named")
        async def named(self) -> FileResponse:
            return FileResponse(path, filename="report.txt")

        @Get("/inline")
        async def inline(self) -> FileResponse:
            return FileResponse(path, filename="rep ort.txt", disposition="inline")

        @Get("/missing")
        async def missing(self) -> FileResponse:
            return FileResponse(path.parent / "nope.txt")

    return FileController


async def test_a_file_is_sent_with_its_validators(sample: Path) -> None:
    response = await call(file_controller(sample), "/f/plain")
    assert response.status == 200
    assert response.body == CONTENT
    assert response.headers["content-type"] == "text/plain"
    assert response.headers["content-length"] == "100"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["etag"].startswith('"')
    assert response.headers["last-modified"].endswith("GMT")


async def test_a_missing_file_is_404(sample: Path) -> None:
    response = await call(file_controller(sample), "/f/missing")
    assert response.status == 404
    assert response.json()["detail"] == "file not found"


async def test_a_directory_is_not_a_file(tmp_path: Path) -> None:
    with pytest.raises(HTTPError) as info:
        FileResponse(tmp_path)
    assert info.value.status == 404


async def test_a_filename_becomes_a_content_disposition(sample: Path) -> None:
    response = await call(file_controller(sample), "/f/named")
    assert response.headers["content-disposition"] == 'attachment; filename="report.txt"'


async def test_the_disposition_can_be_inline(sample: Path) -> None:
    response = await call(file_controller(sample), "/f/inline")
    assert response.headers["content-disposition"] == 'inline; filename="rep ort.txt"'


# -- conditional requests ---------------------------------------------------


async def test_a_matching_etag_is_304(sample: Path) -> None:
    controller = file_controller(sample)
    first = await call(controller, "/f/conditional")
    response = await call(
        controller, "/f/conditional", headers={"if-none-match": first.headers["etag"]}
    )
    assert response.status == 304
    assert response.body == b""
    assert "content-length" not in response.headers


async def test_a_weak_etag_still_matches(sample: Path) -> None:
    controller = file_controller(sample)
    etag = (await call(controller, "/f/conditional")).headers["etag"]
    response = await call(controller, "/f/conditional", headers={"if-none-match": f"W/{etag}"})
    assert response.status == 304


async def test_a_star_etag_is_304(sample: Path) -> None:
    response = await call(file_controller(sample), "/f/conditional", headers={"if-none-match": "*"})
    assert response.status == 304


async def test_a_stale_etag_sends_the_file_again(sample: Path) -> None:
    response = await call(
        file_controller(sample), "/f/conditional", headers={"if-none-match": '"nope"'}
    )
    assert response.status == 200
    assert response.body == CONTENT


async def test_if_modified_since_is_304(sample: Path) -> None:
    controller = file_controller(sample)
    modified = (await call(controller, "/f/conditional")).headers["last-modified"]
    response = await call(controller, "/f/conditional", headers={"if-modified-since": modified})
    assert response.status == 304


async def test_an_older_if_modified_since_sends_the_file(sample: Path) -> None:
    response = await call(
        file_controller(sample),
        "/f/conditional",
        headers={"if-modified-since": "Thu, 01 Jan 1970 00:00:00 GMT"},
    )
    assert response.status == 200


async def test_an_unparsable_if_modified_since_is_ignored(sample: Path) -> None:
    response = await call(
        file_controller(sample), "/f/conditional", headers={"if-modified-since": "whenever"}
    )
    assert response.status == 200


# -- ranges -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected", "content_range"),
    [
        ("bytes=0-9", CONTENT[:10], "bytes 0-9/100"),
        ("bytes=10-19", CONTENT[10:20], "bytes 10-19/100"),
        ("bytes=90-", CONTENT[90:], "bytes 90-99/100"),
        ("bytes=-5", CONTENT[-5:], "bytes 95-99/100"),
        ("bytes=0-1000", CONTENT, "bytes 0-99/100"),
    ],
)
async def test_a_range_is_206(
    sample: Path, header: str, expected: bytes, content_range: str
) -> None:
    response = await call(file_controller(sample), "/f/conditional", headers={"range": header})
    assert response.status == 206
    assert response.body == expected
    assert response.headers["content-range"] == content_range
    assert response.headers["content-length"] == str(len(expected))


@pytest.mark.parametrize("header", ["bytes=100-200", "bytes=-0", "bytes=500-"])
async def test_an_unsatisfiable_range_is_416(sample: Path, header: str) -> None:
    response = await call(file_controller(sample), "/f/conditional", headers={"range": header})
    assert response.status == 416
    assert response.headers["content-range"] == "bytes */100"


@pytest.mark.parametrize("header", ["items=0-9", "bytes=0-9, 20-29", "bytes=abc", "bytes=0"])
async def test_a_range_we_do_not_answer_sends_the_whole_file(sample: Path, header: str) -> None:
    """Several ranges, or a malformed one, are answered with everything."""
    response = await call(file_controller(sample), "/f/conditional", headers={"range": header})
    assert response.status == 200
    assert response.body == CONTENT


async def test_if_range_with_a_matching_etag_still_gives_206(sample: Path) -> None:
    controller = file_controller(sample)
    etag = (await call(controller, "/f/conditional")).headers["etag"]
    response = await call(
        controller, "/f/conditional", headers={"range": "bytes=0-4", "if-range": etag}
    )
    assert response.status == 206


async def test_if_range_with_a_stale_etag_gives_the_whole_file(sample: Path) -> None:
    response = await call(
        file_controller(sample),
        "/f/conditional",
        headers={"range": "bytes=0-4", "if-range": '"stale"'},
    )
    assert response.status == 200
    assert response.body == CONTENT


def test_an_empty_file_has_no_satisfiable_suffix_range() -> None:
    assert _parse_range("bytes=-5", 0) is not None
    assert _parse_range("bytes=-5", 0) != (0, -1)


def test_no_range_header_parses_to_nothing() -> None:
    assert _parse_range(None, 100) is None
    assert _parse_range("", 100) is None
