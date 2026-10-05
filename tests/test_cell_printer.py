"""Fair per-cell previews and real worker integration."""
from __future__ import annotations

import pytest

from py_agent.cell_printer import CellPrinter
from py_agent.contracts import ExecutionRequest, Origin
from py_agent.local_executor import LocalExecutor


def test_shared_budget_keeps_each_call_and_tail():
    printer = CellPrinter()
    for i in range(32):
        printer("HEAD" + "x" * 20000 + "TAIL", label=f"item {i}")
    for _ in range(5):
        printer("ignored")
    text = printer.finish()
    assert len(text) <= 6000
    assert text.count("HEAD") == 32
    assert text.count("TAIL") == 32
    assert "5 additional calls omitted" in text
    assert "line " in text
    assert printer.finish() == ""
    with pytest.raises(RuntimeError):
        printer("stale")


def test_redaction_is_in_budget():
    printer = CellPrinter()
    for _ in range(32):
        printer("secret" * 1000, label="secret")
    text = printer.finish(lambda value: value.replace("secret", "[REDACTED]" * 100))
    assert len(text) <= 6000
    assert "secret" not in text


def test_empty_and_short_calls():
    assert CellPrinter().finish() == ""
    printer = CellPrinter()
    printer("a", "b", sep=":", label="result")
    assert "a:b" in printer.finish()


def request(source):
    return ExecutionRequest(Origin(
        session_id="preview-session", request_id="preview-request", frontend_id="test",
        config_revision=1, generation_id="preview-generation", execution_id="preview-cell",
    ), source, "agent")


@pytest.mark.asyncio
async def test_worker_previews_flush_on_error_and_reset_per_cell():
    executor = LocalExecutor()
    await executor.start()
    try:
        result = await executor.execute(request(
            "saved = preview\npreview('A' * 20000, label='first')\n"
            "preview('B' * 20000, label='second')"
        ))
        assert result.status == "success"
        assert result.output_reference is None
        assert "line 2, first" in result.stdout
        assert "line 3, second" in result.stdout
        assert len(result.stdout) <= 6000
        result = await executor.execute(request("preview('kept')\nraise ValueError('oops')"))
        assert result.status == "error"
        assert "kept" in result.stdout
        result = await executor.execute(request("preview('fresh')"))
        assert "preview 1" in result.stdout
        assert "fresh" in result.stdout
        result = await executor.execute(request("saved('stale')"))
        assert result.status == "error"
    finally:
        await executor.close()
