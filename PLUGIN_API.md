# External plugin API (v1 draft)

This document describes the public plugin API currently implemented. API v1 is still a draft; the examples are an external-boundary exercise, not a compatibility freeze.

## Activation and registration

A separately installed distribution declares an entry point in `ipy_agent.plugins`, whose name matches its manifest ID:

```toml
[project.entry-points."ipy_agent.plugins"]
my-plugin = "my_plugin:plugin"
```

```python
from py_agent.plugins import Contributions, PluginManifest, Service, hookimpl

class MyPlugin:
    @hookimpl
    def py_agent_register(self) -> Contributions:
        return Contributions(
            PluginManifest("my-plugin"),
            services=(Service("executor", "alternate", AlternateExecutor),),
        )

plugin = MyPlugin()
```

Discovery lists entry-point metadata without importing plugin code. Only names passed to `PluginRuntime.load(enabled=(... ,))` are imported. Built-ins are also supplied explicitly. Installation, workspace files, notebook metadata, and model output never activate plugins. Plugin loading is trusted arbitrary Python execution, not a security boundary.

Registration hooks are synchronous, non-wrapper Pluggy implementations. They return immutable declarative `Contributions`; they must not create resources or execute user code. Duplicate IDs, unknown hooks, unsupported API versions, missing/ambiguous dependencies, dependency cycles, invalid configuration ownership, and contribution collisions fail registry construction. Manifest dependency ordering is deterministic and lexical among simultaneously ready plugins.

Public registration records are in `py_agent.plugins`; immutable data and protocols are in `py_agent.contracts`; scalar schema types are in `py_agent.configuration`. Examples import only those public modules. Namespaced configuration fields belong to the registering plugin. Factories for commands, transformations, and observers receive an immutable mapping of that plugin's configuration keys with the plugin-ID prefix removed. Defaults are used when a host's `ConfigStore` does not contain those fields. The host snapshots configuration per submission; transformation/command factories are invoked with that request's snapshot.

## Services and commands

A generic `Service(kind, id, factory)` is selected only by explicit `(kind, id)`; service selection never depends on import order. The coordinator selects `router`, `provider`, `interpreter`, and `executor`, and validates their public methods. The selected executor must declare `ExecutorCapabilities`; no local-executor fallback occurs.

Use `CommandContribution(name, factory, summary, usage)` for a slash command. Its factory receives the plugin's config mapping and returns an object with `execute(arguments: str)`. The result can be text or an awaitable producing text. External commands are exposed as `PluginRuntime.commands` and `Coordinator.external_commands`, and are dispatched by the coordinator after checking collisions with the configured core command registry. Existing `Service("command", name, factory)` contributions remain accepted as a compatibility bridge, but do not receive config injection. Queued commands run serially in FIFO order at the next agent cell boundary, before the next provider request, without waiting for the active turn to finish. They retain their enqueue-time request configuration snapshot; other configuration boundaries remain unchanged. A command failure is reported without retry; cancellation propagates, and cancellation of a running command leaves the session failed because command side effects may be uncertain.

The CLI enables external plugins only through explicit `--plugin ID` or a trusted explicitly selected JSON file with `plugins.enabled`. Embedders may supply a separately constructed runtime. Discovery and inspection alone never activate plugin code.

## Async transformation pipelines

`TransformContribution(kind, name, factory, before=(), after=())` registers an async stage for either `kind="context"` or `kind="model-request"`. Factories receive plugin config. The returned object must expose `async transform(value)` and return a fresh `ContextSnapshot` or `ModelRequest`, respectively. A stage must not change the context epoch or the model-request origin. The coordinator applies context stages before constructing the provider request, then applies model-request stages before calling the provider.

`before` and `after` name stages within the same pipeline. Unknown references, duplicate names, self-edges, and cycles fail startup. Topological ordering uses lexical stage names to break ties, independent of plugin registration/import order. Plugin dependencies do not silently imply pipeline order. Each returned immutable snapshot/request contains a host-authored `transform_trace` of qualified stage IDs (`plugin-id:stage-name`); a plugin cannot erase earlier provenance. A stage error fails the request. Cancellation propagates, late transformation results cannot launch a provider request after invalidation, and transformations are never automatically replayed.

Transformations are trusted code, not a sandbox. Keep them deterministic and side-effect free. There is no generic middleware continuation API.

## Executor wrappers

`ExecutorWrapperContribution(name, factory)` registers a decorator with a factory shaped as `factory(delegate) -> Executor`. Wrappers are not activated merely because their plugin is loaded: select them explicitly by qualified ID in caller-specified order with `Coordinator(..., executor_wrappers=("plugin-id:name",))`. The coordinator owns lifecycle calls on the outermost wrapper; each wrapper must forward lifecycle correctly. Wrappers must preserve cancellation and must not replay side-effecting `execute` calls. Capability declarations are part of the public executor contract; each resulting executor must declare `ExecutorCapabilities`.

Production context requires an executor exposing `async store_collapsed(text: str) -> int`;
startup rejects an incompatible context/executor combination. This host-control operation
must store the exact archive string in the live namespace as `collapsed[index]` and
return a distinct positive index only after complete storage. It must not execute Python
source. A transport failure or cancellation fails the session, since the persistent
namespace may be unavailable. Wrappers must forward this method when their delegate
supports it, as well as optional `store_outputs` archival; do not advertise unsupported
capabilities. The example audit wrapper demonstrates this forwarding.
`store_outputs(texts)` accepts 1–10 strings of up to 16000 characters each (including
observation formatting) and acknowledges distinct positive archive IDs. Local
transport splits batches to respect its byte limit even with JSON escapes.

The local `read_output(index, start=0, limit=4000)` helper emits a `display` event
with `text/plain` and `py_agent_output_read` metadata containing integer `index`,
`start`, `end`, and `total` (character offsets into retained text). Preserve that
metadata through wrappers. The coordinator validates bounded excerpts and passes
them separately from ordinary observations; the production context ages them into
original-ID/range references without creating another output archive. Normal
stdout from the same cell is not exempt from output limits.

## Output observers, failure policy, and backpressure

`ObserverContribution(name, factory, critical=True)` registers an async observer exposing `observe(OutputEvent)`. Events are frozen records with copied, read-only string data and their originating session/request/generation/execution identity. Sequence numbers increase monotonically within a coordinator. Current coordinator events cover execution stdout, stderr, and errors; rich worker display/update events are not yet connected to this publication path.

The coordinator awaits observers sequentially in deterministic plugin-dependency order, then observer-name order. It creates no detached per-event tasks or unbounded fan-out queue; a slow observer therefore backpressures the submitting operation. An observer may use a bounded queue and await its capacity, as `example-output-observer` does. Critical observer exceptions fail the operation; best-effort exceptions are counted and delivery continues. `asyncio.CancelledError` is never treated as an ordinary observer failure. Because execution may already have side effects when delivery fails or is cancelled, the coordinator fails that session rather than replaying execution.

`Coordinator.output_observers` exposes the instantiated observers for a host consumer. A bounded observer can intentionally stall execution until its queue is drained. Durable journaling, output spill/truncation, independent frontend transport queues, and worker output streaming are not implemented by this observer hook.

## Configuration, lifecycle, and limitations

`PluginRuntime.config` validates plugin schemas. An embedding host may create a `ConfigStore` from the runtime schema and pass it to the coordinator; namespaced defaults are available even without a store. Config changes remain the host's responsibility. Factories do not receive credentials or coordinator internals.

The coordinator owns the selected executor lifecycle. It does not generally start/close all plugin objects; observers and transformation stages should not acquire unmanaged resources. No hot-unloading, live reconfiguration transaction for plugins, service replacement, or general plugin lifecycle hook is provided. A plugin-set/executor change requires a new runtime/session or process restart.

Cancellation invalidates request identity before waiting for providers/executors. A late provider result cannot execute. Executor/command/observer side effects are not rolled back, and uncertain execution is never automatically replayed. Providers may only be retried by provider implementations under their own explicitly defined safe policy; coordinator execution is never transparently retried.

The separately installable examples are under `examples/plugin_command`, `examples/plugin_executor`, and `examples/plugin_observer`. Install the target distribution, then explicitly activate its entry-point name. They use only the published API, without private imports or monkeypatching. These APIs run with the current user's Python privileges; plugin loading and executor selection do not provide isolation or credential protection. Do not enable untrusted plugins.
