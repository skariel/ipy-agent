# Incremental simplification

## Implemented

- `output_safety.py` owns pure text/JSON redaction and truncated-secret handling,
  shared by the local executor and worker.
- Preview values, labels, and separators are redacted before clipping.
- Worker archives redact before storage and strip possible password prefixes at
  truncated capture boundaries. Archive reads redact whole values before slicing,
  including values stored before a password was learned.
- Read offsets and totals now describe the redacted archive. This deliberately
  changes offsets when redaction changes text length.
- `worker_protocol.py` owns common wire limits and protocol version. Parent and
  worker still independently validate incoming messages.

## Retained deliberately

Jupyter support is an optional dependency with CLI session commands and dedicated
tests, not dead code. Plugin/service/configuration support is exposed through CLI
configuration and tested. Removing either would remove supported functionality.

## Coordinator structure

- `coordinator.py` retains service selection, configuration, and the serialized
  submission/agent orchestration loop.
- `coordinator_conversation.py` owns conversation/overlay/epoch and history
  redaction state, context export, and history commands.
- `coordinator_frontend.py` owns event sequence and best-effort observer failure
  counts, query routing, frontend output dispatch, and progress publication.
- `coordinator_lifecycle.py` owns state transitions, locks, queue, active operation,
  cancellation, executor shutdown, and journal lifecycle flags.
- `coordinator_journal.py` owns fail-closed synchronous journal recording,
  provider usage accounting, and cache summary state.
- `coordinator_observations.py` renders bounded model observations, including
  archive-read provenance. Its tests can exercise it without constructing a
  coordinator or bypassing initialization with `__new__`.
- `coordinator_support.py` contains shared state/queue types, constants, and pure
  observation helpers; components import other dependencies directly.

Components are composed, not inherited. Each holds a typed reference to its
coordinator for orchestration services. Cross-owner mutable state access names the
owner explicitly. Compatibility methods and properties on `Coordinator` preserve
existing callers and test hooks without duplicating state. Epoch configuration
latches are set after service/config selection, as before.

The submission loop remains in the coordinator intentionally: splitting its
interleaved provider/executor/context/journal transactions would need a separate
behavior-changing design. Existing tests cover cancellation, input ownership,
queues, context collapse/export, journal failures, and optional executor/plugin
routing; new tests cover owner isolation, compatibility writes, and dispatch.

Password masking is best-effort protection against accidental disclosure, not a
sandbox. Executed code can access credentials and deliberately transform them.
