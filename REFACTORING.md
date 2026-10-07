# Readability and typing roadmap

## Baseline and first slice

The initial suite passed 592 tests. An unrestricted strict-mypy audit reported
463 errors across 23 modules in the default development environment (including
missing optional Jupyter imports). That was the original baseline; the current whole-repository mypy configuration
passes with the optional Jupyter dependencies installed.

The first slice makes configuration's generic mapping copies, scalar narrowing,
validator return contract, and registry attributes explicit. Numeric validation
retains exact-type checks (notably rejecting bool for int fields). Collapse parsing
now validates and collects literal strings together, instead of relying on type
narrowing across a separate generator. Immutable snapshot and transaction behavior
remain unchanged.

## Runnable strict gate

```sh
uv run mypy --config-file mypy-strict.toml
uv run pytest -q
```

`mypy-strict.toml` uses real `strict = true`, without disabled error codes, for
configuration, limits, collapse control, context export, and terminal Markdown.
Imported legacy modules remain available for inference but are not checked by
this gate (`follow_imports = "silent"`). This is explicitly **not** whole-project
strict typing. Add migrated modules to its file list; do not suppress their errors.

## Next cohesive slices

1. Type event and worker-message envelopes at serialization boundaries; validate
   external JSON before constructing internal types. Avoid replacing missing types
   with pervasive `Any` or unchecked casts.
2. Migrate contracts and plugin factories together, then configuration commands.
   Their call signatures are dependencies of most orchestration code.
3. Extract coordinator concerns behind those contracts: request lifecycle, context
   collapse, and journal updates. Preserve cancellation and transaction boundaries
   with existing tests before moving behavior.
4. Separate CLI parsing/configuration assembly from runtime startup and shutdown.
5. Check optional Jupyter adapters in an environment with the `jupyter` extra.
6. Once all modules join the strict gate, consolidate into `pyproject.toml` and
   remove the existing global error-code suppressions.

Prefer small behavior-preserving changes over a file-size-driven rewrite.


## Reliability and quality gates

Both model entrypoints use the same retry classifier; code execution never does.
Codex reads have an idle deadline even for custom HTTP transports.
`SQLiteJournalWorker` owns one SQLite connection in one thread, with bounded
admission and cancellation-safe acknowledgement. Execution/provider bookkeeping
settles with commits and is deduplicated under locks. Synchronous custom journals
remain supported; their calls retain the existing synchronous contract.

CI installs locked dependencies, runs offline tests with pytest-timeout, checks
Ruff and the strict core on Python 3.12–3.14, and separately checks Jupyter tests
and the full repository's non-strict types. Live provider verification remains
opt-in; offline success is not evidence of live provider availability.

## Refactor completion audit

### Implemented and inspected

- `Coordinator` remains the composition root/public compatibility facade.
  `SessionRuntime` holds shared dependencies; conversation, frontend, lifecycle,
  journal, and observation components reference that runtime, not the facade.
- `RequestRunner` owns submission orchestration; `ModelRequests` handles model
  retry/overflow recovery. `Operation` centralizes active identity and evidence.
- Shared model retry policy is used by coordinator/nested model calls. Executed
  cells are not retried.
- `SQLiteJournalWorker` serializes bounded, acknowledged I/O off the event loop;
  `settle` waits for admitted side effects before propagating cancellation.
- Worker transport limits/validation are centralized. CI defines offline tests,
  lint, strict migration types, and a Jupyter/full-type job.

### Fixes and verification in this follow-up

Updated the stale component-ownership test to assert runtime ownership, including
the runner/model components and per-session isolation. Applied safe Ruff fixes
in `src`/`tests` and moved CLI imports/type parameters into lint-compliant form.

A repeat full run exposed a genuine auth race: `codex_credentials` checked native
credential presence before taking the store lock, racing atomic refresh writes.
Moved that check under the existing lock without relaxing secure-read validation;
added a deterministic regression test requiring the presence read to be locked.

Python 3.13 local verification (optional ipykernel installed):
- Offline suite: **854 passed, 2 skipped**.
- `ruff check src tests`: passes.
- Strict migration mypy: passes (9 files).
- Whole-repository mypy: passes (53 files; this is not strict typing).
- Skips: optional matplotlib absent; no-ipykernel fallback test intentionally
  skips when the real Kernel base is available.
No live provider calls or full Python-version CI matrix were run here.

### Remaining work / recommended completion sequence

1. Run the checked-in CI matrix (3.12/3.13/3.14, Jupyter extra); optionally install
   matplotlib to exercise the skipped image integration. Do opt-in live provider
   smoke tests separately; offline success does not verify real credentials,
   streaming/retry behavior, or provider availability.
2. Review and stage the new modules/tests, CI workflow, and lockfile explicitly.
   Many refactor files are currently untracked; a tracked-only commit would
   omit required code. Split architecture, reliability, and mechanical lint
   changes into reviewable commits where practical.
3. Treat the extraction as functional, not a finished simplification:
   `RequestRunner.submit` is still nearly 900 lines and `Coordinator` retains a
   large forwarding surface. Extract coherent request phases incrementally,
   retaining cancellation/uncertain-execution and queue tests; avoid moving code
   merely to meet a line-count target.
4. Narrow shared runtime dependencies with component-specific protocols/services.
   `SessionRuntime` is still broadly coupled and wired after construction;
   journal dispatch still uses dynamic method names/`Any`. Add these modules to
   the strict migration gate as their interfaces become explicit.
5. Preserve custom synchronous journal compatibility, while documenting that it
   can block the event loop. Exercise slow/failing journal and shutdown paths as
   interfaces change; do not introduce execution replay as a recovery mechanism.

The default persistence policy is unchanged: full session journaling requires
`--journal PATH`. This audit is a durable handoff, not a saved chat transcript.

### Full local CI matrix verification

Ran the checked-in workflow commands in separate fresh uv environments with
locked dependencies (not GitHub-hosted Actions):
- Python 3.12 offline: 855 passed, 1 skipped; Ruff and strict mypy pass.
- Python 3.13 offline: 855 passed, 1 skipped; Ruff and strict mypy pass.
- Python 3.14 offline: 855 passed, 1 skipped; Ruff and strict mypy pass.
- Python 3.13 with Jupyter extra: 854 passed, 2 skipped; full mypy passes.

All install/sync/check steps exited successfully. Live provider tests were
excluded as in CI. This completes the local Python-version matrix item above;
GitHub-hosted execution and optional matplotlib/live checks remain separate.

### Closeout decision

This extraction/reliability phase is complete within the verified offline
scope. Reviewed the composition/facade boundaries, lifecycle evidence handling,
retry policy, journal admission/cancellation, protocol validation, and packaging
of new modules/tests. No additional blocking issue was identified in that review.
The final snapshot includes all refactor source, tests, CI, dependency/lock,
and documentation changes. Unrelated pre-existing deletions of `_repro.py`,
`c5.html`, and `c6.html` are deliberately excluded.

Further request-phase decomposition and narrower runtime interfaces remain
explicit follow-up tasks; full strict typing and live-provider verification are
not claimed by this closeout.

## Implemented follow-up: request phases and strict interfaces

- Extracted direct user-cell execution into `DirectExecution`, independent of
  queue/outer-operation cleanup. Nested llm handler construction is injected;
  it retains the execution origin and existing host runtime checks.
- Added `DirectRuntime` and `ModelRuntime` capability protocols. Model retries
  no longer type against the complete SessionRuntime. The runner remains the
  orchestration owner; the agent loop has not been mechanically split.
- Centralized cancellation/error settlement in `_fail_submission`, preserving
  reserved steering outcomes, best-effort context abandonment, and the rule
  that only acknowledged stopped execution permits reuse after cancellation.
- Expanded strict mypy from 9 to 21 files: all coordinator components, both new
  interface/phase modules, and the journal worker. Dynamic synchronous journal
  compatibility remains supported; this is not a claim of fully typed dispatch.
- Strengthened per-session ownership assertions for the direct phase.

Full locked local matrix rerun: 3.12/3.13/3.14 each 855 passed, 1 skipped;
3.13 + Jupyter 854 passed, 2 skipped. Lint and the expanded strict gate pass in
every job; whole-repository mypy also passes in the Jupyter job.
Remaining deeper work: decompose the agent loop around explicit turn state,
narrow other components' runtime dependencies, and replace dynamic journal
dispatch with a typed compatibility adapter. Those are not implemented here.
