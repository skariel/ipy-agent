"""Shared retry decisions for model requests only; never for executed cells."""
from __future__ import annotations

from dataclasses import dataclass
import math
import random

from .provider import ProviderError

MAX_MODEL_ATTEMPTS = 6
TRANSIENT_KINDS = frozenset({"transport", "rate_limit", "server", "provider", "timeout"})


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    attempts: int
    delay: float


def model_retry(error: BaseException, attempt: int, *, jitter: float | None = None) -> RetryDecision:
    """Decide after a zero-based attempt; hints and jitter are bounded."""
    if type(attempt) is not int or attempt < 0:
        raise ValueError("attempt must be a nonnegative integer")
    kind = getattr(error, "kind", None)
    attempts = MAX_MODEL_ATTEMPTS if kind == "transport" else 3
    retry = isinstance(error, ProviderError) and kind in TRANSIENT_KINDS and attempt + 1 < attempts
    if not retry:
        return RetryDecision(False, attempts, 0.0)
    delay = (1.0, 2.0, 4.0, 8.0, 15.0)[attempt] if kind == "transport" else 0.5 * (attempt + 1)
    hint = getattr(error, "retry_after", None)
    if isinstance(hint, (int, float)) and not isinstance(hint, bool) and math.isfinite(hint):
        delay = max(delay, min(60.0, max(0.0, hint)))
    factor = random.uniform(0.0, 0.2) if jitter is None else jitter
    if not math.isfinite(factor) or not 0.0 <= factor <= 0.2:
        raise ValueError("retry jitter must be from 0 to 0.2")
    return RetryDecision(True, attempts, min(60.0, delay * (1.0 + factor)))
