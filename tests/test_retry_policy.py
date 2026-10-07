from __future__ import annotations

import pytest

from py_agent.provider import ProviderError
from py_agent.retry_policy import model_retry


@pytest.mark.parametrize("kind,attempts", [
    ("transport", 6), ("rate_limit", 3), ("server", 3), ("provider", 3), ("timeout", 3),
])
def test_transient_budget(kind, attempts):
    for attempt in range(attempts + 1):
        decision = model_retry(ProviderError("safe", kind=kind), attempt, jitter=0)
        assert decision.attempts == attempts
        assert decision.retry == (attempt + 1 < attempts)


@pytest.mark.parametrize("kind", ["auth", "tls", "internal", "response_format", "response_error", "rejected"])
def test_nonretryable(kind):
    assert not model_retry(ProviderError("safe", kind=kind), 0).retry


def test_unknown_exception_never_retries():
    error = RuntimeError("local failure")
    error.kind = "transport"
    assert not model_retry(error, 0).retry


def test_hint_and_jitter_bounds():
    error = ProviderError("safe", kind="rate_limit", retry_after=10)
    assert model_retry(error, 0, jitter=.2).delay == 12
    error.retry_after = float("inf")
    assert model_retry(error, 0, jitter=0).delay == .5
    error.retry_after = 10000
    assert model_retry(error, 0, jitter=.2).delay == 60
    with pytest.raises(ValueError):
        model_retry(error, 0, jitter=1)


@pytest.mark.parametrize("attempt", [-1, True, 1.5])
def test_invalid_attempt(attempt):
    with pytest.raises(ValueError):
        model_retry(ProviderError("safe", kind="transport"), attempt)
