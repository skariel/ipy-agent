#!/usr/bin/env python3
"""Export a read-only journal snapshot to inert, private file:// HTML."""
from __future__ import annotations

import argparse
from html import escape
import json
import os
from pathlib import Path
import sys

# Source-tree convenience, matching replay_trace.py; no runtime or provider starts.
try:
    from py_agent.evaluation import journal_events
except ModuleNotFoundError as exc:
    if exc.name != "py_agent":
        raise
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from py_agent.evaluation import journal_events


HEADER = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="no-referrer">
<title>py session journal</title>
<style>
body { max-width: 100ch; margin: 2rem auto; padding: 0 1rem; font-family: system-ui, sans-serif; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; padding: 1rem; background: #f2f2f2; color: #111; }
details { margin: .7rem 0; border: 1px solid #aaa; padding: .7rem; }
summary { cursor: pointer; font-family: monospace; overflow-wrap: anywhere; }
.warning { font-weight: bold; }
</style></head><body><h1>py session journal</h1>
<p class="warning">Private export: this file may contain secrets, prompts, source code and program output. Do not publish it.</p>
<p>Read-only SQLite snapshot, not a live view. Expand an event to see its complete stored record.
Generation-request events also contain the exact messages sent for that request: system prompt, frozen memory, retained conversation and runtime observations.
Use your browser's Find to locate <code>generation_request</code>.
A later request may have a different context after older conversation is evicted. Export again to a new filename to see later events.</p>
"""


def html_text(value) -> str:
    # JSON can contain escaped lone surrogates. Keep those visible rather than
    # failing UTF-8 output; the complete JSON record retains them losslessly.
    text = str(value).encode("utf-8", "backslashreplace").decode("utf-8")
    return escape(text, quote=True)


def render_event(stream, event: dict, number: int) -> None:
    label = f"{event.get('seq', number)} | {event.get('id', '?')} | {event.get('kind', '?')}"
    stream.write(f'<details id="event-{number}"><summary>{html_text(label)}</summary>\n')
    content = event.get("content")
    if event.get("kind") == "generation_request" and isinstance(content, dict):
        messages = content.get("messages")
        if isinstance(messages, list):
            stream.write('<details><summary>Exact model context: messages</summary>\n')
            for index, message in enumerate(messages, 1):
                if isinstance(message, dict):
                    role = message.get("role", "?")
                    text = message.get("content")
                else:
                    role, text = "?", message
                if not isinstance(text, str):
                    text = json.dumps(text, ensure_ascii=True, allow_nan=False, indent=2)
                stream.write(f'<h3>Message {index}: {html_text(role)}</h3><pre>{html_text(text)}</pre>\n')
            stream.write('</details>\n')
    # No field selection or truncation: original provider body, outputs, metadata
    # and all request-message fields remain available, even for unknown events.
    payload = json.dumps(event, ensure_ascii=True, allow_nan=False, indent=2)
    stream.write(f'<details><summary>Complete stored event (JSON)</summary><pre>{html_text(payload)}</pre></details>\n</details>\n')


def export_session(journal: Path, output: Path | None = None) -> tuple[Path, int]:
    journal = Path(journal).expanduser().resolve(strict=True)
    output = Path(output).expanduser().absolute() if output is not None else journal.parent / "session.html"
    # Anchor creation and cleanup to the same directory. O_EXCL refuses both
    # existing files and symlinks, including dangling symlinks. Never overwrite.
    parent_fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    created = None
    fd = None
    try:
        fd = os.open(output.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=parent_fd)
        created = os.fstat(fd)
        # Keep the descriptor pinned through failure cleanup to prevent inode
        # reuse if another process unlinks the newly created destination.
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n", closefd=False) as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(HEADER)
            stream.write(f'<p>Journal: <code>{html_text(journal)}</code></p>\n')
            count = 0
            events = journal_events(journal)
            try:
                for count, event in enumerate(events, 1):
                    render_event(stream, event, count)
            finally:
                events.close()
            stream.write(f'<p>End of snapshot: {count} events.</p></body></html>\n')
        return output, count
    except BaseException:
        if created is not None:
            try:
                current = os.stat(output.name, dir_fd=parent_fd, follow_symlinks=False)
                # Do not delete a replacement file if the destination changed.
                if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                    os.unlink(output.name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Export full session events and model contexts to private, read-only-view HTML. No code execution or network calls.")
    parser.add_argument("journal", type=Path, help="Explicit journal.sqlite path")
    parser.add_argument("output", nargs="?", type=Path, help="New HTML file (default: session.html beside the journal); must not exist")
    args = parser.parse_args(argv)
    print("WARNING: the HTML export may contain secrets; keep it private. It is a snapshot, not a live view.", file=sys.stderr)
    try:
        output, _ = export_session(args.journal, args.output)
        print(output.as_uri())
        return 0
    except Exception as exc:
        # Paths/errors are untrusted terminal text, too.
        print("export_session: " + json.dumps(str(exc), ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
