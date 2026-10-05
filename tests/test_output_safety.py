"""Shared output safety primitives."""
from __future__ import annotations

from py_agent.output_safety import redact_json, redact_text, safe_capture


def test_redaction_handles_nested_values_and_secret_markers():
    assert redact_json({"secret": ["secret", ("safe",)]}, ["secret"]) == {
        "[REDACTED]": ["[REDACTED]", ("safe",)]
    }
    assert redact_text("REDACTED", ["REDACTED"]) == ""


def test_truncated_capture_drops_partial_secret_before_archiving():
    secret = "private-password"
    assert safe_capture("safe private-pass", [secret], truncated=True) == "safe "
    assert safe_capture("safe private-pass", [secret], truncated=False) == "safe private-pass"
    assert safe_capture(secret * 2, [secret], truncated=False) == "[REDACTED]" * 2
