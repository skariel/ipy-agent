# Architecture plan: plugin-driven coding agent

## 1. Product and scope

`py` is an English-first coding agent with a persistent Python workspace. Ordinary input asks the agent to do something. Direct Python, shell commands and application commands remain available. User and agent code share an IPython namespace.

The application is a small dataflow coordinator surrounded by plugins. Its initial frontend is the existing terminal. A Jupyter kernel is added after the plugin architecture works independently of any frontend.

Execution is local and unrestricted by default, with the current user's permissions. There is no mandatory sandbox, permission broker, container runtime or network proxy. Users may launch `py` through their preferred VM/container/sandbox wrapper. Plugins may supply alternative execution environments or isolation for individual operations.

Do not describe unrestricted execution as permission-enforced or credential-isolated. Python can access files, subprocesses and network directly. A separate same-user process alone is not a security boundary. Per-operation isolation does not isolate arbitrary code executed elsewhere.

## 2. Architecture

```text
frontend plugin
    | typed user actions
    v
small core coordinator
    input routing -> context -> provider -> response interpretation
                            ^                    |
                            |                    v
                       observations <- persistent executor
                            |
                     output/event delivery
                            |
                  frontend + journal + observers
```

Built-in plugins implement ordinary application behavior. Third-party plugins use the same public contracts; they must not need private imports or monkeypatching.

### Stable core responsibilities

- Typed records and versioned service contracts.
- Plugin discovery, activation, validation and service selection.
- Configuration resolution and atomic configuration revisions.
- Session/request/execution identities and correlation.
- Dataflow orchestration and explicit state transitions.
- Cancellation, execution serialization and stale-result rejection.
- Hook dispatch, bounded event delivery and failure propagation.
- Lifecycle ownership and deterministic resource cleanup.

The core does not implement provider wire formats, context eviction policy, IPython execution, terminal formatting or filesystem permission policy.

### Plugin responsibilities

| Area | Initial implementation |
| --- | --- |
| Input routing | English, `@` Python, `!` shell, `%`/`%%` magics, slash commands |
| Commands | help, usage, history, reset, config, plugins, interrupt |
| Providers | Codex, API-key adapter, deterministic fake provider |
| Response interpretation | Extract executable Python, validate completion, preserve reasoning separately |
| Context | Append-only epochs, explicit reset, live namespace inventory, memories conventions |
| Execution | Local persistent IPython worker |
| Optional execution | Explicit wrapper-backed worker; future remote/container implementations |
| Observations | Model-facing output reduction, spill references and execution feedback |
| Persistence | Journal and history repositories |
| Accounting | Provider-reported usage and cache statistics |
| Presentation | Markdown, output excerpts and terminal styling |
| Frontend | Current terminal; Jupyter adapter in the final phase |

Some capabilities are required even though their implementations are plugins. Startup must fail clearly when an enabled feature lacks a required service. Optional journaling must be explicit: select a journal implementation or an explicitly configured no-persistence implementation, not silent loss of records.

## 3. Dataflow contracts

Define public typed records before extracting behavior. Prefer frozen dataclasses and defensively copied collections; shallow freezing is not deep immutability. Credentials and mutable coordinator internals are not general hook arguments.

Minimum records:

- `SessionInfo`: session ID, configuration revision, selected capabilities.
- `UserAction`: frontend ID, request ID, original text, submission metadata.
- `RoutedAction`: ask/direct execution/command, language, source.
- `ContextSnapshot`: immutable messages and context epoch.
- `ModelRequest`: request identity, model options, context and config snapshot.
- `ModelResponse`: text, reasoning, finish status, reported usage and adapter metadata.
- `ExecutionRequest`: source, language, author (user/agent), parent request, generation ID.
- `ExecutionResult`: status, error, namespace inventory and evidence references.
- `OutputEvent`: stream/display/error/update/clear, MIME data, display ID and origin.
- `InputRequest` / `InputReply`: owner, prompt, password flag, request correlation.
- `AgentDecision`: execute, finish, wait or reject with a reason.
- `ConfigChange`: validated revision, provenance and application timing.

User-visible rich output and model observations are separate paths. A dataframe, image or HTML display should not accidentally inject a large MIME payload into the next model request.

Every event carries session ID, parent request ID, applicable generation/execution ID, sequence number and configuration revision. Delayed output retains its original attribution. Frontends must not guess origin from whichever cell happens to be active.

### Request cycle

1. Validate and route the submitted action.
2. Dispatch a command or direct cell, or begin an agent turn.
3. Build and transform a context snapshot.
4. Build and transform the provider request; validate the final request.
5. Call the selected provider and interpret its response.
6. Validate the execution decision, run execution policy hooks, then dispatch.
7. Collect output and result, publish events, construct model observations.
8. Continue, wait or finish according to the decision policy.

Journal the final effective request and dispatched source, not only pre-transformation versions. Never record provider authorization headers or credential objects.

## 4. Pluggy and discovery

Use `pluggy` for declared hooks and `importlib.metadata` entry points for package discovery.

```toml
[project.entry-points."ipy_agent.plugins"]
example = "example_agent_plugin:plugin"
```

Discover entry-point metadata without importing every installed plugin. Load only explicitly enabled third-party plugins. Built-ins have an explicit default activation list. Never auto-load code from a workspace, notebook metadata or model output.

Each plugin declares:

- Stable plugin ID and supported plugin API version range.
- Configuration schema and defaults.
- Required and optional capabilities/dependencies.
- Registered services, transformations, commands and observers.
- Startup and shutdown resource ownership.

Validate duplicate IDs, unknown hooks, unsupported API versions, dependency cycles, command collisions and missing services at startup. Plugin loading is trusted arbitrary Python execution, not a security boundary. Dependency metadata that requires import is inspected only after explicit activation; `/plugins inspect` must distinguish discovered metadata from loaded runtime details.

### Hook categories

**Registration hooks:** synchronous Pluggy hooks register service factories and named pipeline contributions.

**Implementation selection:** config explicitly selects one provider/executor/context policy by ID. Multiple providers may be installed, but selection must never depend on import order. Use `firstresult=True` only for intentionally ordered resolver chains where `None` means decline; do not test results by truthiness.

**Transformations:** ordered async-capable pipeline stages return a new typed value or an explicit rejection. No shared-dictionary mutation between plugins. Record stage provenance and validate the final result before dispatch.

**Observers:** receive immutable lifecycle/output events; they do not modify the request by returning a value.

**Middleware:** a registered async callable may wrap a stage using an explicit continuation. Define whether it may replace, reject or invoke the continuation; for execution it may invoke at most once. No automatic replay of a side-effecting operation.

Pluggy itself does not await coroutine implementations. Keep Pluggy registration hooks synchronous; have them return/register async-capable services, transforms and observers that the coordinator awaits explicitly. Do not accidentally treat coroutine objects as hook results. Resource lifecycle is modeled with async context managers or equivalent explicit start/close methods.

### Ordering and failure behavior

- Named stages declare before/after dependencies; explicit configuration resolves overrides.
- Topological ordering is deterministic, with documented tie-breaking. Cycles are startup errors.
- Do not rely on Pluggy's registration order as implicit application policy.
- Provider/executor/policy/transform errors fail the affected operation. An explicitly selected isolation plugin must never fall back to local execution.
- Observers declare critical versus best-effort behavior. Durable journal failures follow the selected journal's declared failure policy; UI telemetry failures need not terminate execution.
- Propagate cancellation separately from ordinary failures. Apply bounded cleanup and mark uncertain side effects rather than replaying them.
- No plugin hot-unloading in the first version. Plugin-set and executor changes require restart.

## 5. Execution, concurrency and lifecycle

The default executor owns a persistent local IPython worker process. It has no sandbox runtime dependency. A separate process provides lifecycle and transport separation, not confidentiality or permission enforcement.

All user and agent executions use the selected executor; there must be no hidden direct-execution shortcut. Worker-extension packages, when needed, are explicitly registered separately from host plugins. No arbitrary host plugin objects are serialized into the worker.

Executor capabilities describe persistence, supported languages, rich output, completion, inspection, input, interrupt and namespace inventory. A stateless isolated executor cannot claim shared-variable semantics. Unsupported capabilities return explicit errors or documented reduced behavior.

Only one execution may own a persistent namespace at a time. Define serialized state transitions for idle, generating, executing, waiting-for-input, stopping and failed. Direct cells cannot race with generated cells. Model cancellation invalidates its generation; a late result cannot execute. Retrying providers is separate from retrying execution: never automatically retry uncertain execution.

Separate frontend attachment from session lifetime. Session IDs and provider affinity belong to the session, not to an attachment. Attaching a new frontend must not reset model context, accumulated usage or Python variables. Worker death loses Python objects; a journal does not restore them automatically.

Use bounded transport frames and explicit backpressure. Keep control/cancellation responsive even under output floods. Frontend slowness must not grow unbounded queues or silently drop required events. Define spill/truncation behavior separately for presentation, transport and model context.

## 6. Configuration as a public interface

One schema registry covers core and plugin configuration. Each field specifies type, default, documentation, constraints, sensitivity, owner and when it can apply.

Precedence, low to high:

1. Built-in defaults.
2. User configuration file.
3. Explicitly selected profile/config file.
4. Launch flags.
5. Session overrides.

No implicit project plugin activation. Settings and executable plugin loading are distinct operations. Sensitive values remain in credential backends where practical and are redacted everywhere configuration is displayed or journaled.

Suggested commands:

```text
/config
/config get model.effort
/config describe context.reset_threshold
/config set model.effort "high"
/config set output.preview_lines 12
/config reset output.preview_lines
/config diff
/config save
/config reload
/plugins
/plugins inspect example
```

`set` changes the current session; persistence requires `save`. Parse values as data, not Python expressions. `save` identifies its target file and scope, writes atomically, and persists only eligible explicitly selected settings—not credentials or every resolved default. `reload` validates a complete candidate before changing anything. Show effective value, provenance, masked overrides and pending restart requirements.

Configuration revisions are transactional. A rejected change leaves the active revision intact. Each in-flight request retains its immutable snapshot. Apply changes immediately, at the next request/cell/epoch, or after restart according to field metadata. Plugin validation must not cause partially applied side effects; staged changes are committed only after validation succeeds.

### Extract configurable policies

- Model selection, effort, verbosity and supported response-token settings.
- Context capacity, reset threshold and context policy.
- Generation retries and configurable transport/startup/shutdown timeouts.
- Model observation limits, spill policy, rich-output limits and history page size.
- Terminal preview lines, theme, colors, padding, status fields and editing mode.
- Input routing preferences and explicitly selected services/plugins.
- Plugin-specific namespaced settings.

Inventory hardcoded values across the repository. Classify them as user policy, implementation detail or invariant. Protocol versioning, execution serialization, valid message structure and hard resource ceilings are not arbitrary user preferences. Configurable limits must still be validated.

Avoid duplicate defaults in parser, provider and worker code. Generate help/reference material from schemas where useful. Adapt existing config fields through one loader rather than scattering compatibility logic across plugins.

Frontend settings are not universal kernel settings. A notebook owns its own colors/editor. `/config` labels frontend-local settings and reports when the attached frontend does not support them.

## 7. Commands, input and output

The router and command registry are plugins. The initial language remains:

```text
English             agent request
@Python             direct shared-namespace execution
!shell              direct IPython shell escape
%magic / %%magic     direct IPython magic
/command            application command
```

Provide an explicit way to ask English beginning with reserved characters. Route completion and completeness through the same syntax rules; account for stripped-prefix cursor offsets. Distinguish command execution from rendered text: `say('/config ...')` is output, never an application command.

Input requests are correlated to their initiating frontend and execution. Password input must not enter history, logs or ordinary output. A frontend can declare input unavailable; code requiring it receives a clear failure instead of hanging. Optional approval plugins use the same input abstraction but cannot claim enforcement in unrestricted execution.

Output contracts support stream fragments, MIME bundles, display updates/clears, errors and progress. The terminal plugin retains sanitization of untrusted terminal escapes. Rich clients apply their own rendering/trust rules; arbitrary worker output must not register host plugins or frontend control handlers.

Journals can contain private prompts and code even when credential fields are redacted. Keep private defaults and document publication/export risks.

## 8. Implementation phases

### Phase 0 — Public contracts and architectural skeleton

- Inventory current dataflow in coordinator.py, context.py, provider adapters, local_worker.py, plain_terminal.py and supporting modules.
- Define records, service protocols, capabilities, error types and ownership rules.
- Implement plugin discovery/activation and registration validation.
- Implement the typed config registry and resolution model.
- Document async dispatch and ordering before third-party hooks are exposed.

Acceptance: an empty coordinator can load a minimal built-in set; invalid plugin graphs and schemas fail deterministically; no frontend/private implementation types leak into the public API.

### Phase 1 — Working vertical slice

- Drive terminal submission -> fake provider -> local IPython executor -> output through the coordinator.
- Add a local executor independent of SRT/bwrap/proxy prerequisites.
- Extract router and response interpretation as plugins.
- Preserve explicit source attribution and shared namespace behavior.

Acceptance: English task and user Python share state, no concurrent execution, cancellation cannot dispatch stale source, and `py` starts without sandbox tools.

### Phase 2 — Extract production behavior

- Migrate Codex and API-key provider adapters behind provider services.
- Migrate context policy, observations, journal/history and usage accounting.
- Move commands and presentation behavior out of coordinator internals.
- Turn existing terminal into a frontend adapter.
- Keep deterministic fake services for development.

Acceptance: normal coding workflow operates entirely through public contracts; provider affinity persists; missing usage is not invented; context resets preserve only explicitly intended state.

### Phase 3 — Configuration and optional isolation

- Wire `/config` and `/plugins` into the shared command registry.
- Replace identified policy constants with schema-owned defaults.
- Implement provenance, revisions, validation, save/reload and restart behavior.
- Support optional explicit isolated executors without adding mandatory sandbox checks to installation/launch.
- Document unrestricted execution, external wrappers and isolated-operation limitations prominently.

Acceptance: users can inspect and modify supported settings; configuration failure is atomic; optional isolation selection never silently downgrades; ordinary installation does not require isolation dependencies.

### Phase 4 — Prove the plugin boundary

Create separate example distributions using only the published API:

1. A command/context-transform plugin with namespaced config.
2. An executor wrapper or alternate executor with declared capabilities.
3. An output observer that demonstrates backpressure/failure handling.

Exercise conflicting registrations, explicit ordering, cancellation and plugin errors. Freeze the first supported API contract only after these examples work without private imports. Do not promise compatibility for arbitrary supervisor internals.

Acceptance: meaningful behavior replacement requires no edits to core and no monkeypatching. Built-ins have no undocumented privileges unavailable to external plugins.

### Phase 5 — Jupyter kernel adapter

Implement the Jupyter adapter only now. Build on Jupyter protocol infrastructure and delegate execution to the selected executor; do not duplicate the agent loop or introduce a second Python namespace.

- Translate execute/complete/inspect/is_complete/history requests into core actions.
- Map streams, displays, display updates, errors and input to standard messages.
- Keep busy status for the whole multi-turn agent task.
- One public execution count per submitted cell; generated subcells use internal IDs.
- Respect parent headers, silent/store_history/allow_stdin/stop_on_error semantics.
- Implement interrupt/shutdown without bypassing coordinator lifecycle.
- Serialize multi-client execution and bind stdin replies to their owner.
- Keep kernel connection keys private; clients with those keys are trusted execution clients, not read-only observers.

Then add:

```text
py                         launch kernel and terminal client
py kernel install          register kernelspec
py kernel start            start independently managed session
py kernels                 list owned live sessions
py attach ID               attach terminal client
py kernel stop ID          stop explicitly
```

Use stock jupyter-console first. Standard clients will not automatically reproduce custom terminal status/panels; presentation enhancements are optional adapters. Text input is the portable selection fallback; rich widgets are optional, not guaranteed by every frontend.

Implement independent process ownership, private connection metadata and safe stale-record cleanup. Console detach must not accidentally shut down the kernel. Restart is not reattachment and does not preserve arbitrary Python objects. Stock JupyterLab attachment to externally started kernels needs separate server-manager integration; do not promise it from a connection file alone.

Acceptance: console and notebook execution work with the same coordinator; shared variables, output, input and interrupts behave correctly; attach/detach retains live state; shutdown cleans owned workers. No provider/context logic resides in the Jupyter adapter.

## 9. Verification and release gates

No test execution is currently authorized. Write regression and contract tests during implementation; run them only when the user authorizes it. Live-provider calls require separate approval.

Required coverage:

- Plugin compatibility, registration conflicts, ordering and dependency cycles.
- Config precedence, redaction, revisions and atomic persistence/reload.
- Shared namespace, user/agent authorship and route bypass prevention.
- Cancellation, stale completions, provider retries and no execution replay.
- Bounded output, slow observers, delayed output attribution and input ownership.
- Provider request serialization, session affinity and honest usage accounting.
- Executor capability negotiation and no implicit isolation fallback.
- Frontend-independent sessions and deterministic cleanup.
- Jupyter protocol behavior, execution counts, MIME output and multi-client semantics.

Manual terminal/notebook checks are a distinct gate; unit tests do not prove visual appearance, integration compatibility or cache-hit performance.

## 10. Completion criteria

- `py` is usable locally with no built-in sandbox prerequisites.
- All replaceable behavior is implemented through documented plugin contracts.
- The coordinator stays small and independent of provider, executor and frontend implementations.
- Configuration is inspectable, typed, attributable and explicitly mutable.
- Third-party plugins work as separately installed packages.
- The final Jupyter adapter adds ecosystem compatibility without redesigning the agent.
