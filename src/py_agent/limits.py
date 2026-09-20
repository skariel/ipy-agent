"""Context capacity and explicit provider/retry configuration, not work quotas."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    input_tokens: int = 272000
    output_tokens: int | None = None
    generation_retries: int = 2

    def __post_init__(self):
        if type(self.input_tokens) is not int or self.input_tokens <= 0:
            raise ValueError("input_tokens must be a positive integer")
        if self.output_tokens is not None and (type(self.output_tokens) is not int or self.output_tokens <= 0):
            raise ValueError("output_tokens must be a positive integer or None")
        if type(self.generation_retries) is not int or self.generation_retries < 0:
            raise ValueError("generation_retries must be a nonnegative integer")
