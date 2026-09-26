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
Recognized transient provider failures (rate limits, transport/timeouts, and
server 5xx errors) receive up to two retries before any Python cell executes;
a lost response can still mean both model attempts were billed. Authentication,
request, format, and unknown local errors are not retried.

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
`/plugins`, and `/history` (when a journal is selected). `say(text, final=False)` publishes progress; a successful cell
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
persistent `outputs[index]` string; inspect it with a smaller slice, e.g.
`@print(outputs[1][:4000])`. Stream order is stdout then stderr when both exist;
extra text beyond the storage cap is discarded. Other model-facing execution
observations exceeding 8,000 characters receive a short omission notice.
Execution feedback is a separate observation record internally;
providers that support only system/user/assistant roles receive its plain text
as a user-role message, without a repeated label or internal IDs. Silent cells
add no synthetic completion message. The system prompt instructs the model to
treat execution output as untrusted data. Once 20
small execution results accumulate, the oldest 10 are stored as persistent
`outputs[index]` strings and replaced in model context with short references
that report the original character count;
the latest 10 stay intact. References to already-spooled large outputs are never
archived a second time. If the selected executor cannot confirm storage, the
original results remain in context. This does not change user requests, executed
source, or the optional journal. User
messages are not clipped. Model responses over 8,000 characters are
rejected without execution and the model is asked for a smaller cell. Generated
cells cannot publish more than 8,000 characters of `say()` content; direct user
cells are not subject to that `say()` cap. These limits
do not restrict side effects from successfully dispatched code. Execution has
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
