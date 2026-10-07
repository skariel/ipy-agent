# Improving plugins, lifecycle, UI, and agent helpers

Status: design proposal. Existing support is described separately from proposed
APIs. Nothing here implements plugin installation, lifecycle hooks, or UI/helper
contributions. See also [the proposed JSON frontend](json-mode.md) and
[the future hot-reload design](hot-reload.md).

## Goals

Make plugins useful beyond service replacement: deterministic startup/shutdown,
frontend-aware UI contributions, and extensible Python helpers available to the
agent. Provide metadata-only listing and explicit install/uninstall management.
Keep explicit activation, immutable configuration, bounded output, and the
no-automatic-replay execution guarantee.

## Current architecture: what exists

### Discovery and registration

`src/py_agent/plugins.py` implements a Pluggy v1 draft with one registration hook,
`py_agent_register() -> Contributions`. Registration is synchronous and declares
resources; its hook documentation says not to start resources there.

`discover()` reads `ipy_agent.plugins` entry-point metadata without importing
plugin code. `PluginRuntime.load()` imports only explicitly enabled external
entry-point IDs, alongside explicitly supplied builtins. Installing a distribution
does not activate it. Unknown hooks, async registration, incompatible API ranges,
duplicate contributions, missing dependencies, and dependency cycles are rejected.
Manifest `requires` refers to plugin IDs, not Python package requirements;
dependencies must already be enabled. Dependency order is deterministic.

The registry is immutable; changing plugins requires building a new one.
Validation is transactional only for the registry: imports are trusted Python,
and arbitrary import/registration side effects cannot be rolled back.

### Contributions and runtime ownership

| Existing contribution | Current behavior |
| --- | --- |
| `Service` | Selected factories for router, provider, interpreter, executor, and other configured services; legacy command services are adapted. |
| `ConfigField` | Typed, owned plugin namespaces with immutable snapshots and immediate/request/cell/epoch/restart application boundaries. |
| `CommandContribution` | Slash-command factory receives its namespace; `execute(arguments)` returns text or awaitable text. No general command/UI context. |
| `TransformContribution` | Async context or model-request stages, selected explicitly; deterministic named ordering and contract validation. |
| `ObserverContribution` | Selected output observers, awaited sequentially for backpressure; critical failures propagate, noncritical failures are recorded. |
| `ExecutorWrapperContribution` | Explicitly selected named executor decorators. |

`cli.py` resolves enabled IDs and selected contributions.
`coordinator.py` constructs services and observers synchronously;
`coordinator_runtime.py` rejects awaitable factories, samples config at the
appropriate boundary, and creates command/transform instances on demand.
There is no universal instance lifecycle or task/resource owner for plugins.

`coordinator_lifecycle.py` handles executor start/interrupt/close, active work
cancellation, uncertain execution, and journal closure. It is **not** a
plugin-wide startup/shutdown protocol; arbitrary provider, observer, command, or
transform resources have no uniform teardown contract.

`coordinator_frontend.py` dispatches immutable output events to selected observers
and a per-request progress callback. Output observation is not a session lifecycle
bus or a terminal layout API.

`PlainTerminal` directly owns `PromptSession`, bindings, toolbar, composer, and
rendering. There are no declarative widgets, shortcuts, dialogs, or custom renderer
contributions. Existing commands cannot shadow protected builtins.

### Management today

`ConfigCommandService.inspect_plugins()` and `/plugins inspect ID` distinguish
metadata discovery from loaded manifests without activating unknown plugins.
The current `/plugins` interface is inspection-only. There is no non-TTY plugin
management CLI and no integrated installer or uninstaller.

The separately packaged examples under `examples/plugin_command`,
`examples/plugin_observer`, and `examples/plugin_executor` demonstrate entry points.
`tests/test_plugins.py` and `tests/test_external_plugins.py` cover activation,
validation, configuration, ordering, backpressure, cancellation, and observer
failure policy.

### Agent standard library today

`stdlib.py` has a fixed `HELPERS` registry describing `say`, `preview`,
`read_output`, `collapse`, `llm`, `source`, and `test_summary`.
`install_helpers()` requires implementations to match that exact registry.
`context.py` inserts `helper_prompt()` into the model contract at module load.

`local_worker.py` installs actual bindings for each execution, including a fresh
cell printer. `llm` uses a validated worker/host bridge in `local_executor.py` and
`host_stdlib.py`; credentials and provider clients stay on the host.
`collapse` is special syntax recognized by `collapse_control.py` and the
coordinator, not an ordinary worker callable.
`display` is IPython display functionality, captured by the worker's display
publisher, not a `HELPERS` entry.

The user or agent can already define functions, import installed modules, and
retain them in the persistent namespace. That does not register model-visible
documentation or a durable extension. Rebinding injected core helpers is not a
supported customization mechanism; subsequent cells reinstall their bindings.

## Comparison with pi

References checked: pi's installed extension/package documentation and extension
type declarations; these APIs may evolve.
[Extensions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)
and [Packages](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/packages.md).

| Area | Pi | Py implication |
| --- | --- | --- |
| Registration | TypeScript extension factories, possibly async; tools, commands, providers, flags, shortcuts, events. | Keep declarative registration; async resource work belongs in lifecycle hooks, not imports. |
| Lifecycle | `session_start`, idempotent `session_shutdown`, session/agent/message/tool events, explicit settled boundary. | Add typed session and request boundaries; don't equate a successful cell with a completed agent request. |
| UI | `ctx.ui` dialogs, notifications, status, widgets, editor access, custom terminal components. | Introduce frontend capabilities and UI ports, not access to terminal internals. |
| Modes | Full TUI; supported RPC dialogs/notifications; JSON/print have no UI. | Headless plugins must remain functional and must not prompt or pollute stdout. |
| State | Session entries, tool-result details, branch reconstruction. | Distinguish ephemeral worker state from persisted audit data; py's journal is not worker restoration. |
| Distribution | `pi install`, `pi list`, `pi remove`, package declarations and project trust; npm/git/local sources. | Add Python distribution management while retaining explicit config/project trust boundaries. |

Pi handlers have event-specific notification, transform, or cancellation contracts.
Py should likewise avoid a generic hook that returns arbitrary dicts.
Pi tools have schemas and optional UI rendering; py primarily acts through Python
cells. The closest extension surface for agent actions is a callable Python helper,
not necessarily a second tool-call protocol.

Borrow lifecycle ownership and mode-aware UI, not pi's implementation or automatic
loading conventions. Neither system's plugins are a sandbox.

## Proposal 1: list, install, uninstall, enable

Separate **distribution installed**, **entry point discovered**, **plugin enabled**,
**contribution selected**, and **runtime started/failed**. A distribution can
publish multiple plugin IDs. Never assume its package name equals an entry-point
name or manifest ID.

### CLI and inspection

Proposed commands (not implemented):

```sh
py plugins list
py plugins list --json
py plugins inspect example-command-context
py plugins install 'py-agent-example-command-context==0.1.0'
py plugins install ./reviewed-plugin.whl
py plugins enable example-command-context --config /path/to/config.toml
py plugins disable example-command-context --config /path/to/config.toml
py plugins uninstall py-agent-example-command-context
```

Dispatch management commands before terminal TTY checks, coordinator creation,
provider/auth setup, or worker startup. No credentials should be required.
Preserve ordinary agent CLI behavior; these are top-level management verbs, not
prompts. `/plugins` and `/plugins inspect ID` should use the same read-only
inspection service. Do not install packages from an active agent cell.

`list` reports distribution/version, entry-point ID/target, location, ownership
(managed/unmanaged), enabled state for the selected configuration, selected
contributions, and restart requirements. Runtime-only facts are unknown outside a
running session. `inspect` does not load plugin modules to retrieve a manifest;
label imported/runtime facts unavailable when only metadata exists.
`--json` here means a single versioned management-result object, not the
[agent JSONL stream](json-mode.md).

Keep activation explicit: `install` installs but does not enable.
`enable` changes the specified config atomically after validating known IDs;
it must not invoke plugin code to “preview” activation. Full manifest/service
compatibility is checked during explicit runtime loading. Dependencies known only
after import cannot be silently enabled by metadata inspection.

`disable` must report other enabled plugins that depend on it when manifests are
known; never silently cascade. Don't discard plugin settings on disable: quarantine
disabled-plugin settings before active schema validation, and retain them for
re-enabling. Today settings for disabled plugins are rejected; changing that
behavior is part of this work, not something the manager can assume already works.

### Installation environment

Do not run bare `pip` against whichever interpreter happens to be on PATH.
Management must name the runtime interpreter and target environment explicitly.

Recommended managed mode: one dedicated, versioned virtual environment containing
the compatible py-agent host and its plugins, selected by a thin launcher.
Both metadata inspection and agent startup use that environment. The persistent
worker receives the relevant environment/helper plan as well; verify it rather
than assuming executor wrappers inherit the same interpreter. Do not merge
multiple site-packages trees by ad hoc `sys.path` modification.

For v1, if that launcher is not yet available, support only an explicitly selected
writable virtual environment and use its interpreter's package installer.
Refuse system/externally managed environments; explain the target and migration
steps for uv-tool/pipx installations. Do not bypass package-manager protections.
Installing into an environment the running host cannot discover is not success.

Track managed installs in an atomic inventory/lock record: distribution name,
resolved version, source and artifact hash where available, runtime environment,
entry-point IDs from installed metadata, and management ownership.
The inventory is not a second activation config.
Use an environment lock for concurrent managers; reject modifications while that
managed runtime has an active session, including processes in other terminals.

Use argument arrays, never shell interpolation. Capture installer output on stderr
(or structured diagnostics), not management JSON stdout. Initial sources should
be pinned index requirements and reviewed wheels/local sources; avoid inventing a
git checkout/updater protocol before Python packaging support is solid.
Source builds execute arbitrary build code; warn and require explicit consent for
untrusted source builds. A wheel can still execute arbitrary code when enabled.
Project-local declarations must not install or enable code automatically from a
repository checkout; require explicit trust/approval and show the resolved path.

Resolve and check dependencies against host/API constraints before committing a
candidate managed environment. Prefer staging a replacement environment, testing
metadata/dependency consistency, then switching the launcher pointer atomically.
Do not claim an in-place package install has rollback guarantees.
A partial failure must be reported and reconciled in inventory; never import the
plugin as an installer health check.

### Uninstall semantics

Uninstall operates on a **distribution**, not a plugin ID. `inspect` can show the
mapping; an ambiguous plugin-ID shortcut must ask for an explicit distribution.
Show all affected entry points and configs known to the manager.

Refuse to uninstall the host, core dependencies, unmanaged distributions, or
packages required by remaining installs. Don't automatically remove dependencies:
they may be shared. Refuse when the selected config still enables affected IDs;
offer an explicit `--disable` transaction with a dry-run preview.
That option may edit only explicitly selected/managed config files, never arbitrary
user files found by scanning the disk.

Known configurations are not all configurations: warn about other files that may
retain stale IDs, and ensure subsequent startup gives an actionable missing-plugin
error. Retain settings/data by default; a separately confirmed `--purge` removes
only manager-owned records for this distribution.

Installed code is already imported in active sessions. No hot uninstall/reload:
changes take effect on the next session. Keep install/uninstall out of model-facing
helper capabilities, and treat a request to install agent-written plugin code as a
user approval boundary, not implied permission from generated Python.

## Proposal 2: explicit runtime lifecycle

Keep `py_agent_register()` declarative and synchronous. Add a version-negotiated
lifecycle contribution whose factory builds a session-scoped owner. This is an
additive contribution surface with a new advertised API capability/version;
old v1 plugins retain their existing behavior. Do not silently interpret arbitrary
`start`/`close` methods on existing services as plugin lifecycle hooks.

Illustrative public contract, not current code:

```python
class PluginSession:
    async def start(self, context: SessionContext) -> None: ...
    async def stop(self, context: ShutdownContext) -> None: ...
```

`SessionContext` provides identity, immutable namespaced config, capabilities,
structured logging, cancellation, and managed resource/task registration.
It does not expose mutable coordinator internals, provider credentials, or direct
worker execution. Register cleanup before awaiting risky initialization.
The resource owner can release partially initialized resources even when `start`
never returns.

### Ordering and failure policy

1. Discover metadata, import explicitly enabled plugins, validate declarations
   and API ranges, and create the selected service graph without async resources.
2. Establish session resources and start the executor through existing lifecycle
   code. Run plugin startup in manifest dependency order. Helpers are negotiated
   and installed before declaring the worker ready.
3. Publish session-ready only after mandatory startup and helper validation
   succeed. Plugins requiring UI wait for frontend attachment, not session startup.
4. On failure, stop accepting work; roll back acquired plugin resources in reverse
   acquisition/dependency order, then close core resources. Include the failing
   plugin's partially acquired cleanup stack. Preserve the original error and
   report cleanup failures separately.
5. On normal shutdown, reject new requests, cancel/drain owned request tasks,
   detach UI, stop plugins in reverse dependency order, close executor/services,
   then close the journal after final lifecycle records.

Lifecycle hooks run sequentially, with deadlines and a cancellation scope.
Shutdown cleanup gets a bounded cancellation-resistant window; no task may hang
process exit indefinitely. Calling coordinator shutdown twice must not run plugin
teardown twice. Hard process death cannot guarantee hooks; document that limitation.

Default startup failure is fatal. An explicitly optional, notification-only
contribution may fail disabled with diagnostics; required dependencies or selected
executor/provider/helper capabilities cannot be silently replaced. During shutdown,
try every remaining cleanup even after one fails. Managed tasks cannot outlive their
session/frontend/request owner. Do not start background tasks with untracked
`create_task()` in examples.

### Lifecycle notifications are not interception

Add typed notifications with immutable payloads and explicit identity:
`session_ready`, `frontend_attached`, `frontend_detached`,
`request_started`, `request_finished`, `session_stopping`.
A request outcome includes completed/failed/paused/cancelled/uncertain, using the
explicit outcome contract proposed for JSON mode. Notifications do not alter
completion or replay execution. Keep output observers and context/model transforms
as their existing distinct contracts; add cancellation/veto hooks only when their
semantics and failure policy are individually specified.

Avoid reentrant submission from hooks while the coordinator operation lock is
held. Queue post-boundary actions through an explicit scheduling port if later
needed, never call `submit()` recursively. A lifecycle hook must not send raw text
to stdout; use structured logs or declared output/UI ports.

Session instances own long-lived resources. Existing per-request command/transform
factories remain compatible; add a scoped acquisition context for transient
resources, released on every success/failure/cancellation path. Instantiate once
per session only through an explicitly declared scope, not caching arbitrary old
factories. Track replacement/cleanup across existing config application boundaries:
immediate values do not justify rebuilding an epoch resource mid-cell.

## Proposal 3: frontend-aware UI plugins

Separate **agent services** from **frontend contributions**. A headless session
must not import terminal-specific dependencies merely because a plugin also has UI.
Define declarative UI contributions with separate factories, invoked only when a
compatible frontend attaches.

`FrontendContext` identifies the frontend and advertises capabilities such as
`notifications`, `status`, `widgets`, `dialogs`, `editor`, and `shortcuts`.
Support multiple frontend attachments; UI state is scoped to `(plugin, frontend)`,
not a global “current terminal”.

### Start with portable primitives

- `notify(level, text)`: a structured notification. JSON mode can emit a versioned
  notification record; it must never leak raw terminal output onto stdout.
- `set_status(key, text | None)` and `set_widget(key, content | None)`: bounded,
  namespaced, frontend-local state with handles cleared on detach.
- `confirm`, `select`, `input`: async requests only when dialogs are supported.
  Headless returns a typed `UIUnavailable`, not a guessed answer.
- `get_editor`/`set_editor`: explicit composer capability, never implicit execution.
  Programmatic submit goes through coordinator policy and user intent.
- Declarative commands and shortcuts: protected builtins/reserved keys cannot be
  overridden; duplicates require explicit user resolution, not last-writer wins.
  An optional command context adds UI/cancellation/identity without breaking the
  existing `execute(arguments)` command contract.

Do not expose PromptSession, arbitrary ANSI output, or mutable toolbar objects in
the portable API. `PlainTerminal` implements this adapter and owns layout,
sanitization, rendering, focus, and invalidation. Limit text/asset size and update
rate; coalesce superseded status updates in a bounded queue.

A later advanced `terminal-components` capability may accept prompt-toolkit
components through a separate optional package/API. That contract is terminal-only,
with frontend-thread scheduling and dispose handles; it cannot be represented as
an arbitrary object in JSON or Jupyter. Avoid making it the first UI milestone.

### Mode behavior and cleanup

| Frontend | Behavior |
| --- | --- |
| Plain terminal | Portable UI first; opt-in advanced components later. |
| JSON one-shot | No dialogs, editor, or shortcuts. Notifications may use schema records; widgets/status remain unavailable until explicitly specified. |
| Jupyter | Capability adapter where notebook support exists; no terminal bindings. |
| Future RPC | Explicit correlated UI requests/responses and cancellation, not assumed present today. |

UI-required plugins must declare that requirement and fail clearly on headless
attachment; optional UI plugins degrade without blocking core services.
Do not “confirm” automatically when no UI exists. Password dialogs must use the
existing input/redaction discipline and never enter model context or logs.

Attach only once the frontend is ready; detach before its event loop closes,
dispose handles, and cancel pending dialogs/tasks. A notification cannot recursively
trigger a request. UI state does not enter the model context by default; publish
model-visible content only through an explicit audited channel.

## Proposal 4: extensible agent helpers

### Three ways to add functionality

1. **Ordinary Python:** the user or agent defines functions/imports modules in the
   persistent worker today. No approval is required beyond existing execution
   policy. Definitions are session-local, may have arbitrary Python side effects,
   and disappear with the worker. Do not claim sandboxing or restart persistence.
2. **Reusable worker library:** a user installs/enables a reviewed helper plugin
   containing importable Python callables. It supplies bounded model documentation
   and explicit bindings; ordinary imports remain available too.
3. **Host-backed capability:** a trusted plugin registers a validated host handler
   and worker proxy for actions requiring host context. This is a separate
   permission/protocol boundary, analogous to `llm`, not a serialized callable.

No dynamic `pip install`, host registration, or durable trust grant should follow
automatically from an agent-written definition. Users can promote reviewed code
into a packaged plugin; the manager handles its installation and activation.

### Registry and namespace

Add a `HelperContribution` with, at minimum:

- stable qualified ID (`plugin_id:helper`), version, explicit implementation target;
- invocation signature, concise description and examples, documentation budget;
- scope (`session` or `cell`), supported executor capabilities;
- worker-local or host-backed execution kind;
- input/output schema and limits for host-backed helpers;
- declared side effects, resource/cancellation requirements, optional short alias.

Use import targets and serializable specs, not pickled functions/closures.
Worker-local helpers can accept normal Python objects within that process; they
do not need a JSON schema unless crossing the host boundary.

Keep core globals reserved: `say`, `collapse`, `preview`, `read_output`, `llm`,
`source`, `test_summary`, input/display infrastructure, and archive namespaces.
Reject collisions before worker startup. Do not permit plugins to change final
completion or collapse semantics by replacing a core helper.

Expose extension bindings in a session registry, for example
`helpers["example:search"](query)`, with optional user-selected aliases.
The core API remains unchanged; no automatic injection of every package function.
An importable binding accessor must resolve the active worker session, not retain a
stale singleton. Alias collisions with existing user variables fail visibly.
Reserve selected aliases at startup; do not clobber unrelated user definitions on
each cell.

### Worker installation and model documentation

Replace the fixed-only install check with validation against an immutable
**session helper plan**: builtins plus explicitly selected contributions.
Send that plan during executor startup, validate targets/capabilities in the worker,
and acknowledge installed IDs/versions/signatures before declaring it ready.
`runtime_helpers()` remains the source of core bindings.

Install session-scoped libraries once; refresh cell-scoped bindings alongside the
existing printer/LLM bridge. Resources follow the plugin owner lifecycle, and
cell-scoped cleanup runs in `finally`. External executors/wrappers must advertise
helper-install/host-call support; unsupported required helpers fail before work.
Never tell the model a helper exists if the actual worker did not install it.

Generate the helper prompt from that acknowledged plan per session. Today
`context.CONTRACT` expands the fixed registry at import time; making `HELPERS`
mutable would leak documentation across sessions and still not install bindings.
Keep a static core contract plus a per-session extension section. Bound total
documentation and redact secrets. Record a helper-plan fingerprint with request
config so completion checks and audit logs identify the version actually used.

Changes to selected helpers are restart-bound in v1. Config changes follow declared
application boundaries; side-effecting helper implementations cannot change halfway
through a cell. A name/version in a journal is provenance, not serialized worker
state or a promise that restoring the journal reinstalls that helper.

### Host-backed calls

Extend `worker_protocol.py` with versioned helper request/response frames and
session, execution, helper, and call IDs. Validate payloads on **both** sides,
enforce allowlisted active helper IDs, frame/result size, call count, deadlines,
active-cell/main-thread rules, and cancellation. Never unpickle incoming values
or eval returned text. Use explicit asset references or existing bounded image
transport where needed.

Host handlers use a narrow context, not unrestricted coordinator objects.
Declare capabilities such as network or protected host access and require user
approval when enabling them. Capabilities describe/audit access; they do not
sandbox trusted Python plugins or unrestricted worker code.

Do not automatically retry side-effecting calls. On disconnect/cancellation,
report uncertain outcome if work may have happened; a correlation ID alone does
not make replay safe. Redact credentials/passwords and identify helper ownership
in structured usage/audit events. Return errors as typed failures the cell can
handle without confusing them with frontend lifecycle failure.

A UI-backed helper checks the actual frontend capability. In headless mode it
raises `UIUnavailable`, never reads the prompt pipe. Avoid lock inversion: handlers
must not recursively submit work or execute a worker cell while servicing its call.

### Agent-authored helpers

A future `helpers.describe_session(...)` could attach a bounded signature and
description to an existing callable without installing a package or granting host
permissions. Label it agent/user-authored and session-local; include it only in
subsequent requests. Registered descriptions are untrusted data, not system-policy
overrides. Record provenance, unregister on worker reset, and verify the binding
still exists before advertising it.

This is optional and later than packaged helpers. For v1, the agent can already
define and reuse functions and mention their signatures in context. Persistence
requires explicit user review and a package/config change, never automatic code
promotion.

### Inspection

Add metadata/plan inspection commands such as `py helpers list --json` and
`/helpers inspect plugin:helper`. Outside a session, report only declared metadata,
not imported runtime facts. The active session can show installed plan, version,
aliases, docs, backend, limits, and ownership. Package metadata must carry a static,
versioned summary if contributions are to be discoverable without activation;
otherwise show “unknown until enabled”.

Helpers are contributions of ordinary plugins: uninstall removes the distribution,
disable prevents future installation, and active sessions must be restarted.
No parallel helper package manager or hidden global registry is needed.

## Delivery plan and acceptance criteria

Ship small compatible increments, not a replacement plugin framework:

1. **Inspection and management:** extract shared metadata-only inspection from
   `config_commands.py`; add non-TTY `plugins list/inspect`. Define the environment
   ownership/lock model before implementing install/uninstall and config edits.
2. **Resource lifecycle:** add explicit session owners, typed notifications,
   deadline-bound cleanup, and service acquisition scopes. Keep the existing
   executor cancellation/uncertainty rules and observer backpressure tests.
3. **Portable UI:** introduce a small frontend port and adapt `PlainTerminal`.
   Start with notifications/status and command context; add dialogs and shortcuts
   only with capability, cancellation, and collision coverage.
4. **Packaged helpers:** add versioned session plans, worker capability negotiation,
   runtime-derived prompt documentation, and inspection. Deliver worker-local
   helpers first; add the generalized host bridge after protocol/security tests.
5. **Later:** advanced terminal components, RPC UI, optional session-local helper
   descriptions, and safe staged updates. No hot code reload in the first version.

Use versioned capability declarations and migration examples. Unknown contributions
must fail clearly, never be ignored as “optional” by an older host. Keep old plugins
working when they request only v1 features. Update public module exports and typing
stubs alongside runtime contracts.

### Tests

- Metadata listing/inspection imports no plugin code and needs no TTY/provider.
  Duplicate entry points, multiple IDs per distribution, unavailable manifests,
  unmanaged installs, and version/API incompatibility are visible.
- Install targets the actual selected runtime. Test a fake installer, staged
  environment failure, source-build consent, lock contention, host/dependency
  protection, partial failures, uninstall of enabled/shared packages, stale config
  IDs, and atomic config/inventory edits. Do not contact package registries in
  unit tests.
- Plugin startup ordering, dependency failure, partially initialized cleanup,
  reverse teardown, double shutdown, slow/raising hooks, owned task cancellation,
  and journal-close ordering preserve the original failure and release resources.
- No notification hook can reenter submit or cause execution replay.
  Failed/cancelled/paused/uncertain request outcomes remain distinguishable.
- Multiple frontend attachments isolate UI state. Verify detach cleanup, reserved
  shortcuts, widget size/rate limits, cancelled dialogs, password redaction, no-UI
  behavior, and no raw stdout contamination in JSON mode.
- A packaged helper works in the real local worker; custom executor capability
  refusal is explicit. Test alias collisions, reserved names, per-cell freshness,
  session cleanup, plan fingerprinting, and model/runtime documentation agreement.
- Host helper tests reject unknown IDs, malformed/oversized frames, stale calls,
  excess calls, cross-session responses, unsupported assets, and worker-thread
  calls. Cancellation/transport loss never retries a side effect.
- Defining a Python function still works without registration. Session description,
  if implemented later, grants no host permissions and does not persist secretly.
- Run existing plugin, external-plugin, stdlib, terminal, worker, and coordinator
  regression tests. New examples should separately package a lifecycle observer,
  portable UI extension, and worker-local helper without private module imports.

## Decisions to settle before coding

- Managed launcher versus explicitly selected existing virtual environment: choose
  one supported v1 path and document discovery/executor behavior end to end.
- Which config files are manager-owned, and how disabled-plugin settings are
  quarantined without weakening active schema validation.
- Public API version/capability negotiation and precise lifecycle deadlines.
- Minimal portable UI/command context versus optional terminal-component package.
- Host-helper permissions and audit/result limits; align with existing LLM and
  input transport instead of adding an unconstrained RPC channel.

The important separation is: **installation distributes code; activation trusts
it; lifecycle owns resources; frontend capabilities own UI; helper plans describe
what the agent can actually call.**
