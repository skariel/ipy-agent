"""Archive reads stay bounded and retain original provenance across aging."""
from __future__ import annotations

from dataclasses import replace

import pytest

from py_agent.contracts import ExecutionOutput, ExecutionRequest, ExecutionResult, InputReply, Origin
from py_agent.coordinator import Coordinator
from py_agent.local_executor import LocalExecutor
from py_agent.production_services import ProductionContextAdapter, ProductionObservationAdapter


def request(source):
    return ExecutionRequest(Origin("session", "request", "test", 1, "gen", "exec"), source, "agent")


def pack(req, result):
    coordinator = Coordinator.__new__(Coordinator)
    coordinator.observations = ProductionObservationAdapter()
    return coordinator._packed_observation(req, result)


def contents(adapter):
    return [message["content"] for message in adapter.provider_messages(adapter.snapshot())]


@pytest.mark.asyncio
async def test_archive_read_is_direct_and_ages_to_original_reference_without_copying():
    executor = LocalExecutor()
    await executor.start()
    try:
        original = "abcdefghij" * 1000
        large = await executor.execute(request(f"print({original!r}, end='')"))
        assert large.output_reference == 1
        req = request("read_output(1, start=100, limit=4000)")
        result = await executor.execute(req)
        assert result.status == "success"
        assert result.output_reference is None
        assert result.stdout == ""
        display, = result.output_events
        assert display.kind == "display"
        assert display.data["text/plain"] == "outputs[1] chars 100:4100 of 10000\n" + original[100:4100]
        observation = pack(req, result)
        assert observation["output"] == ""
        assert len(observation["_output_reads"]) == 1
        adapter = ProductionContextAdapter()
        adapter.prepare_request("inspect archive", "request")
        adapter.commit_response("request", req.source, observation=observation)
        assert display.data["text/plain"] in contents(adapter)
        for number in range(19):
            adapter.commit_response("request", f"print({number})", observation={"output": str(number)})
        saved = []

        async def store(texts):
            saved.extend(texts)
            return await executor.store_outputs(texts)

        assert await adapter.archive_execution_outputs(store) == 10
        assert saved == [str(n) for n in range(9)]
        aged = contents(adapter)
        assert display.data["text/plain"] not in aged
        assert any("outputs[1] chars 100:4100 of 10000; retrieve with read_output(1, start=100, limit=4000)" in text for text in aged)
        reread = await executor.execute(req)
        assert reread.output_events[0].data == display.data
        keys = await executor.execute(request("print(sorted(outputs))"))
        assert keys.stdout == str(list(range(1, 11))) + "\n"
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_read_survives_unrelated_oversized_stdout_and_failure():
    executor = LocalExecutor()
    await executor.start()
    try:
        await executor.store_outputs(("original archive",))
        req = request("read_output(1); print('x' * 9000); raise ValueError('after reading')")
        result = await executor.execute(req)
        assert result.status == "error"
        assert result.output_reference == 2
        observation = pack(req, result)
        assert observation["_stored_output_index"] == 2
        assert observation["_output_reads"][0]["text"].endswith("original archive")
        assert "after reading" in observation["output"]
        adapter = ProductionContextAdapter()
        adapter.commit_response("request", req.source, observation=observation)
        assert any(text.endswith("original archive") for text in contents(adapter))
        assert any("outputs[2]" in text for text in contents(adapter))
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_worker_read_validation_budget_and_stale_helper():
    executor = LocalExecutor()
    await executor.start()
    try:
        await executor.store_outputs(("x" * 10000, ""))
        for source in (
            "read_output(1, limit=4001)", "read_output(1, limit=0)",
            "read_output(True)", "read_output(1, start=-1)",
            "read_output(999)", "read_output(1, limit=1.5)",
        ):
            result = await executor.execute(request(source))
            assert result.status == "error", source
            assert not any(event.kind == "display" for event in result.output_events), source
        req = request("read_output(1); read_output(1); read_output(1, limit=1)")
        result = await executor.execute(req)
        assert result.status == "error" and "cell budget exceeded" in result.error
        assert sum(event.kind == "display" for event in result.output_events) == 2
        assert len(pack(req, result)["_output_reads"]) == 1
        eof = await executor.execute(request("read_output(2, start=100)"))
        assert eof.output_events[0].data["text/plain"] == "outputs[2] chars 0:0 of 0\n"
        await executor.execute(request("old_read = read_output"))
        stale = await executor.execute(request("old_read(1)"))
        assert stale.status == "error" and "completed cell" in stale.error
        current = await executor.execute(request("read_output(1, limit=1)"))
        assert current.status == "success"
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_read_only_aging_does_not_call_storage_and_failed_mixed_batch_is_atomic():
    adapter = ProductionContextAdapter()
    observation = {"output": "", "_output_reads": [{
        "text": "excerpt", "reference": "outputs[7] chars 0:7 of 123",
    }]}
    for _ in range(20):
        adapter.commit_response("r", "read_output(7, limit=7)", observation=observation)

    async def fail(_texts):
        raise RuntimeError("storage unavailable")

    assert await adapter.archive_execution_outputs(fail) == 10
    assert contents(adapter).count("excerpt") == 10
    assert sum("outputs[7] chars 0:7" in text for text in contents(adapter)) == 10
    adapter.commit_response("r", "print('ordinary')", observation={"output": "ordinary"})
    for _ in range(19):
        adapter.commit_response("r", "read_output(7, limit=7)", observation=observation)
    assert await adapter.archive_execution_outputs(fail) == 10
    before = contents(adapter)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await adapter.archive_execution_outputs(fail)
    assert contents(adapter) == before


@pytest.mark.parametrize("raw", ["x" * 8000, '\\"\n\t' * 2000, "é" * 8000, "😀" * 8000])
@pytest.mark.asyncio
async def test_boundary_text_survives_coordinator_context_and_worker_archival(raw):
    req = request("print(text)")
    result = ExecutionResult(req.origin, "success", stdout=raw)
    observation = pack(req, result)
    assert observation == {"output": "stdout:\n" + raw}
    executor = LocalExecutor()
    await executor.start()
    try:
        adapter = ProductionContextAdapter()
        for _ in range(20):
            adapter.commit_response("request", req.source, observation=observation)
        assert await adapter.archive_execution_outputs(executor.store_outputs) == 10
        retrieved = await executor.execute(request("read_output(1, limit=20)"))
        assert retrieved.output_events[0].data["text/plain"].endswith(("stdout:\n" + raw)[:20])
    finally:
        await executor.close()


def test_malformed_read_metadata_does_not_bypass_regular_output_bounds():
    req = request("render()")
    result = ExecutionResult(req.origin, "success", output_events=(ExecutionOutput(
        "display", {"text/plain": "safe"}, metadata={
            "py_agent_output_read": {"index": True, "start": 0, "end": 4, "total": 4},
        },
    ),))
    assert pack(req, result) == {"output": "Out:\nsafe"}
    with pytest.raises(RuntimeError, match="different execution origin"):
        pack(req, replace(result, origin=replace(req.origin, session_id="foreign")))


@pytest.mark.asyncio
async def test_read_budget_after_password_redaction_and_password_learned_after_read():
    executor = LocalExecutor()
    await executor.start()

    async def answer(item):
        return InputReply(item.origin, item.sequence, item.owner_frontend_id,
                          value="zz", password=True)

    try:
        await executor.store_outputs(("zz" * 2000,))
        req = replace(request("read_output(1); import getpass; pw = getpass.getpass()"),
                      allow_stdin=True, input_handler=answer)
        result = await executor.execute(req)
        assert result.status == "success"
        read, = pack(req, result)["_output_reads"]
        assert len(read["text"]) <= 4032
        assert "zz" not in read["text"]
        assert "Redacted excerpt shortened" in read["text"]
        assert "outputs[1] chars 0:4000 of 4000" in read["reference"]

        # Once known, expansion is checked before any excerpt is emitted.
        over = await executor.execute(request("read_output(1)"))
        assert over.status == "error" and "budget exceeded after redaction" in over.error
        assert not any(event.kind == "display" for event in over.output_events)
        small_req = request("read_output(1, limit=100)")
        small = await executor.execute(small_req)
        assert small.status == "success"
        read, = pack(small_req, small)["_output_reads"]
        assert "zz" not in read["text"] and "[REDACTED]" in read["text"]
        assert "chars 0:100 of 4000" in read["reference"]
    finally:
        await executor.close()


@pytest.mark.parametrize("secret", ["py_agent_output_read", "start", "plain", "index"])
@pytest.mark.asyncio
async def test_password_matching_protocol_keys_cannot_erase_read_provenance(secret):
    executor = LocalExecutor()
    await executor.start()

    async def answer(item):
        return InputReply(item.origin, item.sequence, item.owner_frontend_id,
                          value=secret, password=True)

    try:
        await executor.store_outputs(("safe archive",))
        req = replace(request("import getpass; pw = getpass.getpass(); read_output(1)"),
                      allow_stdin=True, input_handler=answer)
        result = await executor.execute(req)
        assert result.status == "success"
        read, = pack(req, result)["_output_reads"]
        assert read["text"].endswith("safe archive")
        assert "outputs[1] chars 0:12 of 12" in read["reference"]
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_maximum_formatted_archive_batch_fits_transport_with_astral_escapes():
    executor = LocalExecutor()
    await executor.start()
    try:
        assert await executor.store_outputs(("😀" * 16000,) * 10) == tuple(range(1, 11))
        result = await executor.execute(request("read_output(10, start=15990)"))
        assert result.status == "success"
        assert result.output_events[0].data["text/plain"] == "outputs[10] chars 15990:16000 of 16000\n" + "😀" * 10
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_two_full_excerpts_fit_budget_excluding_headers():
    executor = LocalExecutor()
    await executor.start()
    try:
        await executor.execute(request("outputs[1] = 'x' * 12000"))
        result = await executor.execute(request(
            "read_output(1, limit=4000); read_output(1, start=4000, limit=4000)"
        ))
        assert result.status == "success"
        assert len([e for e in result.output_events if e.kind == "display"]) == 2
        over = await executor.execute(request(
            "read_output(1); read_output(1, start=4000); read_output(1, limit=1)"
        ))
        assert over.status == "error"
    finally:
        await executor.close()
