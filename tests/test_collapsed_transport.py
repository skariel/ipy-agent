"""Lossless, chunked collapse archive storage; no model calls."""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from py_agent.contracts import ExecutionRequest, Origin
from py_agent.local_executor import MAX_FRAME, LocalExecutor
from py_agent.local_worker import ProtocolError, _CollapsedStorage, _validate_request


def execution(source: str) -> ExecutionRequest:
    return ExecutionRequest(
        Origin("session", "request", "test", 0, execution_id="exec"), source, "user",
    )


def test_archive_stages_until_final_and_never_reuses_indexes():
    storage = _CollapsedStorage()
    assert storage.append("one", 0, "first\x00", False) is None
    assert storage.archives == {}
    with pytest.raises(ProtocolError, match="out of sequence"):
        storage.append("two", 6, "bad", True)
    with pytest.raises(ProtocolError, match="out of sequence"):
        storage.append("one", 0, "bad", True)
    assert storage.append("one", 6, "\ud800😀", True) == 1
    assert storage.archives[1] == "first\x00\ud800😀"
    del storage.archives[1]
    assert storage.append("two", 0, "", True) == 2
    assert storage.archives[2] == ""


@pytest.mark.parametrize("changes", [
    {"offset": True}, {"offset": -1}, {"text": 3}, {"text": "x" * 32_769},
    {"final": 1}, {"request_id": ""}, {"extra": 0}, {"text": "", "final": False},
])
def test_invalid_archive_frames_rejected(changes):
    frame = dict(type="store_collapsed", version=1, request_id="archive", offset=0,
                 text="ok", final=True)
    frame.update(changes)
    with pytest.raises(ProtocolError):
        _validate_request(frame)


@pytest.mark.asyncio
async def test_large_archives_are_exact_chunked_serialized_and_survive_name_rebinding():
    executor = LocalExecutor()
    await executor.start()
    frames = []
    original_send = executor._send

    async def capture(process, frame):
        assert int.from_bytes(frame[:4], "big") <= MAX_FRAME
        decoded = json.loads(frame[4:])
        if decoded["type"] == "store_collapsed":
            frames.append(decoded)
        await original_send(process, frame)

    executor._send = capture
    # JSON escapes, non-BMP characters, NULs, and secret-like text must be exact.
    text = json.dumps({"originals": "😀\x00\\\napi_key=exact-original-secret " * 40_000},
                      ensure_ascii=False)
    assert len(text.encode()) > MAX_FRAME
    digest = hashlib.sha256(text.encode()).hexdigest()
    try:
        first, second = await asyncio.gather(
            executor.store_collapsed(text), executor.store_collapsed("second"),
        )
        assert (first, second) == (1, 2)
        inspected = await executor.execute(execution(
            "import hashlib\n"
            f"print(hashlib.sha256(collapsed[{first}].encode()).hexdigest())\n"
            "print(collapsed[2])\n"
            "collapsed = None"
        ))
        assert inspected.status == "success"
        assert digest in inspected.stdout
        assert "second" in inspected.stdout
        third = await executor.store_collapsed("")
        assert third == 3
        inspected = await executor.execute(execution(
            "print(sorted(collapsed))\nprint(repr(collapsed[3]))"
        ))
        assert "[1, 2, 3]" in inspected.stdout
        assert "''" in inspected.stdout
        transactions = [frame["request_id"] for frame in frames]
        assert len(set(transactions)) == 3
        # No transaction interleaving despite concurrent callers.
        runs = [value for i, value in enumerate(transactions)
                if i == 0 or transactions[i - 1] != value]
        assert len(runs) == 3
        assert len(frames) > 3
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_invalid_archive_input_rejected_before_worker_access():
    executor = LocalExecutor()
    with pytest.raises(TypeError, match="requires text"):
        await executor.store_collapsed(None)
    with pytest.raises(RuntimeError, match="unavailable"):
        await executor.store_collapsed("valid")


@pytest.mark.asyncio
async def test_malformed_archive_acknowledgement_stops_worker():
    executor = LocalExecutor()
    await executor.start()
    original_receive = executor._receive

    async def corrupt_ack(process):
        frame = await original_receive(process)
        if frame.get("type") == "collapsed_stored":
            frame["index"] = True
        return frame

    executor._receive = corrupt_ack
    try:
        with pytest.raises(RuntimeError, match="Collapsed storage failed"):
            await executor.store_collapsed("original")
        assert executor.process is None or executor.process.returncode is not None
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_archive_cancellation_stops_worker():
    executor = LocalExecutor()
    await executor.start()
    sent = asyncio.Event()
    original_send = executor._send

    async def pause_after_send(process, frame):
        await original_send(process, frame)
        sent.set()
        await asyncio.Event().wait()

    executor._send = pause_after_send
    try:
        task = asyncio.create_task(executor.store_collapsed("x" * 70_000))
        await sent.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert executor.process is None or executor.process.returncode is not None
    finally:
        await executor.close()
