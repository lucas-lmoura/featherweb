"""RFC 6455 framing, at the level of bytes.

The parser is what a hostile peer talks to first, so most of this file is about
what it refuses: an unmasked frame, a reserved bit, a fragmented control frame,
a close code that is not allowed on the wire.
"""

from __future__ import annotations

import pytest

from featherweb.server.parser import RequestHead
from featherweb.server.ws_protocol import (
    CLOSE_INVALID_PAYLOAD,
    CLOSE_NO_STATUS,
    CLOSE_PROTOCOL_ERROR,
    CLOSE_TOO_LARGE,
    DEFAULT_MAX_MESSAGE_SIZE,
    OP_BINARY,
    OP_CLOSE,
    OP_CONTINUATION,
    OP_PING,
    OP_TEXT,
    Frame,
    FrameParser,
    WebSocketProtocolError,
    accept_token,
    build_frame,
    close_payload,
    handshake_response,
    is_upgrade,
    parse_close,
    subprotocols_of,
)

MASK = b"\x37\xfa\x21\x3d"


def masked(opcode: int, payload: bytes, *, fin: bool = True, mask: bytes = MASK) -> bytes:
    """A client frame: same as ours, but masked and with the mask bit set."""
    head = bytearray(2)
    head[0] = (0x80 if fin else 0x00) | opcode
    length = len(payload)
    if length < 126:
        head[1] = 0x80 | length
    elif length < 1 << 16:
        head[1] = 0x80 | 126
        head += length.to_bytes(2, "big")
    else:
        head[1] = 0x80 | 127
        head += length.to_bytes(8, "big")
    scrambled = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(head) + mask + scrambled


def parse_one(data: bytes, *, max_frame_size: int = DEFAULT_MAX_MESSAGE_SIZE) -> Frame:
    """The one frame in ``data``, which every caller here expects to be whole."""
    parser = FrameParser(max_frame_size=max_frame_size)
    parser.feed_data(data)
    frame = parser.next_frame()
    assert frame is not None, "expected a whole frame"
    return frame


# -- reading frames ---------------------------------------------------------


def test_a_masked_text_frame_is_unmasked() -> None:
    frame = parse_one(masked(OP_TEXT, b"hello"))
    assert (frame.opcode, frame.payload, frame.fin) == (OP_TEXT, b"hello", True)


def test_an_empty_frame_is_a_frame() -> None:
    assert parse_one(masked(OP_TEXT, b"")).payload == b""


@pytest.mark.parametrize("size", [0, 1, 125, 126, 127, 1000, 70000])
def test_every_length_encoding_round_trips(size: int) -> None:
    payload = b"x" * size
    assert parse_one(masked(OP_BINARY, payload)).payload == payload


def test_a_frame_split_across_feeds_is_assembled() -> None:
    """One byte at a time: nothing may come out until the last one arrives."""
    data = masked(OP_TEXT, b"hello world")
    parser = FrameParser()
    for index in range(len(data) - 1):
        parser.feed_data(data[index : index + 1])
        assert parser.next_frame() is None, f"a frame appeared after only {index + 1} bytes"
    parser.feed_data(data[-1:])
    frame = parser.next_frame()
    assert frame is not None
    assert frame.payload == b"hello world"


def test_two_frames_in_one_read_are_both_returned() -> None:
    parser = FrameParser()
    parser.feed_data(masked(OP_TEXT, b"one") + masked(OP_TEXT, b"two"))
    first, second = parser.next_frame(), parser.next_frame()
    assert first is not None
    assert second is not None
    assert (first.payload, second.payload) == (b"one", b"two")
    assert parser.next_frame() is None


# -- what it refuses --------------------------------------------------------


def test_an_unmasked_client_frame_is_refused() -> None:
    """RFC 6455 §5.1: every frame from a client has to be masked."""
    with pytest.raises(WebSocketProtocolError, match="must be masked"):
        parse_one(build_frame(OP_TEXT, b"hello"))


def test_a_reserved_bit_is_refused() -> None:
    data = bytearray(masked(OP_TEXT, b"hi"))
    data[0] |= 0x40  # RSV1, which only an extension we did not negotiate would set
    with pytest.raises(WebSocketProtocolError, match="reserved bits"):
        parse_one(bytes(data))


@pytest.mark.parametrize("opcode", [0x3, 0x7, 0xB, 0xF])
def test_an_unknown_opcode_is_refused(opcode: int) -> None:
    with pytest.raises(WebSocketProtocolError, match="unknown opcode"):
        parse_one(masked(opcode, b""))


def test_an_oversized_control_frame_is_refused() -> None:
    with pytest.raises(WebSocketProtocolError, match="125 bytes"):
        parse_one(masked(OP_PING, b"x" * 126))


def test_a_fragmented_control_frame_is_refused() -> None:
    with pytest.raises(WebSocketProtocolError, match="cannot be fragmented"):
        parse_one(masked(OP_PING, b"x", fin=False))


def test_a_frame_over_the_limit_is_refused() -> None:
    with pytest.raises(WebSocketProtocolError) as info:
        parse_one(masked(OP_BINARY, b"x" * 200), max_frame_size=100)
    assert info.value.code == CLOSE_TOO_LARGE


def test_a_length_with_the_top_bit_set_is_refused() -> None:
    data = bytearray(masked(OP_BINARY, b""))
    data[1] = 0x80 | 127
    data[2:2] = (1 << 63).to_bytes(8, "big")
    with pytest.raises(WebSocketProtocolError, match="top bit"):
        parse_one(bytes(data))


# -- close frames -----------------------------------------------------------


def test_an_empty_close_means_no_status() -> None:
    assert parse_close(b"") == (CLOSE_NO_STATUS, "")


def test_a_close_carries_its_code_and_reason() -> None:
    assert parse_close(close_payload(1001, "going")) == (1001, "going")


def test_a_one_byte_close_is_malformed() -> None:
    with pytest.raises(WebSocketProtocolError, match="one byte"):
        parse_close(b"\x03")


@pytest.mark.parametrize("code", [1005, 1006, 1015, 999, 5000])
def test_a_close_code_that_is_never_sent_is_refused(code: int) -> None:
    with pytest.raises(WebSocketProtocolError, match="not allowed"):
        parse_close(code.to_bytes(2, "big"))


@pytest.mark.parametrize("code", [1000, 1001, 1011, 3000, 4999])
def test_the_codes_a_peer_may_send_are_accepted(code: int) -> None:
    assert parse_close(code.to_bytes(2, "big"))[0] == code


def test_a_close_reason_that_is_not_utf8_is_refused() -> None:
    with pytest.raises(WebSocketProtocolError) as info:
        parse_close(b"\x03\xe8\xff\xfe")
    assert info.value.code == CLOSE_INVALID_PAYLOAD


def test_the_codes_that_mean_nothing_was_sent_carry_no_payload() -> None:
    assert close_payload(CLOSE_NO_STATUS) == b""


def test_a_long_close_reason_is_truncated_to_fit_a_control_frame() -> None:
    assert len(close_payload(1000, "x" * 500)) <= 125


# -- frames we build --------------------------------------------------------


def test_what_the_server_sends_is_never_masked() -> None:
    """§5.1 again, the other way round: a server must not mask."""
    assert build_frame(OP_TEXT, b"hi")[1] & 0x80 == 0


@pytest.mark.parametrize(("size", "marker"), [(10, 10), (300, 126), (70000, 127)])
def test_the_shortest_length_encoding_is_used(size: int, marker: int) -> None:
    assert build_frame(OP_BINARY, b"x" * size)[1] == marker


def test_a_non_final_frame_says_so() -> None:
    assert build_frame(OP_TEXT, b"a", fin=False)[0] & 0x80 == 0


# -- the handshake ----------------------------------------------------------


def head(headers: list[tuple[bytes, bytes]], method: str = "GET") -> RequestHead:
    return RequestHead(
        method=method,
        http_version="1.1",
        path="/ws",
        raw_path=b"/ws",
        query_string=b"",
        headers=headers,
    )


UPGRADE_HEADERS = [
    (b"upgrade", b"websocket"),
    (b"connection", b"Upgrade"),
    (b"sec-websocket-key", b"dGhlIHNhbXBsZSBub25jZQ=="),
    (b"sec-websocket-version", b"13"),
]


def test_the_accept_token_is_the_one_from_the_rfc() -> None:
    """The worked example in RFC 6455 §1.3."""
    assert accept_token(b"dGhlIHNhbXBsZSBub25jZQ==") == b"s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_an_upgrade_request_is_recognised() -> None:
    assert is_upgrade(head(UPGRADE_HEADERS))


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"upgrade", b"h2c"), (b"connection", b"Upgrade")],
        [(b"upgrade", b"websocket")],
    ],
)
def test_anything_else_is_an_ordinary_request(headers: list[tuple[bytes, bytes]]) -> None:
    assert not is_upgrade(head(headers))


def test_a_post_cannot_upgrade() -> None:
    assert not is_upgrade(head(UPGRADE_HEADERS, method="POST"))


def test_the_handshake_answers_101_with_the_accept_token() -> None:
    response = handshake_response(head(UPGRADE_HEADERS))
    assert response.startswith(b"HTTP/1.1 101 Switching Protocols\r\n")
    assert b"sec-websocket-accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n" in response


def test_a_chosen_subprotocol_is_echoed() -> None:
    response = handshake_response(head(UPGRADE_HEADERS), "chat")
    assert b"sec-websocket-protocol: chat\r\n" in response


def test_another_version_is_426_naming_the_one_we_speak() -> None:
    headers = [*UPGRADE_HEADERS[:3], (b"sec-websocket-version", b"8")]
    response = handshake_response(head(headers))
    assert response.startswith(b"HTTP/1.1 426 Upgrade Required")
    assert b"sec-websocket-version: 13" in response


@pytest.mark.parametrize("key", [b"", b"short", b"x" * 24, b"dGhlIHNhbXBsZSBub25jZQ"])
def test_a_key_that_is_not_16_base64_bytes_is_400(key: bytes) -> None:
    headers = [*UPGRADE_HEADERS[:2], (b"sec-websocket-key", key), (b"sec-websocket-version", b"13")]
    assert handshake_response(head(headers)).startswith(b"HTTP/1.1 400")


def test_the_offered_subprotocols_are_listed() -> None:
    headers = [*UPGRADE_HEADERS, (b"sec-websocket-protocol", b"chat, superchat")]
    assert subprotocols_of(head(headers)) == ["chat", "superchat"]


def test_no_subprotocol_header_is_an_empty_list() -> None:
    assert subprotocols_of(head(UPGRADE_HEADERS)) == []


# -- fragmentation, through the cycle ---------------------------------------


def test_continuation_without_a_start_is_a_protocol_error() -> None:
    from featherweb.server.ws_protocol import WebSocketCycle

    cycle = WebSocketCycle(_FakeProtocol(), head(UPGRADE_HEADERS), {"type": "websocket"})
    cycle.feed_data(masked(OP_CONTINUATION, b"orphan"))
    assert cycle.closed


def test_a_new_message_before_the_last_one_finished_is_a_protocol_error() -> None:
    from featherweb.server.ws_protocol import WebSocketCycle

    cycle = WebSocketCycle(_FakeProtocol(), head(UPGRADE_HEADERS), {"type": "websocket"})
    cycle.feed_data(masked(OP_TEXT, b"one", fin=False))
    cycle.feed_data(masked(OP_TEXT, b"two"))
    assert cycle.closed


def test_a_message_over_the_limit_closes_with_1009() -> None:
    from featherweb.server.ws_protocol import WebSocketCycle

    protocol = _FakeProtocol()
    cycle = WebSocketCycle(
        protocol, head(UPGRADE_HEADERS), {"type": "websocket"}, max_message_size=10
    )
    cycle.feed_data(masked(OP_BINARY, b"x" * 50))
    assert cycle.closed
    assert _close_code(protocol.written) == CLOSE_TOO_LARGE


def test_text_that_is_not_utf8_closes_with_1007() -> None:
    from featherweb.server.ws_protocol import WebSocketCycle

    protocol = _FakeProtocol()
    cycle = WebSocketCycle(protocol, head(UPGRADE_HEADERS), {"type": "websocket"})
    cycle.feed_data(masked(OP_TEXT, b"\xff\xfe"))
    assert _close_code(protocol.written) == CLOSE_INVALID_PAYLOAD


def test_an_unmasked_frame_closes_with_1002() -> None:
    from featherweb.server.ws_protocol import WebSocketCycle

    protocol = _FakeProtocol()
    cycle = WebSocketCycle(protocol, head(UPGRADE_HEADERS), {"type": "websocket"})
    cycle.feed_data(build_frame(OP_TEXT, b"unmasked"))
    assert _close_code(protocol.written) == CLOSE_PROTOCOL_ERROR


def test_a_ping_is_answered_with_a_pong_carrying_the_same_payload() -> None:
    from featherweb.server.ws_protocol import OP_PONG, WebSocketCycle

    protocol = _FakeProtocol()
    cycle = WebSocketCycle(protocol, head(UPGRADE_HEADERS), {"type": "websocket"})
    cycle.feed_data(masked(OP_PING, b"beat"))
    answer = protocol.written[-1]
    assert answer[0] & 0x0F == OP_PONG
    assert answer[2:] == b"beat"


class _FakeProtocol:
    """Just enough of the connection for the cycle to talk to."""

    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def close(self) -> None:
        self.closed = True


def _close_code(written: list[bytes]) -> int:
    for data in reversed(written):
        if data and data[0] & 0x0F == OP_CLOSE:
            return int.from_bytes(data[2:4], "big")
    raise AssertionError("no close frame was sent")
