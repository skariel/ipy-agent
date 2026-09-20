# Implementation status

## Current design — in-process notes and stable context epochs

Latest user decisions supersede the historical sections below:

- `memories = []` is initialized in the worker. No memory.md, snapshots, automatic
  note-content injection, variable persistence or resume. The obsolete memory
  module and its file-policy tests were removed. Ordinary source/output is still
  journaled; this does not restore Python state.
- A context's system prompt is immutable while its conversation grows by appending.
  At 95% of configured token capacity, ALL dispatched conversation is cleared.
  Only pending, not-yet-dispatched user input carries over. No rolling tail,
  maintenance turns, replay or automatically retained unfinished-task instructions.
  `tail_groups` was removed. Explicit `/reset` remains a manual exception.
- Current-epoch reported input tokens control eviction. Late/orphan usage cannot
  influence it. Before measurement, usage is unknown: the byte estimate is audit
  information only, never an eviction/rejection gate. Abrupt input growth between
  measurements can still exceed a provider's real window.
- At each new context, freeze `This context started with X memories` and a bounded
  namespace inventory (public names/types, not values/reprs). Count/inventory are
  reported by the worker after cells but do NOT change the existing prompt.
  Introspection avoids user repr/len/metaclass hooks, scans at most 512 bindings,
  and validates a <=2000-byte inventory. Missing/rebound state is explicitly unknown.
- Python variables, memories, functions and list identity survive context clearing.
  The model inspects memories/history when needed; a new kernel starts fresh.
- UI input is `In [n]:`, counting successful user submissions. Generated source is
  hidden unless `/trace` is enabled (`Python [n]:`). Results remain visible as
  `Out[n]:`/stdout/stderr; result numbers are worker-cell IDs, not user-input IDs.
- Status displays `ctx(last) 30%/272k` using current-context reported input tokens
  and configured capacity. `--context-window-tokens` or trusted config controls
  capacity; fallback is the `input_tokens` budget, now default **272000**, not the
  obsolete 24000 cap. Default eviction starts at 258400 reported input tokens.
  This is not model discovery. Missing measurements show `?`; `out(last)` retains output counters.
- Removed Codex's local output-token acceptance cap: completed code is not rejected
  because reasoning/output counters exceed the generic output budget. Missing usage
  stays unknown; failed/incomplete responses and invalid protocol still cannot run.
  API-key output caps are also omitted by default, with an explicit override only.
  Native SDK/provider defaults can still apply.
- Removed application work quotas: request/cell totals, execution deadlines, worker
  resource ceilings, active-capture ceiling, file/spool size, message/frame size,
  provider response/chunk ceilings, history broker counts and journal storage cap.
  Workers inherit actual OS limits. Actual resource exhaustion is still possible.
  Quota configuration fields and their obsolete tests were removed.
- Worker output over 8,000 characters still becomes a private hash-deduplicated
  file notice. Large `say` strings/structured replies use the same mechanism,
  preserving final-answer staging. Files have no application size cap; real disk/
  OS failures are explicitly reported. Observations reach the model losslessly:
  the duplicate serialized-byte clipping layer is gone, including for Unicode.
- Terminal backlog events and trace text are not silently dropped/clipped. Offline
  journal readers/export accept complete records beyond the former 8 MiB ceiling.
- Sandbox confinement, correlation/schema validation, incomplete-response rejection,
  cancellation/stale-result revocation, no replay and actual-storage-failure handling
  remain. Connection/startup/cleanup waits, protected-control-file validation and
  abbreviated namespace inventories remain—not normal-work allowances.
  Historical snapshot records stay readable without any memory runtime.

Verified: **701 passed, 2 skipped**; `uv lock --check` and `uv build` passed.
New regressions cover 121 requests, 120 history calls in a cell, >64 MiB files,
large Unicode frames, >2 MiB responses, >16,384 stream events, large journal/export
records, untruncated 8k-character model observations and large staged `say` replies.
Coverage includes stable prompts, full epoch eviction, pending steering, frozen
memory counts/inventories, no note injection, fresh kernels, safe bounded metadata,
usage isolation from stale responses, hidden source, numbered prompts and PTY exit.
Skipped real sandbox tests remain unverified, not confinement evidence. Fresh live
provider behavior and target-host sandbox/descendant cleanup need separate checks.

## Historical design — before in-process memories

The following implementation snapshot is superseded by the current design above.

### Previous design — checkpoint machinery removed

The runtime is a simple **user → model → IPython → results → model** loop.
The original [IPYTHON_AGENT_PLAN.md](IPYTHON_AGENT_PLAN.md) is preserved unchanged
as design history; the user's later decisions below intentionally supersede its
forced memory-maintenance/checkpoint design.

- Raw Python responses, no provider execution tools; Codex explicitly requests
  medium reasoning effort with read-only pi authentication. After validating a
  successful complete response, its first complete assistant message supplies
  ONE cell, regardless of commentary/final-answer phase. Later messages generated
  without execution feedback are discarded; the selected phase is preserved in
  history. Hidden reasoning, tool calls and unsuccessful/partial streams never run.
  This supersedes the historical final-message-only selection described below.
- `say()`/`wait()` provide user interaction. Python variables persist between
  cells; stdout, stderr, displayed values and errors return to the model.
- `memory.md` is optional agent-authored notes. The model decides when and what
  to write through ordinary Python. In-place edits are supported; no mandatory
  `os.replace()` publication or host-requested save/repair turn remains.
- No checkpoint generation/execution path, checkpoint budget flags, speculative
  cell-source headroom gate, or synthetic retained-tail benchmark remains.
- Context is shortened only before a request when its configured estimate does
  not fit, or after `/reset`. Existing last-valid notes, recent complete groups,
  unfinished-task instructions and queued input are retained. This neither runs
  Python nor restarts the kernel. Original evidence remains in the journal.
- Normal UI: `You:` composer, visible `In [n]:` Python, `Out [n]:` displays,
  labelled stdout/stderr and a thinking spinner. `/trace` adds audit detail;
  it is not needed to see code/results. The status bar shows last reported input/
  output token counts, not the byte-based context estimate; missing counters are
  `?`. `/usage` still exposes the estimate; context budgeting itself is unchanged.
- Writes are allowed in the project workspace and shared `/tmp`, per the latest
  user request. Host storage, launcher sockets and runtime/import roots remain
  protected even under `/tmp`; default TMPDIR still uses workspace scratch.
  Codex auth must be outside both writable roots; explicit config must be outside
  them or under the protected host root. Preflight now verifies `/tmp` read/write.
- Combined stdout/stderr/displays over 8,000 Unicode characters spill to a private
  `/tmp/py-output-<sha256>.txt` file. Completed identical contents reuse one canonical
  file, after bounded content/metadata verification without following symlinks.
  Provisional names expire at final publication; follow the last reported path.
  Existing unsafe/corrupt destinations are never overwritten; failed publication
  or cleanup may leave a unique capture, explicitly noted when cleanup fails.
  The worker sends counts and a path, not the full text;
  ordinary large output no longer trips the supervisor's frame quota. Small text
  is buffered until a control boundary or cell completion. Prefixes already shown
  are included in the spill; late subprocess output keeps its original cell ID.
  Counts become final after both cell completion and pipe EOF. Files are mutable
  worker artifacts, not host evidence, capped at 64 MiB (or inherited FSIZE).
  Disk failures explicitly mark incomplete artifacts while draining continues.
  Files persist until removed; no aggregate disk quota or automatic cleanup.
  History contains notices, not spilled originals; the model reads smaller file
  chunks using Python. `say`/broker/control-frame quotas remain unchanged.
- Cell wall-clock deadline defaults to 300 seconds (override `--cell-seconds`).
  It covers the entire cell, not each subprocess separately. Timeout still kills
  the kernel without replay; the diagnostic now names the limit and suppresses
  the redundant EOF/crash message caused by intentional termination.
- Sandbox, cancellation, output/resource bounds, durable evidence and readonly
  inspection remain. Session export produces inert private file:// HTML with
  complete events and exact per-request messages. Offline accounting still
  understands checkpoint fields in **historical journals only**.

Remaining limits: conservative byte-based context estimates rather than a real
provider tokenizer; local-only Codex output acceptance cap; no aggregate cgroup/
filesystem quotas; no kernel resume or source replay; nested sandbox verification
blocked here; full target-host cleanup/network and live model reliability remain
unproven. Test skips are not evidence of confinement. Readable private data can
reach the model through results even with worker networking denied; journals and
exports must also remain private.

Current checks: **664 passed, 2 skipped**. `uv lock --check` and `uv build` passed
before the latest toolbar-only token-label change.
A fixed trusted worker/supervisor
regression emits 2 million characters, returns a notice, then reads a 4,000-character
chunk in the next turn, verifying live variables survive and no source is replayed.
The transport-flood failure test now uses control traffic, not ordinary print output.
Before content-addressed naming, the spooling suite under inherited worker ceilings
passed **620 tests, 3 skipped** (the additional skip was the host-thread prerequisite).
Content-addressed tests isolate artifact storage so cleanup cannot delete a real
user's preexisting file with identical content. Supervisor boundary regressions
exercise 7,999, 8,000 and 8,001 output characters, both ASCII and four-byte Unicode;
execution continues in the same kernel across the boundary. Opt-in srt probes still skip here on
Unix-socket EPERM; the new `/tmp` permission needs target-host verification.
Coverage includes ordinary in-place notes edits, stable-read race detection,
resets without additional model/worker turns, unfinished-task retention, visible
Python/results, thinking animation and real-PTY exit/terminal restoration.
The obsolete atomic-publication inode ledger and duplicated CLI budget definitions
were removed; normal startup is brief, with detailed policy retained for the
sandbox diagnostic command. Historical journal readers remain compatible.
- Run `11c52816c0d442cc8e804123c3e5493d` exposed pipe-fragment rendering and
  model-observation overhead: 206 stdout frames contained 13722 bytes. UI now
  renders one continuous stream with incremental escape sanitization before its
  bounded queue. The observation packer coalesces first, preserving typed raw
  head/tail excerpts only when needed. Existing journal records remain unchanged;
  current oversized worker output is now spooled before reaching the journal.
- Offline replay of that run renders one stdout heading with exact concatenated
  text, and packs the 206 frames into one 8000-byte observation retaining source
  and test listings. Parser replay selects the first complete cell in responses
  10, 24 and 238, dropping 0, 3 and 1 unobserved follow-ups respectively. No stored
  model code was executed. A real-adapter/mock-transport plus trusted-worker test
  verifies actual execution feedback reaches the next request, rather than acting
  on the later invented failure. Additional tests cover phase replay, unsuccessful
  response rejection and split terminal control sequences.
- Run `8779d7378e794e54b688d4f81a51a1ce` finished pytest after 55.4 seconds,
  started `uv build` in the same cell, then hit the old 60-second wall deadline.
  Its 17 test failures were 16 host-launcher unit tests writing read-only `/tmp`
  and one host-thread stress test unable to create 80 threads under inherited
  resource limits. Launcher unit tests now use hermetic local scratch; the stress
  test explicitly skips when it cannot establish its host-thread prerequisite.
  Production sandboxing is not substituted with those test mocks. The ordinary
  inherited-NPROC and worker-capture failure tests still run under resource caps.
- Config permission checks normalize `..` consistently with config loading.
  A nonwritable parent of a valid private host root no longer breaks preflight:
  it uses the protected sentinel for boundary checks in that case. Other failures,
  including exhausted storage, still propagate.
These tests do not prove fresh live-model reliability or full sandbox confinement.

## Historical record — before the latest simplification

The following preserves useful investigations and prior decisions. References to
forced checkpoint turns, atomic-only memory edits, source headroom, and synthetic
retention comparisons describe **removed behavior**, not the current runtime.

### Previously implemented and deterministically tested

- Pinned uv project, `py` console entry point, `py_agent` package, lockfile.
- Linux srt launcher with bounded preflight, explicit network policy, sanitized
  environment, protected host/runtime/import roots, hardlink/symlink runtime
  verification and no weaker/unsandboxed production fallback.
- Versioned bounded strictly validated IPC, descriptor-separated raw output,
  persistent IPython, native/subprocess capture, rendered-once displays, history,
  format/runtime errors, partial effects, duplicate dispatch rejection, yields.
- litelm 0.5.2 adapter and fake provider; buffered streaming, text/reasoning
  separation, finish/refusal/tool checks, raw/normalized usage, cancellations.
- User-authorized Codex subscription integration: Python/httpx SSE adapter,
  read-only private pi OAuth credentials reloaded per request, fixed endpoint,
  complete-response validation and exact nonsecret request-body journaling.
  No pi agent-loop dependency. OAuth refresh remains pi-owned to avoid shared
  refresh-token races. Codex output-token limits are local acceptance limits,
  not server-side caps; startup, request records and docs explicitly say so.
- Serialized single-agent supervisor; queued/included/accepted user IDs; stale
  generation rejection (even when cancellation is swallowed), staged finals,
  deadlines/output/request limits and no replay. Fatal logging failure initiates
  kernel cleanup independently of further logging.
- Private append-only SQLite history, bounded scoped search/paging, retrieval
  provenance, exact requests/source/events, durable one-record epoch commits.
- Agent-owned atomic memory, full frozen snapshots, cell-boundary draft versions,
  same-agent checkpoint turns, bounded retries, chronological deep resets,
  pending input retention and continued use of live Python/history.
- Prompt-toolkit UI: responsive composition, paste/draft preservation, multiline,
  Emacs/Vi, Ctrl-R/history, narrow/Unicode/no-color, escape sanitization, commands,
  explicit active cancellation, private input history, non-TTY error.
- Offline trace accounting and synthetic retained-tail comparisons.

The test-local unsandboxed worker fixture accepts fixed trusted test snippets
only. It is not a supported backend or evidence of OS isolation.

### Earlier external-gate record

1. Installed srt package 0.0.71 cannot express the requested open network through
   its CLI schema. Default startup fails closed. Proxy mode is available only by
   explicit user selection and is not described as equivalent open networking.
   Direct bubblewrap could be investigated as a separate Linux backend; it has
   not been substituted for the agreed srt launcher.
2. This development environment itself runs under srt. The complete nested
   launcher fails creating a Unix socket (`listen EPERM`). Actual filesystem,
   seccomp, stdin/stdout transport, HTTP(S)/TCP/DNS/UDP/localhost behavior and
   detached-descendant cleanup require rerunning on an appropriate host.
3. The user subsequently selected Codex and authorized reuse of pi credentials.
   Private credential validation passed without printing tokens or making a
   network request. No live model calls were made. A Codex live smoke test and a
   second-provider comparison remain mandatory before portability claims.
   Native metadata discarded by litelm cannot be recovered by that adapter.

### Earlier unfinished implementation gates / limitations

- Aggregate writable-filesystem disk quota and aggregate CPU/memory/process
  enforcement. Existing hard limits are per process/file, not tree-wide;
  inherited UID-scoped process limits are preserved, not lowered. Namespace cleanup is implemented but not measured on this host.
- Execution interruption terminates the kernel; interrupt-and-continue with a new
  kernel epoch and restart/resume reconstruction is not implemented. Current
  CLI sessions are fresh runs; history survives but source is never replayed.
- Native writes from unmanaged Python background threads can cross cell capture
  boundaries. Such threads are prohibited by the runtime contract but arbitrary
  code cannot be made to honor that contract. Subprocess origin capture is tested.
- Large originals use SQLite rather than separate immutable artifact files;
  logical storage quotas do not bound physical SQLite overhead.
- Real task-quality/cost/latency evaluation, actual cache behavior, and full
  target-host terminal/sandbox validation remain outstanding.

**A–C mechanisms are implemented; their complete first-release definition of
done is not yet satisfied. Do not label the project production-ready.**

### Earlier deliberately deferred scope

- D: recursive agents, child scheduler and shared tree budgets. No `agent` API is
  advertised or faked in the root namespace.
- JSON mode, richer displays, package conveniences, resumable child waits,
  detach/attach, sockets and daemon operation.
- OAuth login/refresh owned by py, other subscription providers, and pi SDK/
  agent-loop integration. Only read-only existing Codex OAuth reuse is implemented.
- Cost prices without a versioned source. No inferred cache economics optimum.

### Historical validation and investigations

- CPython 3.13.11; locked dependencies installed in copy mode.
- Full deterministic suite: **488 passed, 2 skipped**. The prior 266-test suite
  also passed with 80 unrelated same-UID threads active. This reproduced the reported EOF/exit-70
  cascade before the fix: Linux `RLIMIT_NPROC=64` prevented capture threads from
  starting. Worker now preserves inherited UID limits, reports bounded fatal
  diagnostics, and tests tolerate the child-exit/killpg teardown race.
- Added short, private host-only srt bridge directories and explicit in-sandbox
  worker temp overrides. Session scratch paths produced 113-byte Unix-socket
  names, exceeding Linux's 107-byte limit. Regression tests cover long paths,
  unchanged worker write roots, host-directory protection and failure cleanup.
- Opt-in sandbox suite: **27 passed, 2 skipped**; both real probes blocked by
  nested srt Unix-socket `EPERM`, not counted as confinement passes.
- `uv build`: source distribution and wheel built successfully; source packaging
  explicitly excludes ambient harness configuration, caches and virtualenvs.
- `uv lock --check`: passed. A provider-only live format diagnostic was attempted
  after the user's first run failed, but the outer proxy rejected CONNECT with
  403. No model response was obtained or executed. The user-run journal showed
  the old adapter rejected HTTP 200 solely for a non-SSE content-type. The adapter
  now validates actual SSE/native JSON payloads, exposes bounded/redacted JSON
  errors, and stops blind format-error retries. Terminal queue receipts now
  deduplicate by user ID; retry exhaustion preserves the underlying error.
- At the user's request, exit keys now supersede the original idle/active Ctrl-C
  and Ctrl-D behavior: empty idle Ctrl-C exits; active double Ctrl-C exits; empty
  Ctrl-D exits with cancellation; /quit + Enter works in multiline mode. SIGTERM
  cancels the foreground task through cleanup. Six real-PTY tests verify exit,
  terminal-mode restoration and cleanup, including a stuck interrupt fixture.
- User live run `14948bd9312544f2869e97f72fe94436` exposed Codex's sparse terminal
  variant: complete messages arrive in `response.output_item.done`, followed by
  `response.completed` with `output: []`. The adapter now validates correlated,
  complete item events and releases source only after response-level success.
  A sanitized recorded trace is a regression fixture. Offline replay of all
  three original responses passes adapter validation; the latter two contain
  syntactically valid Python. No recorded model source was executed during replay.
  Completion rejection messages now expose their final reason in the UI.
- Follow-up live run `5f2bac7c4dc545feba27d7b193a4b5c4` included commentary before
  a valid final-answer cell. Commentary is now retained but never executed; exactly
  one completed final-answer (or legacy unphased) message supplies source after
  response-level success. Assistant history preserves final-answer phase.
  Offline parser replay of events 169, 172 and 175 accepts exactly their final
  cells, ignoring 1, 3 and 5 commentary messages respectively; nothing was executed.
  Added mixed-phase regression tests. Rejected completions receive corrective
  model feedback and visible retry notices, with existing retry/request bounds.
- Runtime prompt now explicitly explains autonomous non-final iteration, returned
  stdout/stderr/displays/status, print versus say, completion versus progress, and
  short rationale in Python comments rather than separate commentary actions.
  A trusted fixed-cell integration test verifies stdout/stderr/display feedback,
  automatic continuation, and namespace persistence. This tests loop mechanics,
  not model task-completion quality. Terminal status now puts state before model.
- Live run `1d807f3d0bcc4476948ae185b02b7865` demonstrates that Codex raw-cell
  protocol reliability is STILL UNRESOLVED. Request instructions were transmitted,
  echoed and counted; execution observations reached subsequent requests. Yet the
  model sent inspection code as commentary, then claimed missing filesystem
  results in its final cell before any inspection ran. No sandbox failure occurred.
  Do not treat parser replay or deterministic test counts as model-quality proof.
  Comparison with installed pi-ai 0.85.1 found no missing system-prompt mapping;
  both Codex adapters use top-level instructions. Recorded reasoning defaults to
  medium. Native execution tools are an alternative, not implemented or authorized
  as a replacement for the raw-cell design. Live Codex evaluation here is blocked
  by the development sandbox's domain allowlist; nested srt remains unavailable.
- Fixed a separate observed format bug: IPython suffix-help transformation used
  to convert question prose such as `Hi! How can I help?` into a successful help
  lookup. The worker now requires the full suffix-help expression to match the
  pinned IPython grammar, preserving explicit `len?`, attribute/index inspection,
  magic help and prefix help. Regression tests verify prose errors occur before
  side effects and return to the model for correction. This fixes misleading
  execution status, not the outstanding Codex protocol-compliance issue.
- Per user direction, retain raw Python/no provider tools. Codex requests now
  explicitly set `reasoning.effort: medium` (prior live responses already reported
  medium as the backend default). The system prompt begins “You speak only Python.
  Always answer with pure Python code and nothing else,” permits plans/notes in
  Python comments, gives a `say(...)` example, and explains that execution happens
  after generation, not during commentary. A wire-level test with the real Context
  verifies that this prompt is sent as instructions, observations as user input,
  and only prior assistant code as output_text. No live reliability claim is made.
- User run `66919b022cb04b1a8d2b75893b37347a` successfully executed 12 review/
  checkpoint cells, then failed dispatch headroom immediately after a checkpoint.
  Commit validation previously checked request fit, not working-set fit. It kept
  a duplicate checkpoint source/observation group with the new memory snapshot.
  Retention now drops whole optional groups until normal-cell working headroom
  remains; pending input and agent memory are never silently trimmed. Generation
  checks actual source bytes and sends oversized proposals back for bounded
  smaller-cell retries, without dispatch. The pre-dispatch guard remains.
  Offline reconstruction of that exact checkpoint: 706 source bytes available
  before, 3299 after evicting one optional group; rejected source was 1139 bytes.
  No recorded model source was executed. Added budget/retention/retry regressions
  including a persistent-worker checkpoint-to-next-cell integration case.
- Added `scripts/export_session.py`: read-only transactional journal snapshot to
  private, exclusive-created HTML with full events and exact per-request context.
  All journal text is escaped; no JavaScript, network, model calls or source
  execution. Output prints a file:// URI; existing files/symlinks are refused.
  Thirteen export tests cover injection, preserved values, permissions, readonly
  journal behavior and error cleanup. A snapshot of the above run was exported to
  `/tmp/py-session-66919b022cb04b1a8d2b75893b37347a.html`; the protected host session
  directory is read-only in this implementation sandbox. The exporter can write
  beside the journal when run by the user outside this sandbox.

## Current validation commands

```sh
uv sync --locked
uv run --locked pytest -q
PY_AGENT_SANDBOX_TESTS=1 uv run --locked pytest -q -rs tests/test_sandbox.py
uv run --locked python scripts/replay_trace.py ~/.py/sessions/RUN/journal.sqlite
```

The README contains launch instructions and effective policy/privacy limitations.
The real sandbox tests skip with an explicit reason when prerequisites/isolation
are unavailable. Count those skips as blocked, never passed.
