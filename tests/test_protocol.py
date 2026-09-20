"""Transport/schema tests; no sandbox or provider involved."""
import asyncio
import io
import json

import pytest

from py_agent.protocol import (
    HOST_TYPES, WORKER_TYPES, ProtocolError, decode_frame,
    encode_frame, read_frame, read_frame_sync, validate_frame, write_frame,
    write_frame_sync,
)

OLD_FRAME_LIMIT = 65536  # Regression threshold, not a production quota.

FRAMES = [
    {"v": 1, "type": "execute", "cell_id": "a1:c1", "source": "print('λ')"},
    {"v": 1, "type": "ready", "kernel_pid": 42},
    {"v": 1, "type": "output", "cell_id": "a1:c1", "stream": "display", "text": "λ😀\n"},
    {"v": 1, "type": "say", "cell_id": "a1:c1", "content": {"ok": [True, None, 1.5]}, "final": True},
    {"v": 1, "type": "broker_request", "cell_id": "a1:c1", "request_id": "r1", "method": "recent", "args": {"n": 10}},
    {"v": 1, "type": "broker_request", "cell_id": "a1:c1", "request_id": "r2", "method": "search", "args": {"query": "foo", "kind": None, "limit": 20}},
    {"v": 1, "type": "broker_request", "cell_id": "a1:c1", "request_id": "r3", "method": "read", "args": {"event_or_cell_id": "a1:c1", "offset": 0, "limit": 8000}},
    {"v": 1, "type": "broker_response", "cell_id": "a1:c1", "request_id": "r1", "result": {"content": "hello"}},
    {"v": 1, "type": "broker_response", "cell_id": "a1:c1", "request_id": "r2", "error": "Not found"},
    {"v": 1, "type": "cell_end", "cell_id": "a1:c1", "status": "success", "execution_count": 1},
    {"v": 1, "type": "cell_end", "cell_id": "a1:c2", "status": "error", "execution_count": 2, "error": "ValueError: oops"},
]


@pytest.mark.parametrize("frame", FRAMES)
def test_sync_roundtrip(frame):
    stream = io.BytesIO()
    write_frame_sync(stream, frame)
    assert stream.getvalue().count(b"\n") == 1
    stream.seek(0)
    assert read_frame_sync(stream) == frame


def test_async_roundtrip_and_fragmentation():
    async def run():
        reader = asyncio.StreamReader(limit=32)
        raw = encode_frame(FRAMES[2])
        task = asyncio.create_task(read_frame(reader))
        reader.feed_data(raw[:10])
        await asyncio.sleep(0)
        assert not task.done()
        reader.feed_data(raw[10:])
        assert await task == FRAMES[2]
        class Writer:
            def write(self, value):
                self.data = value
            async def drain(self):
                self.drained = True
        writer = Writer()
        await write_frame(writer, FRAMES[0])
        assert writer.data == encode_frame(FRAMES[0]) and writer.drained
        reader.feed_data(encode_frame(FRAMES[0]) + encode_frame(FRAMES[1]))
        assert await read_frame(reader) == FRAMES[0]
        assert await read_frame(reader) == FRAMES[1]
    asyncio.run(run())


@pytest.mark.parametrize("frame", [
    {}, {"v": True, "type": "ready", "kernel_pid": 1},
    {"v": 2, "type": "ready", "kernel_pid": 1},
    {"v": 1, "type": "ready", "kernel_pid": False},
    {**FRAMES[0], "source": " "}, {**FRAMES[0], "cell_id": "../file"},
    {**FRAMES[0], "extra": "no"}, {**FRAMES[0], "type": "eval_host"},
    {**FRAMES[1], "kernel_pid": -1}, {**FRAMES[2], "stream": "commands"},
    {**FRAMES[3], "final": 1}, {**FRAMES[3], "content": object()},
    {**FRAMES[3], "content": {1: "non-string key"}},
    {**FRAMES[3], "content": float("nan")},
    {**FRAMES[3], "content": float("inf")},
    {**FRAMES[4], "method": "open"}, {**FRAMES[4], "args": {"n": True}},
    {**FRAMES[4], "args": {"n": -1}}, {**FRAMES[4], "args": {"path": "/secret"}},
    {**FRAMES[5], "args": {"query": 0}},
    {**FRAMES[6], "args": {"event_or_cell_id": "../../etc/passwd"}},
    {**FRAMES[7], "error": "both"},
    {"v": 1, "type": "broker_response", "cell_id": "a1:c1", "request_id": "r1"},
    {**FRAMES[8], "error": {"message": "no"}},
    {**FRAMES[9], "status": "final"}, {**FRAMES[9], "execution_count": True},
])
def test_reject_schema(frame):
    with pytest.raises(ProtocolError):
        encode_frame(frame)


def test_directions_are_explicit():
    with pytest.raises(ProtocolError):
        decode_frame(encode_frame(FRAMES[0]), allowed_types=WORKER_TYPES)
    with pytest.raises(ProtocolError):
        validate_frame(FRAMES[1], allowed_types=HOST_TYPES)


@pytest.mark.parametrize("raw", [
    b"{}", b"not json\n", b"[1]\n", b"\xff\n",
    b'{"v":1,"v":1,"type":"ready","kernel_pid":1}\n',
    b'{"v":1,"type":"say","cell_id":"c1","content":NaN,"final":false}\n',
    b'{"v":1,"type":"say","cell_id":"c1","content":1e999,"final":false}\n',
    b"x" * OLD_FRAME_LIMIT + b"\n",
])
def test_bad_wire(raw):
    with pytest.raises(ProtocolError):
        decode_frame(raw)


@pytest.mark.parametrize("source", ["# " + "x" * 100000, "# " + "🐍" * 10000])
def test_source_larger_than_old_wire_limit_roundtrips(source):
    frame = {**FRAMES[0], "source": source}
    encoded = encode_frame(frame)
    assert len(encoded) > OLD_FRAME_LIMIT
    assert decode_frame(encoded) == frame
    assert read_frame_sync(io.BytesIO(encoded)) == frame


def test_structural_nesting_guard_remains():
    value = []
    for _ in range(65):
        value = [value]
    with pytest.raises(ProtocolError):
        encode_frame({**FRAMES[3], "content": value})


def test_async_eof_and_truncation_still_rejected():
    async def run():
        for raw, error in [(b"", EOFError), (b"{", ProtocolError), (b"x" * (OLD_FRAME_LIMIT + 1), ProtocolError)]:
            reader = asyncio.StreamReader(limit=32)
            reader.feed_data(raw)
            reader.feed_eof()
            with pytest.raises(error):
                await read_frame(reader)
    asyncio.run(run())
    with pytest.raises(EOFError):
        read_frame_sync(io.BytesIO())


def test_large_async_frames_ignore_reader_line_limit_and_wait_for_completion():
    async def run():
        reader = asyncio.StreamReader(limit=32)
        frame = {**FRAMES[0], "source": "# " + "🐍" * 20000}
        raw = encode_frame(frame)
        reader.feed_data(raw[:OLD_FRAME_LIMIT + 1])
        task = asyncio.create_task(read_frame(reader))
        await asyncio.sleep(0)
        assert not task.done()  # no fragments are accepted as a complete cell
        reader.feed_data(raw[OLD_FRAME_LIMIT + 1:] + encode_frame(FRAMES[1]))
        reader.feed_eof()
        assert await task == frame
        assert await read_frame(reader) == FRAMES[1]
        with pytest.raises(EOFError):
            await read_frame(reader)
    asyncio.run(run())


def test_sync_large_frame_consumes_only_one_complete_message():
    frame = {**FRAMES[0], "source": "# " + "x" * (OLD_FRAME_LIMIT * 10)}
    raw = encode_frame(frame)
    stream = io.BytesIO(raw + encode_frame(FRAMES[1]))
    assert read_frame_sync(stream) == frame
    assert stream.tell() == len(raw)
    assert read_frame_sync(stream) == FRAMES[1]
