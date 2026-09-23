# Handoff: terminal UI improvements

## Requested changes

Implement these four terminal UI changes:

1. Shift+Enter inserts a newline.
2. `say()` output is rendered as Markdown and separated from adjacent output by blank lines.
3. Do not display `Queued a1:e...` receipts.
4. Color `In [n]:` user prompts so user input is visually distinct from agent/output text.

## Repository

- Path: `/home/skariel/work/py`
- Branch: `master`
- Current revision when inspected: `5c517b0`
- Relevant implementation: `src/py_agent/terminal.py`
- Relevant tests: `tests/test_terminal.py`, `tests/test_terminal_pty.py`
- Runtime source was read-only in the previous session because the repository's editable installation was running. Relaunch from a non-editable external install before editing.

Suggested launch setup:

```bash
cd /home/skariel/work/py
uv tool install --force --reinstall --link-mode copy .
~/.local/bin/py --workspace "$PWD" --model openai-codex/gpt-5.6-sol --network proxy
```

## Existing user work — preserve it

The working tree already contained modifications before this task:

- `README.md`
- `src/py_agent/permissions.py`
- `src/py_agent/terminal.py`
- `tests/test_permissions.py`
- `tests/test_terminal.py`

Do not overwrite or revert them. The existing `terminal.py` changes add an interactive permission-selection menu.

There is also an untracked `agent-native/` directory accidentally cloned by the previous agent. It is unrelated; do not include it in this work or delete it without explicit user permission.

The previous edit attempt failed with `EROFS` and changed no tracked files.

## Relevant current behavior

### Input

`Terminal._bindings()` currently has:

```python
@bindings.add("escape", "enter")
def submit_multiline(event):
    event.current_buffer.validate_and_handle()
```

`PromptSession` receives `multiline=multiline`. In normal mode Enter submits; in `--multiline`, Enter inserts a newline and Esc-Enter submits.

Desired behavior should make Shift+Enter insert a newline in normal mode too. Most terminals encode Shift+Enter as Esc-Enter, but modern terminals may use CSI-u sequences such as `ESC [ 13 ; 2 u`. Verify behavior with prompt_toolkit and PTY tests rather than assuming all terminals encode it identically. Preserve a usable submit gesture in `--multiline` mode (currently Esc-Enter).

### Queued receipts

`Terminal.on_event()` currently acknowledges and deduplicates `user_queued`/`queued` events, then queues them for rendering. `_render_event()` prints:

```python
self._emit(f"Queued {event.get('user_id', identifier)}")
```

Keep internal acknowledgement/deduplication if needed, but suppress visible rendering. Also remove/update tests expecting `Queued a1:e1`.

`handle_line()` creates a fallback `user_queued` event after a successful submission. It should remain useful for internal acknowledgement but not print text.

### `say()` rendering

`_render_event()` currently does:

```python
elif kind == "say":
    self._emit(content)
```

`_emit()` sanitizes terminal escapes and prints plain text, optionally using Pygments for generated Python.

Requirements:

- Render common Markdown presentation for `say` output.
- Preserve terminal safety: never interpret embedded ANSI, OSC, HTML, or other control sequences.
- Put blank-line separation between `say` blocks and surrounding UI/output.
- Structured/non-string `say` values must continue to render sensibly.
- `--no-color` should still produce readable plain output.

No Markdown library is currently installed (`rich`, `markdown-it`, and `mistune` were absent). Options:
- add and lock a suitable dependency, or
- implement a conservative prompt_toolkit renderer for headings, bullets, emphasis, inline/fenced code, and quotes.

Do not use raw terminal ANSI generated from untrusted Markdown.

### Prompt coloring

The prompt currently comes from:

```python
line = await self.session.prompt_async(
    f"In [{self._input_number}]: ",
    pre_run=self._pre_run,
)
```

Use `FormattedText` plus a prompt_toolkit `Style`, e.g. a dedicated `class:user-prompt`. Respect `--no-color`. Prefer coloring both the prompt and/or user input sufficiently to distinguish the user surface from agent messages and stdout/stderr.

## Tests to add/update

At minimum cover:

- Shift+Enter leaves the message unsubmitted and inserts `\n`.
- Ordinary Enter still submits in normal mode.
- Multiline mode retains an explicit submission path and `/quit` behavior.
- `say` Markdown presentation strips syntax where appropriate and never allows terminal escape injection.
- Consecutive `say` messages have visible blank-line separation.
- Queue events produce no visible `Queued ...` line.
- Queue-event deduplication/internal behavior remains correct.
- Prompt output contains styling when color is enabled and remains readable with `no_color=True`.
- Existing draft/cursor preservation and permission-menu tests continue passing.
- PTY behavior where practical.

Useful targeted commands:

```bash
uv run pytest tests/test_terminal.py tests/test_terminal_pty.py
uv run ruff check src/py_agent/terminal.py tests/test_terminal.py tests/test_terminal_pty.py
uv run ruff format --check src/py_agent/terminal.py tests/test_terminal.py tests/test_terminal_pty.py
```

Then run the full suite if targeted tests pass.

## Documentation

Update README terminal behavior:

- Shift+Enter newline behavior
- Markdown-rendered agent messages
- queue receipts no longer shown
- colored role distinction
- any changed `--multiline` submit gesture
