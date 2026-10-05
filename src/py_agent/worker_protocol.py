"""Shared wire limits for the local executor and worker.

Keep transport validation on both sides of the process boundary. Capture and
presentation budgets are separate policies and are deliberately not defined here.
"""
from __future__ import annotations

PROTOCOL_VERSION = 1
MAX_COMPLETION_MATCHES = 512
MAX_COMPLETION_MATCH_CHARS = 2_048
MAX_ERROR_CHARS = 8_192
MAX_FRAME = 1_048_576
MAX_INSPECTION_CHARS = 16_384
MAX_OUTPUT_FRAME_CHARS = 8_192
MAX_PASSWORD_SECRETS = 128
MAX_PASSWORD_SECRET_CHARS = 1_048_576
MAX_QUERY_CHARS = 65_536
MAX_RICH_OUTPUT_BYTES = 2_097_152
MAX_RICH_OUTPUT_FRAMES = 256
MAX_SAY_MESSAGES = 1_024
