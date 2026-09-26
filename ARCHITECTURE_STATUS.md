# Architecture implementation status

This document records the implementation visible in the current CLI, coordinator,
plugin/configuration modules, optional Jupyter adapter, and `pyproject.toml`, in
contrast with the target in [ARCHITECTURE_PLAN.md](ARCHITECTURE_PLAN.md). It is a
code/documentation review, not a verification report: no tests or live provider
calls were run for this update.

## Current default architecture

The default `py` command is a coordinator-based vertical slice:

```text
PlainTerminal -> Coordinator -> selected router/provider/interpreter/executor
                                   |                         |
                             context + config          LocalExecutor
                                                         IPython worker
```

`pyproject.toml` installs the coordinator dependencies without SRT, bubblewrap,
socat, or other sandbox-runtime prerequisites. The normal CLI has no workspace
confinement or network-policy option. It explicitly selects a provider and starts
a persistent local IPython child process. Direct user cells and agent-generated
cells share that worker namespace. The process boundary supplies transport and
lifecycle separation only: code runs as the current user, with ordinary access to
available files, subprocesses, network, and credentials. There is no default
permission broker or credential isolation. To isolate execution, users must run
`py` inside isolation they provide, or explicitly select the optional wrapper-backed `isolated` executor with
`--plugin isolated-executor --executor isolated` and a trusted wrapper config.
`py` does not verify the wrapper's actual isolation policy.

The default TTY path handles one request at a time. An English request iterates
provider responses and generated cells until a successful `say(..., final=True)`
or interruption; a positive `agent.max_steps` opts into a step limit (default 0,
unlimited). Execution results become observations for later steps;
side-effecting cells are never automatically replayed.
Direct `@` Python, `!` shell escapes, and `%` IPython magics execute in the same
namespace. The default terminal also has built-in `/help`, `/status`, `/interrupt`,
`/quit`, `/config`, `/plugins`, and `/history` commands. Batch/JSON mode is not implemented.

`LocalExecutor` replaces stdout/stderr exceeding 8000 characters with a notice
and retains up to 1 Mi characters in the persistent `outputs[index]` namespace
for small follow-up slices. At 20 small execution results, older results are
stored in that namespace and replaced with short references in model context;
large-output references are exempt. Other model-facing observations and model
responses also have 8000-character bounds; user input is not clipped. The executor has
worker startup/interrupt timeouts, but no normal cell execution deadline. Correlated
`input()`/`getpass()` requests require a capable owning frontend. A worker failure or interrupt can discard in-memory Python state;
partial side effects are not rolled back or replayed. An explicit `--journal PATH`
selects an append-only SQLite journal and bounded `/history`; otherwise a
no-persistence implementation is selected. `/usage` and `/trace` are not implemented.

## Providers: deterministic fake versus production

Provider selection is explicit through `--provider` or `provider.id` in the
selected JSON config. There is no implicit production provider/model.

- **`fake`** is deterministic, requires no credentials, and makes no outbound
  model request. It is a fixture/demo provider, not an LLM; the built-in behavior
  returns a small `say(..., final=True)` cell containing the latest user request.
- **`litelm`** adapts the API-key provider path and requires an explicit model.
- **`codex`** adapts the Codex subscription path and requires an explicit
  `openai-codex/...` model; pi owns login/refresh and `--pi-auth` can select the
  auth file.

Production adapters preserve provider response status and reported usage.
The coordinator retries classified transient provider failures at most twice
without repeating Python execution; credentials, malformed responses and other
non-transient errors do not retry. A retry repeats the model request, however:
providers may bill both attempts if the original response was lost after processing.
The basic interpreter accepts only a
complete, non-rejected response, removes a
single enclosing Python Markdown fence, and compiles the resulting cell before
execution. Oversized responses and mixed/malformed Markdown are rejected
without execution and receive bounded model-only format corrections. Other
provider rejections stop the turn.
The CLI makes provider calls only after user submission.

The worker still has the current user's process privileges. Provider credentials
are not a worker sandbox boundary: agent code may inspect the environment and
other files it can read. The Codex credential file is not injected as a worker
capability, but ordinary same-user file access still applies.

## Implemented components and gaps

| Area | Implemented now | Not implemented or materially incomplete |
| --- | --- | --- |
| Coordinator | Serialized submissions; explicit router/provider/interpreter/executor selection; request/execution identities; cancellation invalidates generation results; executor lifecycle ownership. | The full planned event/dataflow architecture, session attachment lifetime, a fully general middleware pipeline and journal-backed session recovery. |
| Execution | Persistent local IPython worker, shared direct/agent namespace, framed transport, stdout/stderr capture, optional short frontend-only preview (default terminal suppresses it), bounded retained output, interrupt/cleanup. | Default isolation, execution deadlines/resource quotas, continuous stream/rich-event delivery, rollback/recovery, and automatic resumption. |
| Context/observations | Production context adapter bridges the existing context policy; observation adapter packs execution output; provider-reported usage is retained. | Rich output observations and host history APIs beyond bounded `/history`. `wait()` is absent. |
| Commands/frontend | Plain TTY frontend, built-in config/plugins and local help/status/interrupt/quit commands, and explicitly activated plugin command contributions with collision checks. | Batch/JSON frontend, terminal-independent output event subscription, rich terminal/notebook rendering, and general lifecycle hooks. |
| Configuration | Typed core plus enabled-plugin scalar schemas, defaults, provenance/revisions, validation, config commands, explicit plugin activation from `--plugin` or the selected trusted config file. | Full plan precedence with profiles/environment/project layers, credential backends, and automatic service reconstruction for restart-only changes. |
| Plugins | Metadata-only entry-point discovery; explicit CLI activation; selected provider/router/interpreter/executor services, ordered wrappers, and explicit context/model-transform/observer selection; plugin config namespaces reach command/transform/observer/wrapper and opted-in service factories. | Middleware, general lifecycle hooks, hot reload, and a frozen compatibility contract. |
| Jupyter | Optional `ipykernel` protocol adapter; CLI management commands and private session/process-record helpers for install/start/stop/list/attach are present. | A verified end-to-end managed launch path, verified stdin exchange, incremental rich output delivery, dynamic completion/full history, and a finished notebook experience. |

### Configuration precedence and source attribution

The current CLI supports a narrower precedence chain than the plan:

1. Core schema defaults.
2. One explicitly selected flat JSON config file, attributed as `user`.
3. Explicit CLI arguments and later `/config set` values, attributed as
   `session`.

There is no implicit config file, profile, project setting, or TOML config in the
new CLI. Only scalar JSON values are accepted. `plugins.enabled` is a
comma-separated scalar in that explicitly selected file and is parsed before
external plugins are loaded; repeatable `--plugin ID` adds explicit activations.
The core schema and schemas of those enabled plugins are then merged before the
full file is validated. Unenabled plugin settings are rejected. `/config get` and
`/config describe` show effective values, provenance, schema owner, and application
timing. `/config save` writes only explicitly named session overrides, and
`/config reload` validates a candidate file layer before committing it.

Fields marked `restart` remain desired configuration; changing them does not
replace the already-created provider/executor or resize the existing context
service in-place. Provider, router, interpreter, executor and wrapper selections
are resolved at startup; unknown or disabled selections fail without fallback.

Core fields cover provider/service IDs, model/options, explicit plugin activation,
router/interpreter/executor selection, wrappers, context capacity, executor startup
timeout, and maximum retained output characters. Plugin command, transform and
observer factories receive their enabled plugin's immutable namespaced values.
The Codex adapter does not enforce a client-side output token cap.

### Plugin discovery and activation

External plugins declare an entry point in `ipy_agent.plugins` and implement the
synchronous `py_agent_register` hook. Discovery lists entry-point metadata without
importing entry-point code; runtime loading imports only IDs explicitly supplied
through repeatable `--plugin ID` flags or `plugins.enabled` in the explicitly
selected, trusted JSON config file. The CLI does not search project/workspace
folders for config or plugin code. It merges core and enabled-plugin schemas
before validating the complete config, and selected plugin settings are passed to
command, transform and observer factories. Loading plugins is trusted Python
execution, not isolation. The public v1 API remains a draft.

The default CLI explicitly resolves provider, router, interpreter and executor
service IDs. `--executor ID` selects an enabled executor; repeatable
`--executor-wrapper ID` selects qualified wrappers in caller order. Router and
interpreter selections are exposed as options too. Unknown services, disabled
plugins, wrappers, and command collisions with terminal/config built-ins fail
startup; there is no local-executor fallback. `/plugins` reports metadata
separately from loaded manifests, and inspection itself never imports code.

Enabled plugins contribute their declared commands, transformations, observers,
and wrappers. Plugin commands dispatch through the coordinator and cannot shadow
built-in commands. Commands and wrappers are available after plugin activation;
context transforms, model-request transforms, and observers must additionally be
selected by qualified IDs through CLI flags or the corresponding config settings.
Unselected stages are excluded from the coordinator runtime. The coordinator
awaits selected observers sequentially. A general middleware or lifecycle hook is
absent. Context/observation service selection remains fixed to the built-in
production/bounded pair in the CLI.

### Optional Jupyter adapter

`pyproject.toml` places `ipykernel` and `jupyter-client` in the optional `jupyter`
extra; neither is required for the normal CLI. The CLI has experimental
`py kernel install/start/stop`, `py kernels`, and `py attach` commands. The
`kernel_sessions` module contains private connection/session storage, Linux
process-identity checks, stale-record cleanup, process stop, kernelspec install,
and stock-console attach helpers. The kernel entrypoint constructs the same
configured coordinator and starts it on the kernel's event loop. Launch uses
an isolated interpreter with the pinned installed package path, not workspace
module resolution. These helpers use private connection files and are lifecycle
hygiene, not a same-user security boundary. End-to-end startup remains unverified.

Current limitations include:

- Notebook cells use the same English-first routing as the terminal. Prefix
  direct Python with `@`; `!` and `%` retain their IPython meanings.
- Interactive stdin is correlated to the owning request when `allow_stdin` is
  true; the socket integration remains unverified. Queued `stop_on_error` is
  handled by the adapter; user expressions remain unsupported.
- Completion and static inspection use the selected persistent executor with
  bounded replies. Dynamic completers and user-expression evaluation are not
  supported; Jupyter history is adapter-local submitted source without outputs.
- The worker captures bounded rich MIME displays and the adapter translates
  them into Jupyter IOPub messages after the coordinator submission completes.
  Streaming and late-output subscriptions are not yet implemented.
- Managed-session helpers handle private connection files and verified process
  identity, and the CLI exposes start/list/stop plus stock-console attach. Forced
  kernel termination may leave an executor worker running if graceful shutdown
  failed. There is no complete multi-client input ownership contract. Stock
  Jupyter server/console interoperability has not been established.

Treat Jupyter support as experimental and unverified, not a finished notebook integration.

## Progress against the architecture plan

| Plan phase | Current assessment |
| --- | --- |
| Phase 0 — contracts and skeleton | **Partial.** Typed records, service protocols, plugin registration validation, and scalar config schema/store exist. The complete contract set, general hook model, and integration guarantees are not present. |
| Phase 1 — working vertical slice | **Implemented in structure.** The default terminal/coordinator/fake-provider/local-executor path exists, with direct and generated code sharing one namespace. This status is based on code review; tests were not run here. |
| Phase 2 — production behavior | **Partial.** Litelm and Codex production adapters, context policy, observations, and usage accounting are bridged. Optional durable journal/history is wired; rich event flow and complete accounting presentation remain incomplete. |
| Phase 3 — config and optional isolation | **Partial.** Typed config commands, plugin-schema merging, explicit trusted-config activation, and provenance are present. The opt-in wrapper-backed executor is available through explicit plugin activation; its isolation policy is not verified. The former SRT path has been removed. |
| Phase 4 — plugin boundary | **Partial.** External distributions/examples and the API draft exist; the CLI activates explicitly selected entry points and selects services, wrappers, and named stages. Middleware/lifecycle integration and a frozen contract remain gaps. |
| Phase 5 — Jupyter | **Experimental/incomplete.** Protocol adapter, manager commands, and private session/process helpers exist, the executable entrypoint is present but the managed path remains unverified; several core protocol services are absent. |

The completion criteria in `ARCHITECTURE_PLAN.md` are therefore not met. The
external plugin API is not frozen, enabled plugins run as trusted host code,
default execution is intentionally unrestricted, general middleware/lifecycle
hooks are absent, and Jupyter is not a managed product workflow. No tests or live
provider calls were run for this update.
