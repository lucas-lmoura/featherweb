"""Unit tests for the HTTP/1.1 parser, malformed and smuggling cases included."""

from __future__ import annotations

import pytest

from featherweb.server.parser import (
    DEFAULT_LIMITS,
    NEED_DATA,
    BodyData,
    EndOfMessage,
    Event,
    HttpParser,
    Limits,
    ParserError,
    RequestHead,
)

GET = b"GET / HTTP/1.1\r\nHost: example.com\r\n\r\n"


def drain(parser: HttpParser) -> list[Event]:
    events: list[Event] = []
    while True:
        event = parser.next_event()
        if event is NEED_DATA:
            return events
        events.append(event)


def parse(raw: bytes, *, limits: Limits = DEFAULT_LIMITS) -> list[Event]:
    parser = HttpParser(limits)
    parser.feed_data(raw)
    return drain(parser)


def head_of(raw: bytes, *, limits: Limits = DEFAULT_LIMITS) -> RequestHead:
    head = parse(raw, limits=limits)[0]
    assert isinstance(head, RequestHead)
    return head


def body_of(events: list[Event]) -> bytes:
    return b"".join(event.data for event in events if isinstance(event, BodyData))


def expect_error(raw: bytes, status: int, *, limits: Limits = DEFAULT_LIMITS) -> ParserError:
    with pytest.raises(ParserError) as info:
        parse(raw, limits=limits)
    assert info.value.status == status
    return info.value


# -- well formed requests ---------------------------------------------------


def test_simple_get() -> None:
    events = parse(GET)
    head = events[0]
    assert isinstance(head, RequestHead)
    assert head.method == "GET"
    assert head.http_version == "1.1"
    assert head.path == "/"
    assert head.query_string == b""
    assert head.headers == [(b"host", b"example.com")]
    assert head.keep_alive is True
    assert head.has_body is False
    assert isinstance(events[1], EndOfMessage)


def test_header_names_are_lowercased_and_values_trimmed() -> None:
    head = head_of(b"GET / HTTP/1.1\r\nHost: x\r\nX-Token:  \tabc \r\n\r\n")
    assert head.headers == [(b"host", b"x"), (b"x-token", b"abc")]


def test_path_and_query_are_split_and_unquoted() -> None:
    head = head_of(b"GET /a%20b/c?x=1&y=%20 HTTP/1.1\r\nHost: x\r\n\r\n")
    assert head.path == "/a b/c"
    assert head.raw_path == b"/a%20b/c"
    assert head.query_string == b"x=1&y=%20"


def test_absolute_form_target() -> None:
    head = head_of(b"GET http://example.com/a?b=1 HTTP/1.1\r\nHost: example.com\r\n\r\n")
    assert head.raw_path == b"/a"
    assert head.query_string == b"b=1"


def test_asterisk_form_target() -> None:
    head = head_of(b"OPTIONS * HTTP/1.1\r\nHost: x\r\n\r\n")
    assert head.path == "*"


def test_leading_empty_line_is_ignored() -> None:
    head = head_of(b"\r\n" + GET)
    assert head.method == "GET"


def test_http_10_defaults_to_close() -> None:
    head = head_of(b"GET / HTTP/1.0\r\n\r\n")
    assert head.http_version == "1.0"
    assert head.keep_alive is False


def test_http_10_keep_alive_opt_in() -> None:
    assert head_of(b"GET / HTTP/1.0\r\nConnection: keep-alive\r\n\r\n").keep_alive is True


def test_connection_close_disables_keep_alive() -> None:
    head = head_of(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: keep-alive, Close\r\n\r\n")
    assert head.keep_alive is False


def test_expect_continue_is_reported() -> None:
    raw = b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\nExpect: 100-continue\r\n\r\n"
    head = head_of(raw)
    assert head.expect_continue is True


def test_expect_continue_ignored_in_http_10() -> None:
    head = head_of(b"POST / HTTP/1.0\r\nContent-Length: 0\r\nExpect: 100-continue\r\n\r\n")
    assert head.expect_continue is False


def test_body_with_content_length() -> None:
    events = parse(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello")
    head = events[0]
    assert isinstance(head, RequestHead)
    assert head.content_length == 5
    assert head.has_body is True
    assert body_of(events) == b"hello"
    assert isinstance(events[-1], EndOfMessage)


def test_body_arriving_in_pieces() -> None:
    parser = HttpParser()
    parser.feed_data(b"POST / HTTP/1.1\r\nHost: x\r\nCon")
    assert drain(parser) == []
    parser.feed_data(b"tent-Length: 5\r\n\r\nhe")
    events = drain(parser)
    assert isinstance(events[0], RequestHead)
    assert body_of(events) == b"he"
    assert not parser.message_complete
    parser.feed_data(b"llo")
    events = drain(parser)
    assert body_of(events) == b"llo"
    assert parser.message_complete


def test_chunked_body_with_extensions_and_trailers() -> None:
    raw = (
        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"5;name=value\r\nhello\r\n"
        b"3\r\n mo\r\n"
        b"0\r\nX-Checksum: abc\r\n\r\n"
    )
    events = parse(raw)
    head = events[0]
    assert isinstance(head, RequestHead)
    assert head.chunked is True
    assert head.content_length is None
    assert body_of(events) == b"hello mo"
    assert isinstance(events[-1], EndOfMessage)


def test_pipelined_requests() -> None:
    parser = HttpParser()
    parser.feed_data(GET + b"GET /second HTTP/1.1\r\nHost: x\r\n\r\n")
    first = drain(parser)
    assert isinstance(first[0], RequestHead)
    assert parser.message_complete
    # The second request stays buffered until the first one is answered.
    assert parser.next_event() is NEED_DATA
    parser.start_next_message()
    second = drain(parser)
    head = second[0]
    assert isinstance(head, RequestHead)
    assert head.path == "/second"


def test_start_next_message_requires_a_complete_message() -> None:
    parser = HttpParser()
    parser.feed_data(b"GET / HTTP/1.1\r\n")
    drain(parser)
    with pytest.raises(RuntimeError):
        parser.start_next_message()


# -- request smuggling ------------------------------------------------------


def test_content_length_and_transfer_encoding_together() -> None:
    expect_error(
        b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 6\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
        400,
    )


def test_duplicate_content_length() -> None:
    raw = b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\nContent-Length: 5\r\n\r\n"
    expect_error(raw, 400)


def test_content_length_list_value() -> None:
    expect_error(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5, 5\r\n\r\n", 400)


@pytest.mark.parametrize("value", [b"+5", b"0x5", b"five", b"-1", b"5 5", b""])
def test_malformed_content_length(value: bytes) -> None:
    expect_error(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: " + value + b"\r\n\r\n", 400)


def test_duplicate_transfer_encoding() -> None:
    expect_error(
        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n",
        400,
    )


@pytest.mark.parametrize("value", [b"gzip", b"identity", b"chunked, chunked", b"gzip, chunked"])
def test_unsupported_transfer_coding(value: bytes) -> None:
    expect_error(b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: " + value + b"\r\n\r\n", 501)


def test_transfer_encoding_rejected_in_http_10() -> None:
    expect_error(b"POST / HTTP/1.0\r\nTransfer-Encoding: chunked\r\n\r\n", 400)


def test_obsolete_line_folding() -> None:
    expect_error(b"GET / HTTP/1.1\r\nHost: x\r\nX-A: 1\r\n  continued\r\n\r\n", 400)


def test_space_before_colon() -> None:
    expect_error(b"GET / HTTP/1.1\r\nHost: x\r\nContent-Length : 5\r\n\r\n", 400)


@pytest.mark.parametrize(
    "raw",
    [
        b"GET / HTTP/1.1\nHost: x\r\n\r\n",
        b"GET / HTTP/1.1\r\nHost: x\n\r\n",
        b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\n",
    ],
)
def test_bare_lf_is_rejected(raw: bytes) -> None:
    expect_error(raw, 400)


def test_missing_host_in_http_11() -> None:
    expect_error(b"GET / HTTP/1.1\r\n\r\n", 400)


def test_duplicate_host() -> None:
    expect_error(b"GET / HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n", 400)


@pytest.mark.parametrize(
    "raw",
    [
        b"GET  / HTTP/1.1\r\n\r\n",
        b"GET /a b HTTP/1.1\r\n\r\n",
        b"GET /\r\n\r\n",
        b"GET / HTTP/1.1 extra\r\n\r\n",
        b"GE\x00T / HTTP/1.1\r\n\r\n",
    ],
)
def test_malformed_request_line(raw: bytes) -> None:
    expect_error(raw, 400)


@pytest.mark.parametrize("version", [b"HTTP/0.9", b"HTTP/2.0", b"HTTP/1.2"])
def test_unsupported_version(version: bytes) -> None:
    expect_error(b"GET / " + version + b"\r\nHost: x\r\n\r\n", 505)


def test_connect_is_not_supported() -> None:
    expect_error(b"CONNECT example.com:443 HTTP/1.1\r\nHost: x\r\n\r\n", 501)


def test_unsupported_target_form() -> None:
    expect_error(b"GET example.com HTTP/1.1\r\nHost: x\r\n\r\n", 400)


@pytest.mark.parametrize("chunk", [b"z\r\n", b"-1\r\n", b"5 \r\n", b"\r\n"])
def test_malformed_chunk_size(chunk: bytes) -> None:
    expect_error(
        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n" + chunk,
        400,
    )


def test_chunk_not_terminated_by_crlf() -> None:
    expect_error(
        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhelloXX\r\n",
        400,
    )


def test_unsupported_expectation() -> None:
    expect_error(b"POST / HTTP/1.1\r\nHost: x\r\nExpect: something\r\n\r\n", 417)


def test_non_ascii_target() -> None:
    expect_error(b"GET /caf\xe9 HTTP/1.1\r\nHost: x\r\n\r\n", 400)


# -- limits -----------------------------------------------------------------


def test_request_line_limit() -> None:
    raw = b"GET /" + b"a" * 9000 + b" HTTP/1.1\r\nHost: x\r\n\r\n"
    expect_error(raw, 414)


def test_header_block_limit() -> None:
    limits = Limits(max_headers_size=256)
    raw = b"GET / HTTP/1.1\r\nHost: x\r\nX-Big: " + b"a" * 300 + b"\r\n\r\n"
    expect_error(raw, 431, limits=limits)


def test_header_count_limit() -> None:
    limits = Limits(max_header_count=4)
    fields = b"".join(b"X-%d: 1\r\n" % index for index in range(10))
    expect_error(b"GET / HTTP/1.1\r\nHost: x\r\n" + fields + b"\r\n", 431, limits=limits)


def test_body_size_limit_with_content_length() -> None:
    limits = Limits(max_body_size=8)
    expect_error(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 9\r\n\r\n", 413, limits=limits)


def test_body_size_limit_with_chunked() -> None:
    limits = Limits(max_body_size=8)
    raw = (
        b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"5\r\nhello\r\n5\r\nworld\r\n0\r\n\r\n"
    )
    expect_error(raw, 413, limits=limits)


# -- end of stream ----------------------------------------------------------


def test_eof_between_requests_is_fine() -> None:
    parser = HttpParser()
    parser.feed_eof()
    assert parser.next_event() is NEED_DATA
    assert parser.idle


def test_eof_in_the_middle_of_a_request() -> None:
    parser = HttpParser()
    parser.feed_data(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhe")
    drain(parser)
    parser.feed_eof()
    with pytest.raises(ParserError) as info:
        parser.next_event()
    assert info.value.status == 400


def test_the_error_is_sticky() -> None:
    parser = HttpParser()
    parser.feed_data(b"GET / HTTP/9.9\r\n\r\n")
    with pytest.raises(ParserError):
        parser.next_event()
    parser.feed_data(GET)
    with pytest.raises(ParserError):
        parser.next_event()
