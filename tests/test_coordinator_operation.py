from __future__ import annotations

from types import SimpleNamespace

import pytest

from py_agent.coordinator_operation import Operation


def test_invalidation_retains_side_effect_evidence_but_rejects_late_output():
    operation = Operation()
    operation.begin("first", None)
    request = SimpleNamespace(source="cell")
    operation.begin_execution(request)
    operation.execution_outcome_status = "uncertain"
    operation.invalidate()
    assert not operation.current("first")
    assert operation.execution_request is request
    assert operation.execution_active
    assert operation.execution_outcome_status == "uncertain"


def test_new_submission_resets_evidence_and_old_completion_cannot_release_it():
    operation = Operation()
    old_task, new_task = object(), object()
    operation.begin("old", old_task)
    operation.begin_model(SimpleNamespace(model="old"))
    operation.provider_usage_recorded = True
    operation.invalidate()
    operation.begin("new", new_task)
    operation.finish("old", old_task)
    assert operation.current("new")
    assert operation.task is new_task
    assert operation.model_request is None
    assert not operation.provider_usage_recorded


def test_begin_requires_single_owner():
    operation = Operation()
    operation.begin("one", None)
    with pytest.raises(RuntimeError):
        operation.begin("two", None)


def test_completion_and_new_model_reset_flags():
    operation = Operation()
    operation.begin("one", None)
    operation.begin_model(SimpleNamespace(model="one"))
    operation.provider_usage_recorded = True
    operation.begin_model(SimpleNamespace(model="two"))
    assert not operation.provider_usage_recorded
    operation.finish("one", None)
    assert not operation.current("one")
