# py

A simple agent loop backed by a persistent, sandboxed IPython session:

```text
user → model → Python cell → stdout / stderr / results / errors → model → …
```

The model speaks **Python only**, not provider tool calls. Variables survive
between cells. `say()` talks to you; `wait()` pauses for input. `memories` is an
ordinary in-process list for short-term notes. There is no memory file, variable
persistence, resume, forced saving turn or automatic injection of note contents.

This is an experimental implementation, not production-hardening or a guarantee
of model compliance. See [PLAN.md](PLAN.md) for current status and historical findings.

## Install and run

Linux with `uv`, `srt`, bubblewrap, socat and ripgrep installed:

```sh
uv sync --locked
uv run --locked py --model openai-codex/gpt-5.6-sol --network proxy
```

Select a model available to your account; there is no implicit model choice.
Dependencies are locked; tested with Python 3.13.11 and IPython 9.10.0.
`uv` uses copy mode because hardlinked runtime files can bypass pathname-based
write protection. To repair an older hardlinked install, run
`uv sync --locked --reinstall --link-mode copy`.

Codex uses **medium reasoning effort** and reads your existing private
`~/.pi/agent/auth.json` on every request (`--pi-auth PATH` overrides it). Login and
refresh remain pi's job; py never rewrites the shared credentials. Auth must be
outside both the writable workspace and `/tmp`. Codex uses a fixed endpoint,
not `--api-base`.
Credentials are not injected into worker environment or request journals, but
readable credential files are subject to the privacy warning below.

**Codex has no client-side output-token cap.** `--output-tokens` is not enforced
by this adapter; the backend does not accept that request parameter. Reported
usage is accounting, not a reason to reject completed code. Incomplete/failed
responses still never execute, and protocol validation remains. Complete
responses are validated before execution. For Codex, the first complete assistant message is the next
cell, regardless of its commentary/final-answer label; later messages generated
without execution results are discarded. Hidden reasoning and unfinished streams
never execute. The worker still requires raw code—prose is not turned into code.
Only a successful `say(..., final=True)` finishes the task; the provider's
`final_answer` label is not that signal.

API-key providers use **kennethwolters/litelm**, not LiteLLM. Set the provider's
key privately in your environment, then run
`uv run --locked py --model openai/YOUR_MODEL --network proxy`.
No output-token cap is supplied by default; `--output-tokens` is an optional
explicit API-key-provider setting. Native providers/SDKs may have their own limits.

Optional private `~/.py/config.toml` can hold `model`, `api_base`, `stream`,
`context_window_tokens` and a
`[budgets]` table matching supported CLI budget flags. Run `py --help` for options.
Explicit configuration must be outside both the writable workspace and `/tmp`,
or under the protected host root. `--workspace` selects the project write root;
shared `/tmp` is also read/write. `--host-root` relocates host storage. Other subscription providers are not implemented.

## Terminal and Python API

- `In [1]:` accepts natural-language requests, **not direct Python execution**.
  Input numbers advance on successful submissions, not commands or cancelled drafts.
  Enter submits; Ctrl-R searches history.
- Generated Python is **hidden by default**; `/trace` shows it as `Python [n]:`
  along with audit events. Results remain visible as `Out[n]:` and stdout/stderr.
  Result numbers identify worker cells, not user-request numbers. Small streams
  are buffered until a control boundary or cell completion. A spinner shows thinking.
- `ctx(last) 30%/272k` uses reported input tokens for the current context divided
  by its **configured capacity**, not the byte estimate. Unknown usage shows `?`;
  `out(last)` shows reported output tokens. `/usage` provides the detailed counters.
- `/history [ID [OFFSET]]`, `/usage`, `/help`, `/interrupt`, `/reset`, `/quit`.
- `/reset` explicitly clears conversation at the next request without resetting
  Python or making extra model calls. New input steers the agent, never Python stdin.
- Empty idle Ctrl-C exits. During work, first Ctrl-C interrupts; a second within
  two seconds exits. Empty Ctrl-D or `/quit` exits and cancels active work.
- `--vi`, `--multiline` (Esc-Enter submits), `--no-color` and `--no-input-history`
  are available. Paste stays a draft; output preserves your draft and cursor.
  Non-TTY interactive launches are not supported.

These names are already available to the model:

```python
say("Working on it")                  # user-facing progress
say("The answer", final=True)         # finish after this cell succeeds
wait()                                # unwind this cell and await user input
history.recent(n=10)
history.search("query", kind=None, limit=20)
history.read("EVENT_OR_CELL_ID", offset=0, limit=8000)
```

Without `final=True` or `wait()`, the next model turn follows automatically.
`print()` and expression results go back as observations. A failed cell does not
publish its staged final answer; earlier side effects remain. Do not combine
`final=True` and `wait()`. There is no interactive stdin/debugger or supported
unmanaged background-thread workflow. No source is blindly replayed.

### Large output

If a cell's combined stdout, stderr and displayed values exceed **8,000 characters**,
the worker saves the full decoded text to a private `/tmp/py-output-<sha256>.txt`
file and returns a notice with the character count, line count and path. Completed
identical output reuses the same file after verifying its contents; it does not
create another permanent copy. Unsafe or modified existing files are never
reused or overwritten; those cases retain a unique capture instead. The kernel
keeps running. Any small prefix already shown before a control boundary is included in
the file too. Read it with ordinary Python, for example:

```python
f = open("/tmp/py-output-THE_REPORTED_NAME.txt", encoding="utf-8")
print(f.read(4000))  # Repeat in later cells to read subsequent chunks.
# f.close() when finished.
```

Keep the handle open across cells to continue reading; a `with` block closes it.
Printing oversized chunks just saves another file. Lines are LF-delimited,
including an unterminated last line. Late subprocess output can extend a provisional
file; its notice identifies counts-so-far until the streams close. Provisional
filenames expire after publication: use the **last reported, final hash path**.
Files contain text
in capture order, not separate stdout/stderr labels.

The journal keeps the notice, **not the oversized text**. These are mutable worker
files, not immutable evidence. They persist in `/tmp` until removed and may contain
secrets. There is **no application file-size cap**; actual OS file limits and disk
failures explicitly report **incomplete** saved output while capture continues.
Large `say()` strings or structured replies also become complete file notices,
while keeping final-answer staging. There is no second byte-based output clipping:
chunks within the 8k-character threshold reach the next model turn intact.

## Memory, context and full session view

The kernel starts with `memories = []`. The model can inspect and edit it normally:

```python
memories.append("Useful finding")
print(memories)
```

The **system prompt stays fixed within each context**; conversation only appends.
At 95% of configured capacity, old dispatched conversation is cleared—not a
rolling tail. Only new user input awaiting dispatch carries over. The fresh prompt
says `This context started with X memories` and includes a bounded `%whos`-like
inventory of names/types. No note contents, values or object representations are
injected. The model can read `memories` or history when it needs earlier context.
The count and inventory remain frozen until the next reset. All variables and
functions stay alive; ending the kernel loses them. **There is no resume.**

Configure your model's supported capacity, for example:

```sh
uv run --locked py --model openai-codex/gpt-5.6-sol --network proxy --context-window-tokens 272000
```

This is configuration, not automatic model-window discovery. Without the flag or
`context_window_tokens` config entry, the `input_tokens` budget (default 272000)
is used. The default is **272,000 tokens**, with eviction at **258,400 reported
input tokens** (95%). No 24k cap remains. Only reported current-context input tokens
drive eviction. Before measurement, usage is unknown: the byte estimate is audit
information only and cannot reject or clear context. Large additions between
responses can still exceed a model's real window. `tail_groups` is removed.

Source and printed output still enter the audit journal, so explicitly written or
printed notes can appear there. This is evidence, not variable persistence or a
restorable execution environment. The private SQLite journal is stored at:

```text
~/.py/sessions/RUN/journal.sqlite
```

Export the **full session and exact per-request context** for a browser:

```sh
uv run --locked python scripts/export_session.py ~/.py/sessions/RUN/journal.sqlite
```

Open the printed `file://` URL; expand `generation_request` → **Exact model
context**. All events include complete stored JSON. This inert HTML is a private
snapshot, not a live view; it may contain secrets. Re-export to a new filename
(second argument); existing files are never overwritten. No code is executed.
Offline counters are available with `scripts/replay_trace.py JOURNAL`; they do
not infer task quality, actual cache behavior or prices.

## Safety and remaining limits

- This is **write confinement, not confidentiality isolation**. Normally readable
  files remain readable; their contents can reach the model/provider through
  results, even with worker networking denied. Journals, composer history and
  exports can also contain secrets. Prefer a disposable workspace.
- Supported srt cannot provide the originally requested transparent open network.
  Default `--network open` therefore fails closed. Explicit `--network proxy`
  with no domains denies worker destinations; add `--allow-domain DOMAIN` only
  as needed. Proxy mode is not transparent DNS/UDP/localhost networking. Provider
  calls happen in the supervisor, separately from worker network permissions.
- Writes are allowed in the workspace **and shared `/tmp`**, subject to normal OS
  permissions. Worker code can modify other writable temporary files there;
  `/tmp` is not session-private. Default `TMPDIR` remains in workspace scratch.
  Host storage, launcher sockets and trusted runtime/import roots remain protected,
  including when located under `/tmp`. No unsandboxed fallback exists.
- **No application work quotas:** no request/cell-count ceilings, execution
  deadlines, CPU/address-space/file/descriptor limits, user-message or logical
  frame-size limits, provider response/chunk quotas, journal quota, or history-call
  cutoff. Provider output limits are omitted unless explicitly configured. Large
  valid responses are accepted; malformed, failed or incomplete ones are not.
- **Resource use can grow without a preset ceiling.** RAM, disk, API usage and
  runtime are constrained by actual OS/provider limits, not application allowances.
  Use `/interrupt` or Ctrl-C to stop work. An actual disk failure still stops the
  session safely rather than pretending evidence was saved. Connection/startup/
  cleanup timeouts and validation of protected control files remain; these are
  not cell execution deadlines. Namespace listings are intentionally abbreviated.
- Observations are losslessly coalesced, not clipped a second time. The terminal
  no longer drops queued events or truncates trace text. Full audit records remain
  in the journal; oversized worker text is in the reported `/tmp` file.
- Descendant cleanup needs target-host verification. Execution interruption loses
  live Python state; earlier effects remain. No rollback, replay or aggregate quota.
- Each launch starts a fresh kernel. **Resume/restart reconstruction is absent**;
  saved files/history do not restore variables. Live multi-provider reliability
  and full sandbox/network/tree-cleanup validation remain incomplete.

```sh
uv run --locked pytest -q
uv run --locked py --check-sandbox --network proxy
PY_AGENT_SANDBOX_TESTS=1 uv run --locked pytest -q -rs tests/test_sandbox.py
```

Nested srt is blocked in the implementation sandbox. Real integration tests skip
when isolation is unavailable; **skips are not confinement evidence**. Runtime
unit tests use fixed trusted snippets, not an unsandboxed model-execution backend.
Launcher unit tests relocate host scratch into pytest's temporary directory.
The 80-host-thread stress test explicitly skips if inherited resource limits
prevent creating its threads; this is not evidence that the stress test passed.
