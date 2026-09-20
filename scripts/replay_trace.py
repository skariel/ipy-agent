#!/usr/bin/env python3
"""Offline accounting: python scripts/replay_trace.py JOURNAL [JOURNAL ...]."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# Source-tree convenience; the installed package works without this fallback.
try:
    from py_agent.evaluation import account_trace, journal_events
except ModuleNotFoundError as exc:
    if exc.name != "py_agent":
        raise
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from py_agent.evaluation import account_trace, journal_events


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only deterministic journal accounting; no model calls or code execution.")
    parser.add_argument("journals", nargs="*", type=Path, help="SQLite journals, each reported independently")
    parser.add_argument("--journal", action="append", default=[], type=Path, help="Additional journal; repeatable")
    args = parser.parse_args(argv)
    paths = args.journals + args.journal
    if not paths:
        parser.error("supply a journal")
    try:
        result = {"journals": [{"path": str(path), "accounting": account_trace(journal_events(path))} for path in paths]}
        # ASCII JSON escapes control sequences, bidi controls, and terminal escape
        # characters even in hostile metadata/path labels. No transcript text.
        print(json.dumps(result, ensure_ascii=True, allow_nan=False, sort_keys=True, indent=2))
        return 0
    except Exception as exc:
        # Diagnostics stay separate from machine-readable stdout; no raw escapes.
        print("replay_trace: " + json.dumps(str(exc), ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
