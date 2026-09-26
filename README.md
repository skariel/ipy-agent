# py

`py` is a local coding-agent prototype with a small coordinator and a persistent
IPython worker. The default CLI routes English requests to one explicitly selected
provider, interprets responses as Python, executes bounded agent cells,
and displays the result. User-authored direct cells and generated cells share the
same live namespace.

## Current default CLI

The default executor is **unrestricted local execution**. Python, IPython shell
escapes, subprocesses, and agent-generated code run with the current user's normal
filesystem, process, network, and credential access. The worker is a separate
process for transport and lifecycle management only; it is not a sandbox or a
security boundary. The default has no permission broker, filesystem confinement,
network proxy, or credential isolation. Starting in a project directory does not
restrict access to other readable/writable paths. If isolation is required, launch
`py` inside an isolation environment that you provide and trust.

Install with Python 3.12–3.14 and `uv`:

```sh
uv sync --locked
uv run --locked py --provider fake
```

For an installed, non-editable tool copy, use the repository scripts:

```sh
./install.sh
./run.sh                              # fake provider; no model calls
PROVIDER=codex MODEL=openai-codex/YOUR_MODEL ./run.sh
./run.sh --config /path/to/trusted-settings.json  # uses that file's provider
```

`run.sh` forwards extra `py` arguments and starts in your current directory,
or in `WORKSPACE` when set. It does not choose a paid provider by default.
Installing/running this way requires no sandbox tools. The installed copy must
be reinstalled after source changes.

The fake provider is deterministic, makes no model request, and is **not a language
model**; use it for a local wiring/demo path only. Production providers are explicit
and require a model:

```sh
uv run --locked py --provider litelm --model openai/YOUR_MODEL
uv run --locked py --provider codex --model openai-codex/YOUR_MODEL
```

Configure litelm credentials through its normal provider environment. Codex uses
pi subscription credentials (login/refresh remain pi's responsibility); `--pi-auth`
can select the auth file. A provider request is made only after submitting an
English request. There is no implicit provider or production model selection.

The default frontend requires a TTY; batch/JSON mode is not implemented. While
an agent request runs, the terminal now shows generation/execution status and
completed-cell output and `say()` messages as they arrive. Provider text and
within-cell output are not streamed live. It accepts English requests, `@` Python,
`!` shell escapes, `%` IPython magics, and the
built-in slash commands `/help`, `/status`, `/interrupt`, `/quit`, `/config`,
`/plugins`, and `/history` (when a journal is selected). `say(text, final=False)` publishes progress; a successful cell
calling `say(text, final=True)` completes the request. Without it, the agent
iterates up to `agent.max_steps` (default 16). `wait()`, a host history API,
and resume are not implemented. Optional `--journal PATH` enables a private,
append-only SQLite history; without it persistence is explicitly disabled.
Prompts, code and output in that journal remain sensitive. `input()` and
`getpass()` request correlated frontend input; unavailable frontends fail clearly.
Execution output is retained up to the configured character limit; execution has
no application deadline. Interrupting or losing the
worker may lose the live namespace, and side effects are not rolled back or replayed.

### Configuration and provenance

Pass `--config PATH` to select an optional flat JSON file of typed scalar settings;
there is no implicit project config/profile. Resolution is built-in defaults, then
the selected file (`user` provenance), then explicit CLI flags and `/config set`
(session overrides). `/config get` and `/config describe` show effective values,
source, masked lower-priority sources, and application timing. Supported settings
are schema-validated and changes are revisioned; restart-only settings do not
hot-swap an already-created provider or executor. `/config save` writes only
explicitly named session overrides to the selected config target (use
`--file PATH` if no `--config` was supplied), and `/config reload` validates the
selected file before replacing its file layer. Credentials are not a config layer.

### Plugins and optional Jupyter adapter

The public plugin API is a v1 draft. Entry-point discovery is metadata-only and
plugin code is trusted executable Python. Installing a plugin never loads it.
Activate external entry-point IDs explicitly with repeatable `--plugin ID`, or
put a comma-separated list in `plugins.enabled` in a config file that you
explicitly select with `--config PATH`:

```sh
uv run --locked py --provider fake --plugin example-command-context \
  --context-transform example-command-context:prefix \
  --model-transform example-command-context:tag
uv run --locked py --provider fake --plugin example-executor-wrapper \
  --executor local --executor-wrapper example-executor-wrapper:audit
```

The CLI does not search the project for configuration or plugins. A selected
config file is required to pass the CLI's owned, regular-file and permission
checks; its plugin IDs are read before loading the matching entry points, then
its settings are validated against the combined core and enabled-plugin schema.
Plugin settings use their declared namespaced keys, for example
`example-command-context.greeting`. Settings belonging to a plugin that is not
enabled are rejected. `--plugin` adds IDs to those explicitly listed in the
selected file.

`--provider ID` selects an enabled provider service (the built-in `fake`,
`litelm`, and `codex` providers remain available); `--executor ID` selects an
enabled executor, with `local` as the default. `--router ID` and
`--interpreter ID` select their respective services, and repeatable
`--executor-wrapper ID` options apply qualified wrappers in the supplied order.
All selections fail clearly when the service is unknown, disabled, or ambiguous;
there is no local-executor or provider fallback. A plugin's config schema is
merged before config validation, and command/selected-transform/selected-observer
factories receive the plugin's immutable configuration namespace. A service factory
may opt in with a named `config` parameter, receiving immutable namespaces keyed by
plugin ID; wrapper factories may similarly accept `config` after their delegate.
External slash commands cannot shadow `/config`, `/plugins`, or the terminal's
built-in commands.
`/plugins` continues to report discovered metadata separately from loaded
manifests and never activates code during inspection.

Plugin commands and wrappers are available after activation. Context transforms,
model-request transforms, and output observers are not selected implicitly; opt
in with repeatable `--context-transform PLUGIN:STAGE`,
`--model-transform PLUGIN:STAGE`, and `--observer PLUGIN:NAME` flags, or the
corresponding `plugins.context_transforms`, `plugins.model_transforms`, and
`plugins.observers` comma-separated config settings. Plugin activation and
service/wrapper/stage selection are not security boundaries: enabled code runs with
the host process's privileges. See [PLUGIN_API.md](PLUGIN_API.md) and
[ARCHITECTURE_STATUS.md](ARCHITECTURE_STATUS.md) for the draft API and remaining
integration gaps.

### Optional wrapper-backed isolated executor

`py_agent.isolated_executor` supplies an opt-in `isolated` executor service and
`create_isolated_executor()` factory. It never changes the default `local`
executor or adds a sandbox dependency. To opt in from the CLI, select a trusted
JSON config file (never put credentials in it) containing, for example,
`"isolated-executor.wrapper_command": "[\"/absolute/path/to/wrapper\", \"--\"]"`,
then run `py --config PATH --plugin isolated-executor --executor isolated --provider fake`.
The wrapper config is executable policy: do not put credentials in argv or untrusted
project files. An embedding may instead register and select it explicitly:

```python
import json
from py_agent.builtin_services import BuiltinPlugin
from py_agent.coordinator import Coordinator
from py_agent.isolated_executor import IsolatedExecutorPlugin
from py_agent.plugins import PluginRuntime

wrapper_argv = ["/absolute/path/to/wrapper", "--wrapper-option", "--"]
plugin = IsolatedExecutorPlugin(config={
    "wrapper_command": json.dumps(wrapper_argv),
})
runtime = PluginRuntime.load(builtins={
    "builtin": BuiltinPlugin(),
    "isolated-executor": plugin,
})
runtime.config.validate({
    "isolated-executor.wrapper_command": json.dumps(wrapper_argv),
})
coordinator = Coordinator(
    runtime,
    router="default", provider="fake", interpreter="basic", executor="isolated",
)
```

The plugin's `isolated-executor` config namespace includes `wrapper_command`,
`worker_executable`, `runtime_parent`, startup/interrupt/input timeouts, and
`max_output_chars`. The
wrapper command is a JSON-encoded argv **prefix**, not shell text; the pinned
Python worker command is appended as individual arguments using
`create_subprocess_exec` (no shell). At startup, the configured wrapper executable
must resolve to an executable file, and the wrapped worker must return the
expected protocol-ready frame. Missing wrappers, launch failures, or handshake
failures are errors; there is no fallback to `local`. For an embedding that
already resolves plugin config, `isolated_executor_service_factory(config=...)`
consumes the immutable mapping keyed by plugin ID. The lower-level
`create_isolated_executor(wrapper_argv, ...)` factory is also available directly.

This executor starts the wrapper **once for the lifetime of one persistent
worker**. It therefore provides whole-worker wrapping, not a fresh isolation
environment for each operation. Per-operation isolation requires a different
executor design and does not preserve this worker's shared namespace. The wrapper
must preserve the worker's stdin/stdout protocol, keep diagnostics off stdout,
make the configured interpreter and installed `py_agent` runtime available
(read-only where appropriate), and correctly handle signals and child cleanup.
For a container with different internal paths, set `worker_executable` and
`runtime_parent` to the corresponding paths inside it. These conditions and the
wrapper's actual policy are not probed or guaranteed by this plugin.

A separate process is not confidentiality isolation. This plugin does not verify
filesystem, network, credential, namespace, or resource policies; it does not
scrub the environment inherited by the external wrapper; and readable host files
or secrets may remain reachable or be exposed through outputs. The wrapper itself
runs with the launching user's authority, and wrapper-specific process cleanup
can vary. Treat the wrapper command and its configuration as trusted executable
policy, avoid putting secrets in command arguments/config, and independently
review/test the selected wrapper and mounts. The legacy `sandbox.py` SRT path is
not adapted here: it has a separate launcher, worker protocol, and confinement
preflight, so reusing it as a generic wrapper would not safely establish this
executor's protocol or policy.

Jupyter support is an **optional experimental adapter**. The CLI and session
helpers include `py kernel install/start/stop`, `py kernels`, and `py attach`, with
private connection/session records. Install the `jupyter` extra (for example,
`uv sync --locked --extra jupyter`) to use them; stock `jupyter-console` is an
additional dependency for attach. These commands and the kernel entrypoint are implemented but the managed-kernel
workflow is not yet verified end to end. Bounded rich displays are forwarded
after a submission completes; incremental streaming is unavailable. Correlated
stdin, bounded completion, and static inspection are implemented but not yet
integration-verified; full history and dynamic completion remain incomplete. See
[ARCHITECTURE_STATUS.md](ARCHITECTURE_STATUS.md) for details.

## Legacy sandboxed supervisor CLI (`--legacy`)

The remainder of this README documents the previous sandboxed supervisor CLI and
its features only. Invoke it explicitly with `--legacy` (for example,
`uv run --locked py --legacy ...`). None of its SRT/bubblewrap confinement,
permission prompts, journal/history, `wait()` semantics, memory/context UI, or
network-proxy behavior applies to the default coordinator CLI above.

### Install and run (legacy)

Linux with `uv`, `srt`, bubblewrap, socat and ripgrep installed:

```sh
uv sync --locked
uv run --locked py --legacy --model openai-codex/gpt-5.6-sol --network proxy
```

The same `./install.sh` installs the tool copy used by `./run.sh`. For the
legacy CLI, pass `--legacy` plus its model and sandbox options explicitly:

```sh
./run.sh --legacy --model openai-codex/gpt-5.6-sol --network proxy
```

When invoking `py` directly, select a model available to your account; the CLI
has no implicit model choice. Dependencies are locked; tested with Python 3.13.11
and IPython 9.10.0.
`uv` uses copy mode because hardlinked runtime files can bypass pathname-based
write protection. To repair an older repository-local environment, run
`uv sync --locked --reinstall --link-mode copy`.

#### Letting the agent edit this repository (legacy)

The sandbox recursively permits writes beneath `--workspace` (the launch directory
by default), but always makes its own runtime and import roots read-only. Consequently,
launching the repository's editable installation with `uv run py --legacy` deliberately makes
`src/` read-only: `src/py_agent` is part of the trusted runtime. A filesystem wildcard
cannot override that protection.

To let the agent modify the entire current repository, install a non-editable runtime
outside it. Explicitly reinstall every dependency in copy mode; `--force` alone can
leave hardlinked dependencies in the tool environment:

```sh
cd /path/to/py-agent
uv tool install --force --reinstall --link-mode copy .

~/.local/bin/py \
  --legacy \
  --workspace "$PWD" \
  --model openai-codex/gpt-5.6-sol \
  --network proxy
```

Here `--workspace "$PWD"` covers the directory recursively—no `*` is needed. The
external tool runtime remains protected while `src/`, `tests/`, and other repository
files are writable subject to their normal OS permissions. Re-run the installation
command after changing `py-agent` itself to refresh the installed runtime.
If startup reports `Trusted runtime has a hardlinked file`, the tool was not fully
reinstalled in copy mode; run the exact install command above again.

Codex defaults to **medium reasoning effort**; select another supported level with
`--effort none|minimal|low|medium|high|xhigh` or `effort` in private config. It reads
existing private `~/.pi/agent/auth.json` on every request (`--pi-auth PATH` overrides it). Login and
refresh remain pi's job; py never rewrites the shared credentials. Auth must be
outside both the writable workspace and `/tmp`. Codex uses a fixed endpoint,
not `--api-base`.
Credentials are not injected into worker environment or request journals, but
readable credential files are subject to the privacy warning below.

Provider prompt caching is automatic when supported. For Codex, py sends the
run's random opaque ID as `prompt_cache_key` and in the `session-id` and
`x-client-request-id` headers, matching pi's current provider behavior. The ID
remains stable across turns, steering restarts, context epochs, and `/reset`, and
a new py run gets a new ID;
it is not derived from prompts, paths, or credentials. No `conversation_id` or
cache-retention field is sent.

The toolbar reports `CH` as the session's weighted cumulative cache-hit rate
(`sum(cached_tokens) / sum(input_tokens)`). `r` and `w` are cumulative
provider-reported cache-read and cache-write token counts. Missing counters stay
unknown (`?`), and a reported write count of zero remains zero; py never infers
or invents cache writes. These counters describe provider accounting, not task
quality, price, or guaranteed cache performance.

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
`uv run --locked py --legacy --model openai/YOUR_MODEL --network proxy`.
No output-token cap is supplied by default; `--output-tokens` is an optional
explicit API-key-provider setting. Native providers/SDKs may have their own limits.

Optional private `~/.py/config.toml` can hold `model`, `api_base`, `stream`,
Codex `effort`, `context_window_tokens` and a
`[budgets]` table matching supported CLI budget flags. Run `py --help` for options.
Explicit configuration must be outside both the writable workspace and `/tmp`,
or under the protected host root. `--workspace` selects the project write root;
shared `/tmp` is also read/write. `--host-root` relocates host storage. Other subscription providers are not implemented.

### Terminal and Python API (legacy)

- `In [1]:` accepts natural-language requests, **not direct Python execution**.
  Input numbers advance on successful submissions and direct cells, not local slash
  commands or cancelled drafts. Enter submits; Shift-Enter inserts a newline;
  Ctrl-R searches history.
- Agent `say()` messages render common Markdown on pi's gray message background and
  are separated from surrounding terminal output by blank lines. User input has no
  background. Queue receipts are kept internal rather than shown.
- Prefix a cell with `@` to execute user-authored Python directly in the same live
  IPython process and namespace used by the agent: `@x = 3`, then `@print(x)`.
  Multiline input works with `--multiline`; start with `@` and put the cell below it.
  IPython `%`/`%%` magics and `!` escapes work inside a direct cell. A leading `@`
  is py's direct-cell marker, not an IPython magic (Python still uses `@` for
  decorators inside the cell). Direct cells are rejected while agent work is active.
- Generated Python is **hidden by default**; `/trace` shows it as `Python [n]:`
  along with audit events. Results remain visible as `Out[n]:` and stdout/stderr, limited to the first five
  lines per result stream. Result numbers identify worker cells, not user-request numbers. Small streams
  are buffered until a control boundary or cell completion. A spinner shows thinking.
- `30%/272k` uses reported input tokens for the current context divided by its
  **configured capacity**, not the byte estimate. `CH` is the weighted session
  cache-hit percentage. Unknown usage shows `?`; `/usage` provides detailed counters.
- `/history [ID [OFFSET]]`, `/usage`, `/help`, `/permissions`, `/interrupt`,
  `/reset`, `/quit`. Permission prompts are resolved with `/approve ID SCOPE` or
  `/deny ID`; saved grants can be removed with `/revoke ID`.
- `/reset` explicitly clears conversation at the next request without resetting
  Python or making extra model calls. New input steers the agent, never Python stdin.
- Empty idle Ctrl-C exits. During work, first Ctrl-C interrupts; a second within
  two seconds exits. Empty Ctrl-D or `/quit` exits and cancels active work.
- `--vi`, `--multiline` (Enter adds lines and Esc-Enter submits), `--no-color`
  and `--no-input-history` are available. Paste stays a draft; output preserves
  your draft and cursor. On legacy terminals that encode Shift-Enter as Esc-Enter,
  use ordinary Enter for newlines in `--multiline` mode.
  Non-TTY interactive launches are not supported.

These names are already available to the model:

```python
say("Working on it")  # user-facing progress
say("The answer", final=True)  # finish after this cell succeeds
wait()  # unwind this cell and await user input
history.recent(n=10)
history.search("query", kind=None, limit=20)
history.read("EVENT_OR_CELL_ID", offset=0, limit=8000)

# Request brokered writes outside the mounted workspace:
fs = ask_rw_approval(
    "/outside/project",
    recursive=True,
    operations=("create", "modify", "rename", "delete"),
    reason="Apply the requested migration",
)
fs.write_text("relative/file.txt", "new contents")
fs.mkdir("generated")
fs.rename("old.txt", "new.txt")
fs.remove("obsolete.txt")
```

#### Interactive permissions (legacy)

With `--network proxy`, a connection to a destination not already covered by
`--allow-domain` is paused at the host proxy and shown as a permission request.
Approve or deny it directly in the terminal:

```text
/approve perm-1 once
/approve perm-1 session
/approve perm-1 project
/approve perm-1 all
/deny perm-1
```

The menu defaults to **Deny**; use the arrow keys and Enter to choose, or press
Escape to deny. There is no approval timeout: the request waits until you decide,
interrupt, or close the session. `once` covers one connection or one successful
brokered filesystem mutation; `session` lasts until this `py` process exits.
`project` is saved for the exact
canonical `--workspace`; `all` saves that same exact resource grant for every
workspace using this host root. Interactive network grants are exact host/port
pairs and are never silently widened to wildcards.

Persistent grants live in protected host state, separately from model/budget
configuration:

```text
~/.py/permissions.json
```

A custom `--host-root` relocates this file. Use `/permissions` to list pending and
saved grants and `/revoke PERMISSION_ID` to remove a saved project/all-project
grant. The writable repository never stores trusted permission policy.

`ask_rw_approval()` returns a supervisor-brokered capability; it does **not**
remount the live sandbox. Its relative capability methods work after approval,
but ordinary `open()`, `pathlib`, shell commands, and subprocesses remain confined
to the original workspace and `/tmp`. Runtime, authentication, journal, launcher,
and host-control paths are permanent hard denials and cannot be approved.

Without `final=True` or `wait()`, the next model turn follows automatically.
`print()` and expression results go back as observations. A failed cell does not
publish its staged final answer; earlier side effects remain. Do not combine
`final=True` and `wait()`. There is no interactive stdin/debugger or supported
unmanaged background-thread workflow. No source is blindly replayed.

#### Large output (legacy)

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

### Memory, context and full session view (legacy)

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
uv run --locked py --legacy --model openai-codex/gpt-5.6-sol --network proxy --context-window-tokens 272000
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

### Safety and remaining limits (legacy)

- This is **write confinement, not confidentiality isolation**. Normally readable
  files remain readable; their contents can reach the model/provider through
  results, even with worker networking denied. Journals, composer history and
  exports can also contain secrets. Prefer a disposable workspace.
- Supported srt cannot provide the originally requested transparent open network.
  Default `--network open` therefore fails closed. Explicit `--network proxy`
  uses a host-mediated prompt for destinations not covered by `--allow-domain` or
  a saved grant. Denial, malformed approval IPC, or shutdown fails closed. Proxy
  mode is not transparent DNS/UDP/localhost networking. Provider calls happen in
  the supervisor, separately from worker network permissions.
- Writes are allowed in the workspace **and shared `/tmp`**, subject to normal OS
  permissions. Worker code can modify other writable temporary files there;
  `/tmp` is not session-private. Default `TMPDIR` remains in workspace scratch.
  Host storage, launcher sockets and trusted runtime/import roots remain protected,
  including when located under `/tmp`. Approved outside-workspace mutations are
  performed only through the bounded brokered capability API described above.
  No unsandboxed fallback exists.
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
uv run --locked py --legacy --check-sandbox --network proxy
PY_AGENT_SANDBOX_TESTS=1 uv run --locked pytest -q -rs tests/test_sandbox.py
```

Nested srt is blocked in the implementation sandbox. Real integration tests skip
when isolation is unavailable; **skips are not confinement evidence**. Runtime
unit tests use fixed trusted snippets, not an unsandboxed model-execution backend.
Launcher unit tests relocate host scratch into pytest's temporary directory.
The 80-host-thread stress test explicitly skips if inherited resource limits
prevent creating its threads; this is not evidence that the stress test passed.
