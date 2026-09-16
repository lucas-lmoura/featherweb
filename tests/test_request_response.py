"""Unit tests for the request and response objects."""

from __future__ import annotations

import gzip
from collections.abc import Mapping
from typing import Any

import pytest

from featherweb import HTTPError, Request, Response
from featherweb._types import Message, Scope
from featherweb.request import ClientDisconnected, Headers, QueryParams
from featherweb.response import MutableHeaders, RedirectResponse


def make_request(
    *,
    method: str = "GET",
    path: str = "/",
    query: bytes = b"",
    headers: Mapping[str, str] | None = None,
    chunks: list[bytes] | None = None,
    disconnect: bool = False,
    max_body_size: int = 1024 * 1024,
) -> Request:
    scope: Scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "root_path": "",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": ("10.0.0.1", 5000),
        "server": ("testserver", 80),
    }
    pending: list[Message] = []
    for index, chunk in enumerate(chunks or []):
        pending.append(
            {"type": "http.request", "body": chunk, "more_body": index < len(chunks or []) - 1}
        )
    if disconnect:
        pending.append({"type": "http.disconnect"})
    elif not pending:
        pending.append({"type": "http.request", "body": b"", "more_body": False})

    async def receive() -> Message:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    return Request(scope, receive, max_body_size=max_body_size)


# -- headers and query ------------------------------------------------------


def test_headers_are_case_insensitive() -> None:
    headers = Headers([(b"content-type", b"text/plain"), (b"x-a", b"1"), (b"x-a", b"2")])
    assert headers["Content-Type"] == "text/plain"
    assert headers.get("CONTENT-TYPE") == "text/plain"
    assert headers.getlist("x-a") == ["1", "2"]
    assert headers["x-a"] == "1"
    assert "x-a" in headers
    assert sorted(headers) == ["content-type", "x-a"]
    assert len(headers) == 2
    assert "Headers(" in repr(headers)


def test_missing_header() -> None:
    headers = Headers([])
    with pytest.raises(KeyError):
        headers["nope"]
    assert headers.get("nope", "fallback") == "fallback"


def test_query_params() -> None:
    query = QueryParams(b"a=1&b=2&a=3&empty=")
    assert query["a"] == "1"
    assert query.getlist("a") == ["1", "3"]
    assert query["empty"] == ""
    assert sorted(query) == ["a", "b", "empty"]
    assert len(query) == 3
    assert query.multi_items() == [("a", "1"), ("b", "2"), ("a", "3"), ("empty", "")]
    with pytest.raises(KeyError):
        query["missing"]


# -- request ----------------------------------------------------------------


def test_request_basics() -> None:
    request = make_request(
        method="POST", path="/users", query=b"page=2", headers={"host": "example.com"}
    )
    assert request.method == "POST"
    assert request.path == "/users"
    assert request.scheme == "http"
    assert request.http_version == "1.1"
    assert request.client == ("10.0.0.1", 5000)
    assert request.query["page"] == "2"
    assert request.url == "http://example.com/users?page=2"
    assert repr(request) == "<Request POST /users>"


def test_request_cookies() -> None:
    request = make_request(headers={"cookie": 'a=1; b="two"; broken; c=with%20space'})
    assert dict(request.cookies) == {"a": "1", "b": "two", "c": "with space"}


async def test_body_is_read_once_and_cached() -> None:
    request = make_request(chunks=[b"hello ", b"world"])
    assert await request.body() == b"hello world"
    assert await request.body() == b"hello world"


async def test_stream_yields_chunks() -> None:
    request = make_request(chunks=[b"a", b"b", b"c"])
    assert [chunk async for chunk in request.stream()] == [b"a", b"b", b"c"]


async def test_stream_after_body_replays_the_buffer() -> None:
    request = make_request(chunks=[b"ab"])
    await request.body()
    assert [chunk async for chunk in request.stream()] == [b"ab"]


async def test_stream_cannot_be_consumed_twice() -> None:
    request = make_request(chunks=[b"ab"])
    assert [chunk async for chunk in request.stream()] == [b"ab"]
    with pytest.raises(RuntimeError, match="already been consumed"):
        [chunk async for chunk in request.stream()]


async def test_disconnect_while_reading_the_body() -> None:
    request = make_request(disconnect=True)
    with pytest.raises(ClientDisconnected):
        await request.body()


async def test_body_over_the_limit() -> None:
    request = make_request(chunks=[b"x" * 100], max_body_size=10)
    with pytest.raises(HTTPError) as info:
        await request.body()
    assert info.value.status == 413


async def test_json_body() -> None:
    request = make_request(chunks=[b'{"a": 1}'])
    assert await request.json() == {"a": 1}


async def test_malformed_json_body() -> None:
    request = make_request(chunks=[b"{"])
    with pytest.raises(HTTPError) as info:
        await request.json()
    assert info.value.status == 400


async def test_urlencoded_form() -> None:
    request = make_request(
        chunks=[b"a=1&a=2&b=x"],
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    form = await request.form()
    assert form.getlist("a") == ["1", "2"]
    assert form["b"] == "x"


async def test_multipart_is_not_supported_yet() -> None:
    request = make_request(headers={"content-type": "multipart/form-data; boundary=x"})
    with pytest.raises(HTTPError) as info:
        await request.form()
    assert info.value.status == 415


async def test_form_refuses_other_content_types() -> None:
    request = make_request(headers={"content-type": "application/json"})
    with pytest.raises(HTTPError) as info:
        await request.form()
    assert info.value.status == 415


# -- response ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "body", "media_type"),
    [
        ("text", b"text", "text/plain; charset=utf-8"),
        (b"raw", b"raw", "application/octet-stream"),
        ({"a": 1}, b'{"a":1}', "application/json"),
        ([1, 2], b"[1,2]", "application/json"),
        (None, b"", None),
    ],
)
def test_rendering(content: Any, body: bytes, media_type: str | None) -> None:
    response = Response(content)
    assert response.render() == body
    assert response.media_type == media_type


def test_media_type_can_be_forced() -> None:
    response = Response("<p>hi</p>", media_type="text/html; charset=utf-8")
    assert response.raw_headers() == [
        (b"content-type", b"text/html; charset=utf-8"),
        (b"content-length", b"9"),
    ]


def test_bodyless_statuses_carry_no_length() -> None:
    assert Response(None, status=204).raw_headers() == []
    assert Response(None, status=304).raw_headers() == []


def test_application_headers_are_kept() -> None:
    response = Response("x", headers={"X-One": "1"})
    assert (b"x-one", b"1") in response.raw_headers()


def test_set_body_updates_the_length() -> None:
    response = Response("hello")
    compressed = gzip.compress(b"hello")
    response.set_body(compressed)
    assert response.render() == compressed
    assert dict(response.raw_headers())[b"content-length"] == str(len(compressed)).encode()


def test_cookies() -> None:
    response = Response(None)
    response.set_cookie("sid", "abc", max_age=60, httponly=True, secure=True, path="/x")
    response.set_cookie("plain", "1", samesite=None)
    cookies = [value for name, value in response.raw_headers() if name == b"set-cookie"]
    assert cookies[0] == b"sid=abc; Max-Age=60; Path=/x; Secure; HttpOnly; SameSite=Lax"
    assert cookies[1] == b"plain=1; Path=/"


def test_delete_cookie() -> None:
    response = Response(None)
    response.delete_cookie("sid")
    cookie = dict(response.raw_headers())[b"set-cookie"].decode()
    assert "Max-Age=0" in cookie
    assert "Expires=Thu, 01 Jan 1970" in cookie


def test_samesite_none_needs_secure() -> None:
    response = Response(None)
    with pytest.raises(ValueError, match="requires secure"):
        response.set_cookie("sid", "abc", samesite="none")


def test_invalid_samesite() -> None:
    response = Response(None)
    with pytest.raises(ValueError, match="invalid samesite"):
        response.set_cookie("sid", "abc", samesite="sideways")  # type: ignore[arg-type]


def test_redirect_response_quotes_the_location() -> None:
    response = RedirectResponse("/a b?x=1")
    assert response.status == 307
    assert dict(response.raw_headers())[b"location"] == b"/a%20b?x=1"


def test_response_repr() -> None:
    assert repr(Response("x", status=201)) == "<Response 201>"


# -- mutable headers --------------------------------------------------------


def test_mutable_headers() -> None:
    headers = MutableHeaders({"X-A": "1"})
    headers["x-b"] = "2"
    headers.append("x-b", "3")
    assert headers["x-a"] == "1"
    assert headers.getlist("x-b") == ["2", "3"]
    headers["x-b"] = "only"
    assert headers.getlist("x-b") == ["only"]
    del headers["x-a"]
    assert "x-a" not in headers
    assert len(headers) == 1
    assert headers.raw() == [(b"x-b", b"only")]
    assert "MutableHeaders(" in repr(headers)


def test_deleting_a_missing_header() -> None:
    headers = MutableHeaders()
    with pytest.raises(KeyError):
        del headers["nope"]
