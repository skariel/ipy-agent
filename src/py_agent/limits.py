"""Provisional conservative supervisor limits, not model-window discovery."""
from dataclasses import dataclass, fields


class LimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    input_tokens: int = 24000
    output_tokens: int = 2048
    observation_chars: int = 8000
    max_requests: int = 100
    cell_seconds: float = 300
    max_output_bytes: int = 1048576
    max_user_bytes: int = 32768
    generation_retries: int = 2

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"Invalid limit: {field.name}")
            if field.name != "cell_seconds" and type(value) is not int:
                raise ValueError(f"{field.name} must be an integer")
            if value == 0 and field.name != "generation_retries":
                raise ValueError(f"{field.name} must be positive")
        if self.cell_seconds > 86400 or self.cell_seconds != self.cell_seconds:
            raise ValueError("cell_seconds must be finite and at most one day")
        if self.input_tokens <= self.output_tokens + 1024:
            raise ValueError("Input budget must leave room for a response and message overhead")
