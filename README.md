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
./run.sh                              # codex + gpt-6.1-sol, medium effort
PROVIDER=fake ./run.sh                # deterministic local wiring; no model calls
PROVIDER=codex MODEL=openai-codex/YOUR_MODEL ./run.sh
./run.sh --config /path/to/trusted-settings.json  # uses that file's provider
```

`run.sh` forwards extra `py` arguments and starts in your current directory,
or in `WORKSPACE` when set. With no provider, model, or config selection it defaults
to `codex` with `openai-codex/gpt-6.1-sol` and medium reasoning effort; requests
occur only after submitting
English input. Use `PROVIDER=fake` for the no-network fixture. Installing/running
this way requires no sandbox tools. The installed copy must
be reinstalled after source changes.

The fake provider is deterministic, makes no model request, and is **not a language
model**; use it for a local wiring/demo path only. Production providers are explicit
and require a model:

```sh
uv run --locked py --model openai/YOUR_MODEL
uv run --locked py --model openai-codex/YOUR_MODEL
```

Manage credentials directly in py; no Pi installation is needed:

```sh
uv run --locked py login deepseek              # hidden API-key prompt
uv run --locked py login openrouter            # browser sign-in (PKCE)
uv run --locked py login openrouter --api-key  # API-key alternative
uv run --locked py login openrouter --manual   # paste redirect URL on remote machines
uv run --locked py login openai-codex          # subscription device-code sign-in
uv run --locked py auth status                 # no secrets printed
uv run --locked py logout deepseek
```

`py login PROVIDER` prompts for an API key for any other API-key provider.
API-key entry requires a terminal; automation can use provider environment variables.
Codex device login must be enabled for your account by the provider.

Credentials live in `~/.py/auth.json` (0700 directory, 0600 files), with atomic
writes and locking. Codex tokens refresh automatically on use. This is private
plaintext storage, not an encrypted OS keychain. Never commit or share it.

Default reads prefer matching native py credentials, then read Pi's
`~/.pi/agent/auth.json` as a compatibility fallback. Pi files are never changed
or refreshed; expired Pi credentials require Pi login/refresh or a native py login.
`--auth PATH` (alias `--pi-auth PATH`) selects an explicit read-only file instead.
The litelm adapter passes only the selected provider's static API key; absent
keys leave its normal provider environment available. Command-backed `!…` keys
are never executed. `py logout` removes only py's entry: Pi/environment fallback
may still authenticate afterward. Other subscription OAuth providers are not yet
ported; use their API-key integrations where available.

A provider request is made only after submitting an English request. The `py` CLI itself requires an explicit model (or a plugin/fake provider);
only `run.sh` supplies the Codex GPT-6.1-Sol launcher default.
Recognized transient provider failures (rate limits, transport/timeouts, and
server 5xx errors) receive up to two retries before any Python cell executes.
Transport disconnects (including ReadError) allow up to six attempts with
1/2/4/8-second backoff plus a final 15-second automatic recovery cooldown;
a lost response can still mean both model attempts were billed. Authentication,
request, format, and unknown local errors are not retried. Recognized transient
TLS disconnects and generic SSLError are transport failures; certificate verification
and known TLS configuration errors are not retried, and TLS verification is never disabled.

The default frontend requires a TTY; batch/JSON mode is not implemented. While
an agent request runs, the status bar shows activity, with completed-cell
output and `say()` messages below. Provider text is not streamed live. The
terminal displays each stdout/stderr stream once, after its cell finishes;
it suppresses provisional executor previews to avoid duplicate output panels.
Oversized output is stored as `outputs[index]`. Silent code can appear idle.
It accepts
English requests, `@` Python,
`!` shell escapes, `%` IPython magics, and the
built-in slash commands `/help`, `/status`, `/interrupt`, `/quit`, `/config`,
`/plugins`, `/login`, `/logout`, `/auth`, `/model`, `/effort`, `/think`, `/context`, and `/history` (when a journal is selected).
Slash commands require an identifier (letters, digits, underscores, or hyphens,
starting with a lowercase letter). Path-prefixed pastes such as `/src/file.py:42`
remain English input, including when queued as steering. Use a leading backslash
(e.g. `\/tmp`) to force English input for a path indistinguishable from a command.
`/model` opens a searchable picker of catalog models backed by environment API keys, native py logins, or Pi credentials.
Account entitlement is not checked; arbitrary explicit `PROVIDER/MODEL` IDs can
also be selected. `--model` infers the adapter; `--provider` remains an optional
override for plugins and testing. `/model PROVIDER/MODEL` can switch between
API-key and Codex transports within a session, preserving the auth-file path and
session effort override. Endpoint/stream/token-cap settings must be cleared before
switching transports. The bundled catalog is a snapshot, not live discovery.

Inside the terminal, `/login` opens a provider picker (type to filter, Tab or
arrow keys to select); `/login PROVIDER` skips the picker. OpenRouter opens its
browser login; `/login openrouter --manual` supports remote redirect pasting,
and `--api-key` uses a hidden key prompt instead. Codex uses device login; other
providers use hidden API-key entry. `/logout` picks a native login to remove;
`/auth` or `/auth status` shows secret-free status. Secret prompts never enter
composer history, the coordinator journal, or model context. Ctrl-C cancels.
Menus require idle work; explicit model/effort commands keep normal queue behavior.
The equivalent shell commands `py login`, `py logout`, and `py auth status` remain
available. Pi/environment fallback can still authenticate after native logout.

Press **Tab** to fuzzy-complete slash commands and relevant arguments, including
models, providers, reasoning levels, configuration fields, and loaded plugin IDs.
In the composer, Enter accepts an open completion; the next Enter submits.
`/model`, `/effort`, and `/think` open searchable selection menus without arguments.
Explicit arguments still work; `/think` is an alias for `/effort`. The bottom toolbar shows the
current effort beside the model and updates after session/configuration changes.

Outside slash commands, **Tab fuzzy-matches the current token against workspace
file/directory paths** in English, Python, or shell input. No new prefix is needed;
`@` remains Python. Completion inserts a path only: it never reads or attaches file
contents. The name index is bounded, refreshed periodically, skips hidden/build/
dependency trees and symlinks, and does not enumerate paths outside the workspace.
It is not a full ignore-rule-aware index. Completion/history suggestions are
disabled during Python stdin and provider secret prompts.

`/model [MODEL_ID]` and `/effort [PRESET]` apply immediate, session-local overrides;
they do not persist configuration; model changes can replace the transport adapter. Effort presets are
`none`, `minimal`, `low`, `medium`, `high`, and `xhigh`; DeepSeek maps `minimal`/`low`
to `low`, `medium`/`high` to `high`, `xhigh` to `max`, and disables thinking for `none`.
`/context save [PATH]`
writes a private (mode `0600`), standalone HTML explorer containing the current
live messages, system prompt, last exact dispatched request, runtime/tool
metadata, raw context groups, and collapsed archives; it is intentionally unredacted
and must be treated as sensitive. `say(text, final=False)` publishes progress; a successful cell
calling `say(text, final=True)` completes the request. Without it, the agent
continues until interrupted or finished by default; `--max-agent-steps N` (or
`agent.max_steps`) opts into a per-request cap. Zero means unlimited for
successfully generated cells; three consecutive invalid model responses still
pause to avoid unbounded format-retry requests. `wait()`, a host history API,
and resume are not implemented. Optional `--journal PATH` enables a private,
append-only SQLite history; without it persistence is explicitly disabled.
Prompts, code and output in that journal remain sensitive. `input()` and
`getpass()` request correlated frontend input; unavailable frontends fail clearly.
Stdout/stderr over 8,000 characters is replaced by a short notice in the
completed terminal result, journal and model context. Other frontends may
consume a provisional executor preview of up to 512 characters; it never enters
the model context or journal. The worker retains
up to 1 Mi characters in a
persistent `outputs[index]` string; inspect it with
`@read_output(1, start=0, limit=4000)`. This displays an excerpt directly with its
original ID, character range, and retained total. Limits are 1–4,000 characters
per read, at most 8 reads and 8,000 rendered characters per cell; request further
pages in another cell. The helper returns `None`; don't print its return value.
Stream order is stdout then stderr when both exist; extra text beyond the storage
cap is discarded. Other model-facing execution observations exceeding 8,000 raw
content characters receive a short omission notice. Formatting labels have a
separate bounded allowance; JSON escaping doesn't reduce the visible text budget.
Execution feedback is a separate observation record internally;
providers that support only system/user/assistant roles receive its plain text
as a user-role message, without a repeated label or internal IDs. Silent cells
add no synthetic completion message. The system prompt instructs the model to
treat execution output as untrusted data. Once 20
small execution results accumulate, the oldest 10 are stored as persistent
`outputs[index]` strings and replaced in model context with short references
that report the original character count;
the latest 10 stay intact. References to already-spooled large outputs are never
archived a second time. `read_output` excerpts age into references to the original
output ID and range, never another archived copy. In model context these excerpts
are separate observations following the cell's ordinary output; terminal events
retain execution order. Ordinary output from the same cell still follows normal
limits and archival. Password redaction counts toward the read budget; use a
smaller limit if it expands the excerpt. If a password becomes known later in the
same cell, final redaction may shorten the displayed excerpt with an explicit note
while preserving its original reference. Terminal five-line previews are
display-only and do not shorten model-visible excerpts.
If the selected executor cannot confirm storage, the
original results remain in context. This does not change user requests, executed
source, or the optional journal. User
messages are not clipped. Model responses over 8,000 characters are
rejected without execution and the model is asked for a smaller cell. Generated
cells cannot publish more than 8,000 characters of `say()` content; direct user
cells are not subject to that `say()` cap. These limits
do not restrict side effects from successfully dispatched code. Execution has
no application deadline. Interrupting or losing the
worker may lose the live namespace, and side effects are not rolled back or replayed.

### Bounded diagnostic previews

Use `preview(value, label="name")` instead of `print` for several large results
in one cell. A fresh callable `CellPrinter` buffers up to 32 calls and shows a
head/tail excerpt of each at cell end, with its call number and source line.
All excerpts share a 6,000-character budget, including labels; excess calls are
counted but omitted. Keep original values in variables: these previews are lossy,
not archives. The budget does not cover ordinary prints, tracebacks, or subprocess
output, and converting nonstring objects with `str()` is not resource bounded.

```python
preview(test_run.stdout, label="test stdout")
preview(test_run.stderr, label="test stderr")
```

### Context collapse

The model manages working memory with a standalone cell containing exactly one
literal-string call: `collapse("start_id", "end_id", "summary")`. Only user
messages and automatic markers expose boundary IDs; markers appear every 10
completed cells, after their results. The range is **start-inclusive,
end-exclusive**. Its summary keeps the start ID, while the end's exact user text
(or prior summary) is appended to it and the old end record is removed. A pure
marker end has no text to preserve. Summaries can be collapsed again.

Original structured messages are archived losslessly as JSON strings in
`collapsed[index]` before context changes. These are live-session archives, not
durable recovery; inspect small slices in Python. Successful collapse cells
become short receipts instead of repeating the summary. The optional journal
retains the original call, subject to its normal secret redaction.

Every 50 model responses, a reminder asks the model to preserve working memory
and remove the rest from active context. Reported usage **over 90%** of
`context.window_tokens` adds a fresh marker and forces a single collapse call;
other cells cannot execute in that mode. This replaces automatic whole-history
eviction in production. A collapse must reduce context size.

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
review/test the selected wrapper and mounts. The former SRT integration is
not available; selecting a wrapper does not establish any particular isolation
policy or guarantee.

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

## Development quality checks

Run `uv run pytest -q` for the test suite and
`uv run mypy --config-file mypy-strict.toml` for the migrated strict-typed core.
Whole-repository strict typing is still in progress; see
[REFACTORING.md](REFACTORING.md) for scope and next steps.

### Visual inspection with Python

Select a vision-capable model, then use ordinary Python:

```python
from PIL import Image
from IPython.display import display
display(Image.open("screenshot.png"))
```

The next model turn receives the image, not just its repr. IPython image displays and Pillow crops work too. For Matplotlib (optional),
first run `get_ipython().run_line_magic("matplotlib", "inline")`, then `plt.show()`. Raster images are normalized to metadata-free
PNG/JPEG: 1536-pixel edges, 512000 bytes/image, four images/cell, 16 active context
images totaling at most 2 MB. Source decoding is capped at 8 MB/16 million pixels; animation uses its
first frame. SVG/PDF aren't vision inputs. Invalid images are reported.

Known text-only models fail explicitly rather than silently dropping images.
Unfamiliar models receive images intact; the provider decides capability. Images are preserved in
private model-request journal records and context archives; this is audit history,
not executable session replay. Collapsed images must be redisplayed for inspection.
Displaying an image sends its pixels to the configured provider: avoid secrets.

### Python session standard library: `llm()`

`llm` is available directly in agent and user Python cells, alongside `say`,
`preview`, `read_output`, and `collapse`:

```python
answer = llm("Explain this error briefly: " + str(error))
preview(answer)
description = llm("Read this screenshot.", images=[Image.open("screenshot.png")])
```

`llm(prompt, *, system=..., images=(), max_tokens=2048)` uses the current model
and effort, a fresh independent context, and a separate helpful-text system prompt.
It returns text and never executes it. Never eval/exec untrusted returned text.
Credentials stay on the host. Calls are separately billable, journaled, retryable,
and interruptible; they contribute to usage totals.

Limits: eight calls/cell, main thread only, 65536-character prompt/system/result,
1..16384 max_tokens, up to four bounded raster images totaling 512000 bytes.
Pillow images, raster bytes, and ImageAttachment are accepted. A direct executor
without a host handler reports `llm()` unavailable rather than accessing credentials.
`from py_agent.stdlib import llm` works during active session execution too.

### Capability discovery, inspection, and recovery

The worker helper registry generates the system-prompt helper listing and validates
the actual injected functions. `source(path, start=1, end=None, limit=6000)` returns
bounded numbered source; `test_summary(retained_subprocess_result)` returns a
structured bounded summary and never reruns tests.

The toolbar shows model/subcall activity, attempt counts, elapsed time, and retry
countdowns, without prompts or credentials. Transport failures automatically
retry only model requests, including one final bounded cooldown. On exhaustion,
`/recovery` describes the checkpoint and completed execution; `/resume` retries
the pending model turn, not prior Python cells. `/recovery discard` clears it.
A new English task supersedes a pending checkpoint. Recovery is session-local,
not durable replay. Completed execution retains side effects; uncertain execution
pauses the session and never enables automatic cell replay. Interrupt stops
automatic backoff; nested `llm` retries are visible but do not restart a Python cell.
