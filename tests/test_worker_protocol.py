from __future__ import annotations

import pytest

from py_agent.worker_protocol import MAX_FRAME, ProtocolError, decode_frame, encode_frame


@pytest.mark.parametrize("payload", [
    b"", b"{", b"[]", b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":Infinity}',
    b'{"a":-Infinity}', b'{"a":1e400}', b'{"a":-1e400}', b"\xff", b"x" * (MAX_FRAME + 1), b"[" * 2000,
])
def test_reject_malformed_payload(payload):
    with pytest.raises(ProtocolError):
        decode_frame(payload)


def test_round_trip():
    message = {"kind": "result", "text": "hello \u2603", "values": [1, True, None]}
    frame = encode_frame(message)
    assert int.from_bytes(frame[:4], "big") == len(frame) - 4
    assert decode_frame(frame[4:]) == message


@pytest.mark.parametrize("message", [[], {"a": float("nan")}, {"a": object()}, {"a": "x" * MAX_FRAME}])
def test_encode_rejects_invalid(message):
    with pytest.raises(ProtocolError):
        encode_frame(message)
