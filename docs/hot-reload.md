# Hot reload: boundaries, generations, and recovery

Status: future design proposal; runtime plugin/code hot reload is not implemented.
This does not change the initial “no hot code reload” scope in
[the plugin architecture proposal](plugin-architecture.md).
See also [JSON mode](json-mode.md) for headless behavior and outcome reporting.

## Goal

Allow selected configuration and extension changes without unnecessarily losing
the persistent Python worker. Never replay executed cells, replace resources during
their use, or disguise a worker reset as a harmless reload.

“Reload” needs a precise target. Updating installed files, re-reading configuration,
rebuilding UI, and replacing a Python module are different operations.

## What exists today

- `configuration.py`: `ConfigStore.reload()` validates a complete candidate before
  committing it and preserves session overrides. This is configuration reload,
  not code reload or plugin activation.
- `config_commands.py`: `/config reload` re-reads the selected JSON configuration
  file, validates it, and reports its new revision. Session overrides remain.
- `plugins.py`: an immutable registry built by importing explicitly enabled
  entry points. Changing installed files does not rebuild it.
- `coordinator_runtime.py` and `coordinator_conversation.py`: service/configuration
  selection observes immediate, request, cell, epoch, and restart boundaries.
  Desired configuration and active restart/epoch snapshots are distinct.
  A context epoch is not a worker process restart.
- `coordinator_lifecycle.py`: serializes submissions, tracks queued work, and owns
  interruption/closure. Idle admission alone is not yet a full reload barrier.
- `PlainTerminal`: owns UI state directly; there is no detach/rebuild plugin port.
- `stdlib.py` and `local_worker.py`: core helper bindings are installed in the
  worker. There is no extension helper-plan swap protocol.
- Model recovery is session-local and retries pending generation, not completed
  execution. An uncertain cell is not a candidate for automatic replay.

The plugin proposal adds lifecycle owners, UI ports, managed installation, and
helper plans. Hot reload depends on those contracts; it should not precede them.

## Scope and terminology

| Operation | Intended boundary | Worker namespace |
| --- | --- | --- |
| Config reload | Each field's declared application boundary | Preserved |
| UI refresh | Frontend detach/attach with disposed handles | Preserved |
| Plugin runtime replacement | Quiescent session, opted-in contributions | Preserved only if no worker changes are required |
| Helper-plan replacement | Quiescent worker plus explicit binding negotiation | Preserved with compatibility checks, or refused |
| Package upgrade/uninstall | No active session using the environment | Unchanged only for already running code; restart required |
| Worker reset | Explicit destructive operation | Lost |

Initial support should be config reload and portable UI refresh. Full plugin/helper
replacement is a later opt-in capability, not a guarantee for every plugin.
Provider, executor, protocol, core helper, and dependency changes require a session
restart until a dedicated migration contract exists.

An executor may retain objects internally even when the user namespace is unchanged.
Advertise only the state preservation actually supported by its capabilities.

## Public operations (proposed)

Illustrative terminal commands, not current APIs:

```text
/reload inspect
/reload plan ui
/reload apply PLAN_ID
```

Plans should identify affected plugins/contributions, candidate versions, config
revisions, required boundaries, blocked tasks/recovery, and whether worker state
would be preserved. Bind each plan to its base generation and reviewed artifact
hashes. Applying a stale plan fails; it does not silently re-plan and commit.

Do not overload `/config reload` with package import or code execution.
Do not implement generic `importlib.reload()` behind `/reload`.
Use a user-initiated action for mutating runtime behavior; a development watcher
may propose a plan, but must not continuously apply edits during executions.

The one-shot JSON frontend has no interactive reload channel. It uses one stable
runtime generation for its invocation. A future RPC transport may expose
plan/apply operations with explicit authorization and versioned responses.
Do not treat text on stdin as reload commands.

## Safety invariants

1. Every request, cell, host-helper call, callback, and resource lease belongs to
   a runtime generation. No in-flight operation sees a half-old, half-new graph.
2. Only one reload transaction can run; shutdown has priority. Use the existing
   session admission mechanism plus an explicit reload state, not a parallel lock
   that can deadlock with submission or host calls.
3. Stop accepting new requests while applying a plan. Reject or hold them in a
   bounded, visible queue. A candidate must not capture a moving request queue.
4. No active generation, Python cell, nested `llm`, input request, UI dialog,
   host helper call, or plugin task may cross a replaced resource boundary.
5. Do not interrupt work automatically to force reload. Return `busy` and explain
   the blockers; cancelling work is a separate explicit operation.
6. Pending generation recovery or uncertain execution blocks runtime replacement.
   A pending checkpoint retains its original graph/config. Resume or explicitly
   discard it before replanning. Discard does not undo side effects or resolve
   uncertainty; an unknown execution outcome requires reconciliation or restart.
7. Lifecycle hooks and migration steps never submit or replay agent cells.
8. Installation/activation is not implicit. A watcher never trusts new code or
   installs dependencies. Model-generated code cannot grant itself reload rights.
9. Errors must describe whether the old graph remains usable, the new graph is
   active, or the session requires restart. Never claim rollback of arbitrary
   Python/import/network side effects.

## Configuration and UI first

### Configuration

Keep existing all-or-nothing candidate validation and session overrides.
Show desired versus active values and pending boundaries after reload.
Do not classify a restart-bound field as immediate to make it “hot”.
Changes selecting new plugins or altering contribution schemas need a new registry;
the configuration loader alone cannot apply them.

### UI refresh

Use the proposed frontend capability port, not direct terminal mutations.
Cancel pending dialogs with a typed reason, stop UI-owned tasks, dispose shortcuts
and handles, then attach a new frontend-local instance. Protect builtin bindings,
redact password state, and do not send editor contents to the model automatically.

Only snapshot explicitly declared serializable UI state: for example selected tab
or bounded widget preferences. Never retain component objects or closures as a
migration format. Preserve the user's unsent composer text and cursor through the
frontend's own supported API; if unavailable, refuse a refresh that would lose them.

Refresh one frontend at a time. Other attached frontends keep independent state.
Failure before detachment leaves the old UI untouched; failure after detachment
leaves a usable builtin UI and a disabled extension with diagnostics, unless an
explicit reattachment contract can safely restore it. Do not promise arbitrary
terminal component rollback.

## Plugin replacement protocol (later)

Add a declared reload capability per contribution, defaulting to
`restart_required`. Supported categories can include `stateless_replace` and
`explicit_migration`; “the plugin has a close method” is not sufficient.
Compute the dependency closure: retained dependents may hold references to replaced
objects and therefore also require replacement or an explicit rebinding contract.

Prefer fresh instances of already loaded, compatible code. This can refresh
resources/settings without claiming source code has been updated.
A live plugin graph must not mutate the existing immutable registry.

### Prepare, quiesce, commit, retire

1. **Plan:** metadata/config validation and capability checks identify the full
   affected graph and its owners. Metadata inspection does not import code.
   Run any imported candidate validation only after explicit user authorization.
2. **Quiesce:** close admission and wait for all affected operation leases.
   Prove there is no pending recovery, active worker action, or dependent UI/task.
   Time out visibly with the old generation still active; never force replay.
3. **Prepare:** construct the candidate graph with its own cleanup owner. Validate
   services, schemas, helper declarations, and UI factories. Preparation cannot
   publish output, start background work, or acquire exclusive resources without
   a declared staging contract. Import and construction can still have arbitrary
   side effects; cleanup is bounded, not magically transactional.
4. **Migrate:** if requested, export bounded versioned data and explicitly import it
   into the candidate. No pickled closures or live resource handles. Treat export
   as read-only; a destructive “export” cannot support safe rollback. Secrets use
   approved private channels and never enter plans/logs.
5. **Activate:** for genuinely stageable resources, start the candidate with
   outbound dispatch suppressed and readiness checks complete. Under the barrier,
   atomically publish the new graph and generation, then enable delivery.
6. **Retire:** close the old graph in reverse dependency order. Keep admission
   closed until teardown is complete. Stale callbacks must fail or be ignored
   with diagnostics, not reach the new graph.
7. **Release:** record the outcome and reopen admission only when safe.

Some resources cannot coexist: ports, file locks, subscriptions, or worker bindings.
Require an explicit handoff protocol; otherwise mark replacement restart-required.
Stopping an old service first destroys the easy rollback point. A plugin that
supports handoff must document how failure leaves the session paused and how a
fresh old instance can be reconstructed without replaying work.

### Failure semantics

| Failure point | Result |
| --- | --- |
| Planning or quiescence | Old graph remains active; no applied change. |
| Staged preparation before old resources change | Dispose candidate; keep old graph if cleanup succeeds. |
| Exclusive handoff or worker binding mutation | Pause; restore only with a proven inverse operation, otherwise require restart. |
| After graph publication | New generation remains authoritative; report degraded/restart-required if retirement fails. |
| Shutdown/cancellation | Close acquired candidate and active resources under bounded shutdown; no new requests. |

Never switch back after requests have used the new generation. That is another
migration transaction, not rollback. A teardown failure that leaves tasks/resources
uncontrolled blocks admission; don't report success and proceed.

Record generation IDs, affected contribution versions, config/plan fingerprints,
phase, and outcome in structured diagnostics and the journal where available.
Preserve the primary failure and include cleanup failures. These records explain
what happened; they are not instructions to replay initialization or execution.

## Why Python module reload is not the implementation

`importlib.reload()` updates a module dictionary but does not replace objects
already imported elsewhere, existing class instances, closures, thread callbacks,
or user variables. Deleted globals and extension-module state also complicate
replacement. Removing `sys.modules` entries is not resource cleanup.

Use session-owned instances and versioned graphs, not in-place module mutation.
Full source/package upgrades are restart-required initially: the plugin manager's
environment lock still prohibits updating an environment in use.
A development process may watch files and propose a restart, but source edits alone
do not prove reload compatibility.

A future isolated plugin host could start a fresh process for code generations,
but that requires serializable interfaces, dependency/environment isolation, and
ownership of all cross-process handles. It cannot directly supply arbitrary
prompt-toolkit objects or worker Python callables. It is a separate implementation
project, not a prerequisite for config/UI refresh.

## Helper-plan replacement (later)

The plugin proposal makes helpers restart-bound in v1. Relax that only with an
executor capability that can stage, validate, and atomically swap an immutable
plan while the worker is quiescent.

- Negotiate exact helper IDs, versions, signatures, aliases, backend limits, and
  plan fingerprint with the worker before updating model-visible documentation.
- Core `say`, `collapse`, display capture, archive machinery, and worker/host
  protocol bindings remain non-reloadable. Provider/helper protocol upgrades
  require a session restart.
- Refuse alias changes that overwrite unrelated user variables. Explicitly remove
  only bindings owned by the old plan; a coincidentally matching name is not proof
  of ownership.
- For stable registry proxies, each call resolves the active generation and checks
  its lease. Never silently redirect a call already in progress to a new handler.
- Ordinary Python references are not rewritten. A user may have retained
  `saved_search = helpers["plugin:search"]`, a bound method, or an imported object.
  Such retained references must either be generation-checked proxies that fail
  clearly when stale, or make replacement unsupported. Do not claim all old
  objects have been retired.
- Install session/cell scope cleanup consistently, update the helper prompt only
  after worker acknowledgment, and preserve core per-cell printer freshness.

If the worker cannot guarantee a consistent inverse operation after partial
binding installation, mark the worker unavailable and require explicit reset or
session restart. Do not continue with documentation advertising unavailable helpers.

Agent-defined functions remain ordinary session state. Redefining a function in
a later cell is already possible, but does not update old references or imply a
plugin/helper-plan reload. Promoting agent-authored code still needs user review.

## Worker reset is not reload

A new worker loses variables, imports, retained results, function definitions, and
worker-local archive access. Host records may remain, but they do not restore those
objects. External side effects, files, and network actions are not undone.

Any future reset command must show this loss and ask for explicit consent in an
interactive frontend; headless/RPC requires an explicit destructive option.
Define how existing context and archive references are retired or marked stale so
the model cannot mistake historical observations for currently available state.
Do not automatically resend previous cells, re-run imports, or reconstruct the
namespace from the journal. Startup/helper installation runs only its declared
initialization, not prior agent work.

After uncertain execution, reset/restart establishes a new runtime; it does not
prove what the old cell did. Keep that uncertainty visible for reconciliation.

## Delivery plan and acceptance tests

1. **Observability:** expose desired/active config and restart-pending values;
   add generation fingerprints and a read-only reload plan.
2. **Portable UI refresh:** implement detach/attach ownership and builtin fallback
   using the frontend port. Keep component code upgrades restart-required.
3. **Compatible instance replacement:** opt-in stageable plugins only, dependency
   closure, admission barrier, bounded cleanup, and structured outcomes.
4. **Helper negotiation:** only for capable executors and generation-checked
   bindings; keep unsafe references/protocol changes restart-required.
5. **Code isolation:** investigate only if live source upgrades have a compelling
   need. Do not weaken manager/environment locking for convenience.

Required tests:

- Invalid config candidates leave the old snapshot unchanged; session overrides
  persist and request/cell/epoch/restart application rules remain intact.
- Plans go stale after config, dependency, artifact, or generation changes;
  apply rejects them without invoking factories.
- An active cell, nested helper, dialog, pending recovery, queued action, or owned
  task blocks replacement; bounded admission never runs work against a mixed graph.
- Quiescence timeout leaves the old runtime usable. Shutdown wins over reload
  without deadlock or task leaks.
- Fault injection at every prepare/migrate/activate/publish/retire step checks
  resource cleanup, primary error retention, visible generation, and admission.
- Dependency order, exclusive resources, non-reloadable contributions, and
  untracked old references cause explicit refusal/restart requirements.
- Frontend refresh preserves composer text, isolates attachments, clears handles,
  cancels dialogs, and leaves usable builtin UI on extension failure.
- Helper plan acknowledgment matches model docs. Alias collisions and stale
  proxies fail clearly; partial installation never leaves an advertised-but-missing
  helper. Core bindings and archive semantics stay unchanged.
- Destructive worker reset loses namespace state explicitly and never replays
  cells, helper calls, or prior external side effects.
- JSON mode remains stable for the invocation; no watcher/UI messages contaminate
  stdout, and no unsupported interactive reload channel appears.
- Existing plugin, configuration, worker, recovery, and terminal tests keep passing.

Use fake resources and clocks for deterministic unit tests, plus actual worker and
subprocess tests for generation negotiation, cleanup, and interruption.
Do not report “rollback succeeded” merely because a dictionary pointer was restored.

## Decisions before implementation

- Which contribution kinds can genuinely stage resources without external effects?
- What exactly counts as quiescent, and which tasks are mandatory versus disposable?
- How are runtime generation IDs carried through existing origins and host calls
  without confusing config revisions or context epochs?
- Which executors can support reversible helper swaps and checked references?
- What is the public outcome for post-commit retirement failure?
- Does live code upgrade justify an isolated plugin host, or is explicit restart
  simpler and safer?

The default remains **restart required unless a contribution proves a narrower
safe operation**. Reload is an optimization with an explicit contract, not a
promise that arbitrary Python state can be replaced in place.
