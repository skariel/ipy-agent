// Single-file transition harness: specification, embedded CPython, runtime and CLI E2E tests.
pub const SPEC: &str = r####"
# Rust Python harness — draft specification
Status: incremental implementation with real CLI/PTY E2E tests. Coverage is
partial; unchecked families are not accepted as complete.

## 1. Fixed requirements
- One handwritten Rust source file contains spec, tests, and embedded Python.
  A minimal Cargo manifest/lockfile and minimal suitable crates are allowed.
- No plugins, tool schemas, tool calls, server, Jupyter, or IPython dependency.
- A single local Rust program owns terminal UI, scheduling, providers, history,
  model context, configuration, usage accounting, and session persistence.
- A persistent vanilla CPython child executes ordinary Python. Its private
  local IPC to Rust is an implementation detail, not a client/server product.
- Execution is unrestricted as the current user. Process separation manages
  lifecycle, not security. No permission broker or sandbox.
- Python exposes a thin `agent` namespace, plus `H`. Helpers use Rust when
  appropriate; ordinary Python computation stays in Python.
- No automatic expression display, magic syntax, `!` escapes inside cells,
  injected global functions, notebook display hooks, or top-level await.
- Ctrl-C interrupts active work. This intentionally differs from Pi.
- All session records live in JSONL under global ~/.py.
- Resume restores H and context, but starts a fresh Python namespace.
  Never deserialize old variables or execute old cells to reconstruct state.
  Explicit core Python initialization (section 30) runs frozen startup source,
  not historical cells; failed/unknown startup requires explicit retry.
- Resume adds a model-visible notice: "Session loaded into fresh Python.
  Previous variables are undefined; H and context are restored."
  The model decides what to reconstruct.
- Much smaller than the existing implementation. Prefer direct functions and
  enums, not a service/plugin framework. No architecture layers for their own sake.

## 2. Migration and reference baseline
Keep the existing Python harness unchanged until the replacement meets the
approved tests. Work only in transition/. Do spec -> failing behavioral tests ->
implementation -> passing tests. No claim that draft-validation tests prove
the future harness works.

Adopt Pi/Pig behavior for login, model listing/resolution, provider invocation,
and basic terminal workflow, except explicitly listed differences.
Inspected Pi checkout: 4ba3e5be229a570187d8efbef5c14c0d5ce40dcc.
Inspected Pig source tree: f880abcd8c1a105c1c01523bdfc7a8d7f2ccf0dd.
Pig installed executable has no VCS revision in Go build metadata; its source
version is not proven equal to the inspected upstream tree.
Pi references: coding-agent README, core/auth-storage.ts, model-registry.ts,
agent-session.ts, interactive-mode.ts, docs/keybindings.md, docs/providers.md.
Pig references: agent/queue.go, cmd/pig, ai/auth and model code.
Pin behavior to the agreed reference versions, not moving upstream "latest".
No copying their tools, extensions, skills implementation, agent loop, or automatic compaction.
The harness's ordinary-file durable entries are defined independently in section 30.
Review licenses/attribution before copying code or catalog data.
Provider priorities and exact compatibility surface remain decisions.

Current harness findings: persistent IPython worker; automatic expression,
stdout/stderr and display inclusion; injected say/preview/llm/collapse;
plugin coordinator; partially redacted SQLite journal. These are not the new
architecture. Existing completion covers explicit-Tab commands and paths.

## 3. Durable history is not model context (frozen)
H is append-only, complete and immutable. Context is an ordered selected view.
Saving a record does not itself select its payload for the model.
Model-generated source is saved in H.code and retained as assistant source in
context. Never automatically edit, truncate or evict that source.
Automatic context housekeeping may drop only old output payloads, retaining
their H references/counts. It may not discard user messages, code or summaries.
Explicit collapse can replace any selected non-system history, including code,
user messages, outputs and earlier summaries; originals remain in H ranges.

Ordinary user prompts and visible direct interactions enter as user messages,
subject to the configured user-message cap, with full originals in H.user.
After model-executed Python, automatically include only status, refs and counts,
not stdout/stderr payloads. Syntax/runtime diagnostics obey the same rule.
The agent's only general way to insert new payloads is explicit read_text or
read_raw; printing, say, H retrieval, or a helper result never does so.
Exceptions are the agreed source/user-message routing, collapse replacements,
system control notices and usage metadata, not alternative payload channels.
An explicitly requested wakeup may select its bounded reason and task status,
counts and H references as control metadata (section 29), never task output.
Normal UI rendering is independent of what the model sees.

## 4. History and storage contract (proposed)
Canonical session path: ~/.py/sessions/<session-id>.jsonl.
Global config, auth, and Python environment also reside under ~/.py; no project
environment management in v1. Config format/file names remain a decision.
Each JSONL event has schema_version, session_id, seq, kind, timestamp, and
payload. Sequence is monotonic. Stable IDs never change after resume/collapse.
Rust is the only journal writer. Commit intent before dispatching code, shell
commands, or provider requests; commit completion separately. Incomplete intent
after a crash means unknown outcome, not permission to retry execution.
Context changes and queue transitions are journaled, so resume reproduces the
committed context view and H exactly. No Python heap checkpointing.

H exposes indexed, read-only, lazy collections:
  H.code[i]    source, author, execution_id, worker_generation
  H.user[i]    submitted user messages, including direct/hidden submissions
  H.stdout[i] full stream bytes and execution/operation reference
  H.stderr[i] full stream bytes and execution/operation reference
  H.stdin[i]  normal input replies and prompt linkage
Also H.events, H.raw, H.say, H.requests, H.responses, H.usage as indexed views.
Proposal: stdout/stderr are one logical record per operation per stream,
persisted as ordered byte chunks. Final counts describe the assembled stream.
A global event sequence captures observed cross-stream order; exact physical
ordering between independent OS pipes is not promised.
Non-UTF8 bytes use explicit base64 payloads in JSONL, never lossy replacement
as their canonical representation. Text decoding policy is explicit.
Images/other binary payloads are base64 JSONL events in v1, no sidecar blobs,
unless the cost of literal "all things in JSONL" changes this decision.
A read via H in Python returns data; it does NOT insert that data into context.
H is not a notebook expression-result/variable cache.

"Full" means no silent truncation of stored session payloads. Stream to disk,
do not accumulate all H in RAM. Disk-full/write failures halt new side effects,
preserve what was committed, and report incomplete capture. No false success.
Fresh files/directories are owner-only (0600/0700 on Unix).
Auth protocol secrets, API keys/refresh tokens, and password entry are NOT
session history. getpass is recorded as a secret-input event without its value.
Normal stdout/user/code may contain arbitrary secrets and are retained in full;
warn users, do not silently redact them. This exception needs approval.
Never send auth headers/tokens to model context or status/log output.
A torn final JSONL line can be diagnosed and ignored on recovery; corruption
in the middle must fail explicitly. Avoid modifying originals to "repair" them.
One writer per session (exclusive lock). Schema upgrades must be explicit.

## 5. Python API and context (proposed signatures)
agent.say(text)
  User-facing Markdown event; stored under H.say; no context inclusion.
  No final flag. Does not stop the loop.
agent.loop.stop(*, wakeup=None)
  Stages return to user after current cell successfully finishes.
  Does not terminate Python or the program. Subsequent code in the cell runs.
  wakeup=(seconds, reason) optionally requests a one-shot delayed continuation;
  None schedules no wakeup. Repeated calls replace the staged stop request.
  Arm the wakeup only when a successful cell actually stops the outer turn.
  On cell failure/cancellation, failure wins and the staged stop/wakeup is
  discarded. Steering that overrides stop also discards its staged wakeup.
  Stop settlement and wakeup creation form one durable transaction; section 29
  defines scheduling, validation, recovery and management.
agent.context.read_text(text_or_ref, *, start=0, stop=None, max_chars=8000)
  Accepts selected text directly, or a typed H reference with optional slicing.
  Commits a context item, then returns that item's stable context ID.
  It does NOT just print or return the payload. Printing never inserts context.
  Text offsets are Unicode code points; a separate lines selector may be added.
  Default max_chars is 8000. A selection above the maximum raises an error;
  it is never silently truncated. The caller may explicitly select a smaller slice.
agent.context.read_raw(ref)
  Explicitly attaches a supported raw image to context after capability,
  format, size and decoding checks, then returns the new stable context ID.
  The active attachment binding belongs to that context item; the immutable
  image payload belongs to H.raw. Removing or textually replacing the item
  must remove the active binding without removing or rewriting H.raw.
  No automatic image transmission from display(), file creation, or bytes.
  v1 proposal: raster images only, not PDFs/audio/video.
agent.context.items()
  Returns IDs, roles, references and counts, not full payloads. For an active
  raw attachment it also returns non-payload metadata: attachment kind, MIME,
  H.raw reference, dimensions/byte count when known, and budget estimate.
  An omitted/detached image must not still be reported as active.
agent.context.collapse(start_id, end_id, summary)
  Standalone literal-only context control using stable boundary IDs.
  Exact range/end-text preservation and pressure behavior are in section 6.
  Originals stay in H; rendered summaries include merged provenance (section 12).
  No implicit LLM call; protected system instructions are not collapsible.
agent.sh(command, *, cwd=None, env=None, timeout=None)
  Runs shell, journals source/output/status, returns a structured result
  containing refs and counts. No automatic context payload.
  Shell/cwd defaults must be documented; timeout does not mean rollback.
agent.bgtasks.run(source, *, kind="shell", cwd=None, env=None, timeout=None,
                  name=None, wakeup_reason=None)
  Starts an independent background job and returns task metadata including
  stable id/status and immediately reserved stdout/stderr H references.
  kind is shell or python. Python jobs use a fresh isolated interpreter, not
  the main persistent namespace; they have no agent/H helper bridge in v1.
  Source/output/status stay in H; output is never automatically selected.
  wakeup_reason opts into one continuation on terminal task completion.
agent.bgtasks.list(state=None)
  Returns task metadata only; optional exact-state filter.
agent.bgtasks.get(task_id)
  Returns current metadata/counts/completeness and H references, not payloads.
agent.bgtasks.kill(task_id, *, force=False)
  Requests bounded process-group cancellation; force requests immediate KILL.
  Returns metadata, not a promise of rollback or synchronous termination.
  Repeated kill of a terminal task is harmless. Missing task IDs reject.
  Blocking wait/live follow and convenience run aliases are not in initial scope.
agent.loop.reset_python()
  Proposal: schedules fresh CPython after this cell. No automatic replay;
  records new generation and inserts the same variables-lost lifecycle notice.
  Explicit terminal /reset can kill a wedged child; preserve H and context.

Example:
  import agent
  r = agent.sh("pytest -q")
  agent.context.read_text(r.stdout, max_chars=6000)
  # The selected text appears in the NEXT model request, not in this stdout.
  agent.say("Tests inspected.")
  agent.loop.stop()

Metadata after every operation includes execution/request IDs, H refs, status,
exit code where applicable, byte/char/line counts,
and completeness flags. No exception message, source line, or traceback text.
Python success maps to exit_code=0; cell exception to 1 while the process stays
alive; signal/process death is separate. Shell returncode is its actual value.
Define lines as newline count plus one for a nonempty unterminated last line.
Char count is nullable for undecodable bytes; byte count is always exact.
Nested helper replies must be delivered to the running Python child even though
the next outer model request waits for cell completion.



### read_text accepts selected text directly (confirmed)
Support the ordinary Python idiom:
  agent.context.read_text(H.stderr[44][:4000])
H text-stream indexing/slicing returns text; read_text(text) explicitly inserts
that text into context. No required start/stop boilerplate. The agent can define
ordinary Python helpers to shorten repeated inspection.
Retain reference-based selection as an optional convenience, not a requirement.
A plain string is text, never implicitly a path or history-reference identifier.
Journal the selected text as a context event so resume reproduces it exactly.
When a typed history reference carries provenance, preserve it; do not guess
provenance from matching string contents. The original stderr remains in H.
Merely slicing H, returning a string, or printing it still does not insert it.
Configured context limits apply to the selected text as to reference reads.
Tests cover direct sliced-text insertion, ordinary helper functions, resume
of selected text, and no accidental interpretation of text as a file/ref.

### Execution failures and explicit inspection (confirmed)
Compile each complete cell before executing it. A syntax/indentation error
executes none of that cell. Serialize its full standard Python diagnostic,
including source location, into that execution's H.stderr stream.
An uncaught runtime exception writes its full Python traceback to the same
stream after any existing stderr output. Preserve earlier stdout/stderr and
side effects; do not roll back or automatically replay. Python normally stays
alive after either failure. Process death has a separate status.
Caught exceptions behave like ordinary Python: no harness-added traceback.

The automatic model observation contains status, stdout/stderr H indices and
byte/char/line counts, plus completeness flags. No automatic exception type,
message, source excerpt, traceback or other error payload.
Example: status=error; stdout=H.stdout[43] (0 chars);
stderr=H.stderr[44] (812 chars).
H stream entries support text inspection/slicing in Python, e.g.
H.stderr[44][:4000]. Merely obtaining or printing this slice does NOT send it
to the model. Use agent.context.read_text(H.stderr[44][:4000])
to explicitly select those characters into the next model request.
Original bytes stay available losslessly; undecodable text uses the documented
decoding policy, not silent corruption of the stored stream.

Collapse/control validation failures follow the same convention even when
Rust validates without running Python: journal the attempted source and put
the full validation diagnostic in its logical H.stderr record. Report only
failure status, stream refs and counts automatically. Do not modify the
selected context range. Successful collapse remains silent.
Forced mode must not automatically leak diagnostics. Restricted explicit
stderr reads are allowed as specified in section 6; other ordinary execution
remains prohibited.

Tests cover syntax error with zero executed side effects; runtime failure
with preserved preceding effects/output; caught exceptions; Python still
usable; tracebacks preserved in H but absent from automatic model context;
explicit stderr reads; slicing/counts; non-UTF8 stderr; collapse validation
failures; and explicit forced-mode error inspection without arbitrary execution.

## 6. Collapse and pressure: follow this harness
Follow the current harness's exact boundary behavior: start is included; end is excluded from summarized content.
Replacement retains start ID. If end is a user message or earlier summary,
append its text verbatim to the replacement and remove its old context item.
If end is marker-only, there is no text to append. Later context stays intact.
This prevents losing the task at the end boundary. Earlier summaries may be
collapsed again. Replacement must strictly reduce the context's rendered size.
Reject missing/reversed IDs and stale plans. Commit atomically against a context
revision so queued user input cannot be deleted by a racing collapse.
System instructions are outside collapsible history.

Every accepted code submission is saved in H.code, including rejected collapse
submissions. Original messages already exist in the session journal; collapse
records their event references and new summary. No `collapsed` global/collection
and no duplicate archive of full messages. H/context APIs expose original refs.
Collapse detaches every image bound to any consumed context item, including
an image whose ID is the retained start boundary and an image at a preserved
end boundary. Retaining an ID or copying the end item's text/provenance must
not retain its binary attachment as an accidental side effect. The replacement
may contain an H.raw reference but is text-only. Originals remain immutable in
H.raw; explicit read_raw creates a fresh context item/attachment to reattach
one. Commit the replacement items and resulting active-attachment set in the
same context transaction, and persist enough detach state that resume cannot
resurrect an attachment from an older attachment event.

Expose stable boundary markers at user turns, every 10 completed cells, and
on context pressure (cadence provisional, inherited from this harness).
Every 50 model responses, remind the model to compact unnecessary history.
Above 90% context usage, insert a fresh end marker and FORCE collapse.
In forced mode accept only standalone collapse or restricted explicit stderr
reads as specified below. Collapse form:
  agent.context.collapse("start-id", "end-id", "summary")
Three literal strings; no assignments, imports, arbitrary expressions or code.
Parse/validate without executing it in the Python namespace. It is a reserved
context-control form, not a tool schema or provider tool call.
Outside forced mode use the same standalone form for consistent atomicity.
Invalid attempts yield failure metadata and H.stderr refs/counts only;
remain in forced mode. Full diagnostics require explicit inspection.
Only resume ordinary execution when pressure is below the threshold.
If protected instructions/task or an oversized selected payload makes useful
compaction impossible, stop for user intervention; no silent deletion or loop.
Use provider usage when available plus conservative local estimation for the
NEXT request, including attachments. Forced mode cannot itself exceed the
provider limit: reserve room for the marker, instruction, and response.
If already over limit, fail visibly for manual context reduction, never send a
known-invalid request repeatedly. Exact token estimator/guard-band is TBD.
Context read limits and collapse reduction checks apply before committing.


### Successful collapse: keep the call, no success output
The retained collapse-call source is the ONLY representation of the supplied
summary in active context. It already contains the selected boundary IDs and
summary. Do not also render a separate summary message.
Replace the selected content with this call at the retained start boundary ID;
remove its original turn-position copy if one was already present. Annotate
this retained item with merged original H ranges, without repeating the summary.
Keep preserved end-boundary text once, per the boundary rules above.
Original content and the submitted call remain immutable in H.

Like an ordinary Python function, collapse returns None on success and raises
an error on failure. Successful collapse emits no receipt, confirmation,
stdout, stderr or extra success context item. No error means success.
The context replacement is committed atomically and journaled internally.
Generic execution-status metadata may follow the normal harness contract;
it must not add a collapse-specific success message.
On failure, save the error in H.stderr and report only failure metadata;
leave the selected context range intact.
Never execute an invalid control submission.
Repeated/recovered commits with the same operation ID cannot duplicate the
retained call. Resume reconstructs it without replaying the call.

Tests inspect actual model-request context:
- retained call contains the supplied summary once; no extra summary message;
- replaced payloads are absent from context but retrievable through H;
- successful return is None, with no receipt or collapse-specific output;
- call metadata exposes the merged original H ranges;
- re-collapse removes old in-range calls and unions original ranges;
- failure/stale revision raises an error and leaves the selected range intact;
- duplicate operation IDs and resume do not duplicate the retained call.


### Forced-mode stderr inspection (confirmed)
While forced compaction is active, accept only:
1. the standalone literal-string agent.context.collapse(...) form;
2. a standalone agent.context.read_text(H.stderr[index][start:stop]) form.
For stderr reads, index and slice bounds are integer literals; omitted bounds
are allowed subject to configured read limits. Also permit H.stderr[index]
without a slice if the entire entry fits those limits.
Validate this restricted syntax without evaluating arbitrary Python. Resolve
only the named H.stderr entry and selected slice, then perform the context read.
No helper calls, assignments, imports, other H collections, or arbitrary code.
Outside forced mode, ordinary Python helpers remain available.

An explicit stderr read does not exit forced mode or count as compaction.
Keep the gate active until a successful collapse reduces pressure sufficiently.
Apply context-budget checks before inserting the selected text; an oversized
read fails with metadata and a diagnostic saved in H.stderr, not silent loss.
No automatic stderr inclusion, even for an invalid collapse or invalid read.

Tests cover explicit stderr inspection after a failed collapse; literal index
and slice handling; rejection of side effects hidden in arguments and helper
calls; rejection of other stream reads; read-budget failure; and continued
forced mode after a successful stderr read.

## 7. Scheduling, routing, interruption
Exactly one foreground outer request or persistent-worker Python cell is
active at a time. Independently managed background shell/isolated-Python jobs
may run concurrently under section 29; they never enter the main namespace.
The Rust event loop must still service terminal input, IPC, output drains,
task controls, timer deadlines and cancellation during foreground and idle waits.
The inner agent.llm call is supported during a Python cell; it is tracked as
its own request, not a competing outer turn. No parallel model subcalls in v1.

Routing at the terminal (not within Python), frozen:
- ordinary text: save complete H.user message, queue, deliver capped user task.
- !command / @source: run shell/Python locally and journal everything; invisible
  to the model, including their source, output, completion metadata and previews.
- !!command / @@source: run shell/Python and deliver the interaction as a capped
  user message, with counts and the full H.user reference.
- /commands: local controls, not forwarded as model prompts.
For direct interactions, H.user stores the complete user-message representation:
source plus tagged captured output/status. Stream bytes also remain losslessly
in H.stdout/H.stderr, linked to that message. A source-only pending user record
and completion event assemble the logical message without mutating old events.
Hidden interactions are available in H but never auto-selected or used to
trigger the outer model loop. Hidden means not auto-forwarded, not secret.
Visible interactions obey normal user-turn queue ordering; do not inject them
in the middle of an active provider request.
Model-visible user-message cap is configurable; proposed initial default 8000
characters. Include a prefix, explicit omitted count, full size/line counts and
H.user[index] reference. This is message routing, not a silent read truncation.
Human previews are separately limited to 12 lines. Neither preview mutates H.
Images are not attached merely because a direct command wrote an image file.
Queued shell/Python operations never race active Python execution.

Pi-like queues: Enter while busy -> steering FIFO; Alt+Enter -> follow-up FIFO.
Steering is delivered at next safe request/cell boundary; it does not interrupt
the cell. Follow-ups wait for agent.loop.stop() or normal turn completion.
Support one-at-a-time/all drain modes with explicit defaults (propose Pi defaults).
Alt+Up restores queued input to editor; preserve kind, order and multiline text.
On abort, restore unconsumed messages visibly; never drop or silently submit them.
Queue IDs and accepted/dispatched/restored/cancelled states survive crashes.
Resume does not automatically dispatch pending work; show and ask first.

Ctrl-C priority:
1. active prompt/menu: cancel that interaction;
2. active provider/subrequest: cancel and drain it; reject late completion;
3. active Python/shell: signal its process group, retain partial outputs;
4. idle with editor text: clear editor;
5. idle and empty: proposed double Ctrl-C within 500 ms quits.
If a prompt was Python input(), cancellation causes EOF/KeyboardInterrupt and
cancels the owning cell, not a hung stdin request.
Repeated Ctrl-C during unresponsive execution escalates to killing that child
and its process group; Python then restarts fresh with a variables-lost notice.
No rollback, replay, or success claim. Exact grace timeout remains a question.
Detached external processes are not guaranteed to die; no sandbox/process-tree
containment promise. POSIX-only v1 is a simplifying proposal.
Escape closes menus; whether it also aliases abort is still open.

Each operation has a monotonic cancellation/worker-generation token. Late IPC,
provider chunks, stdin replies and completions cannot affect a new operation.
Cancellation is journaled once and does not trigger automatic provider recovery.
agent.loop.stop(), queued steering and Ctrl-C races have deterministic order:
committed cancellation wins; otherwise steering takes priority over stop, and
follow-ups begin only after the turn's final cell is settled.
New submissions supersede a pending provider-retry checkpoint explicitly.
Never restart completed Python cells after provider transport failures.

## 8. Providers and inner model calls
Rust owns credentials, provider HTTP/streaming, catalog, model resolution,
reasoning effort, retries and cancellation. No Python provider SDK.
Follow the pinned Pi/Pig reference for:
- /login, /logout, credential status, browser/device OAuth flows;
- API-key environment fallback and credential precedence;
- catalog/model listing, search, provider/id resolution and model selection;
- provider-specific request/response transformation, effort and usage fields.
Before implementation, extract fixtures and settle the initial provider set.
Do not claim parity with all Pi/Pig providers based on one successful request.
No plugins/custom executable provider hooks; explicit config may define an
OpenAI-compatible base URL/model (decision). No dynamic extension discovery.
Credentials belong in ~/.py, not Pi/Pig files. Import existing credentials only
with explicit user consent (decision). Never inspect/import them silently.
Refresh/auth writes must be atomic and owner-only. Login cancellation is safe.
Autodiscovery means a bundled known catalog plus optional authenticated refresh,
not probing every provider at startup. Offline listing must still work.
Expose configured / known / credential-backed / capability-supported states
separately; catalog presence does not prove invocation permission.

Proposed Python namespace:
  agent.llm.list(provider=None, capability=None, refresh=False)
  agent.llm(prompt, *, model=None, effort=None, images=(), max_tokens=2048)
  agent.llm.image(prompt, *, model, ...)  # required image-generation capability
list returns structured model descriptors, not an automatic context dump.
llm returns text to Python; never executes it or auto-inserts it into outer
context. Journal prompt, response, usage and refs; callers use read_text.
Select by capability: text, image-input, image-output. Do not pretend that a
vision model necessarily generates images. Image output is stored in H.raw;
explicit read_raw is required to attach it to the outer model context.
Image generation is required. Extract supported provider scope, signature and
limits before writing its E2E contracts; do not mark it done with a stub.
Outer model invocation carries ZERO tools/function schemas; response is Python
source, not a tool call. Decide code-fence stripping vs strict rejection.
Do not execute partial streamed source. Journal complete/partial responses,
then validate a completed response before dispatching exactly once.
Provider transport retries replay only requests deemed safe, never cells.
Timeout after unknown acceptance can incur duplicate billing; expose uncertainty.
On exhausted retries keep a request-only checkpoint. /resume continues that
request; completed cells and helper side effects are not re-run.

## 9. Terminal minimum, without IPython
Interactive editing should feel like this harness: multiline entry, paste,
cursor navigation, searchable history, syntax highlighting, explicit-Tab
completion, readable Markdown say output, live streaming and responsive input.
Completion covers slash commands/arguments, models/effort, workspace paths,
and safe Python names. Avoid arbitrary getattr/property execution during
completion. Inspect namespace only when worker is idle; cache while busy.
Directory completion reads names, not file contents, and bounds work.
Hidden submissions have a visible local indicator. Menus and prompts must not
garble streaming output. Escape cancels menus. Keep terminal restore reliable.

Minimum commands:
 /help /hotkeys /login /logout /auth /model /models /effort (/think alias)
 /status /config /interrupt /quit (/exit alias)
 /new /sessions /session /resume /recovery /reset /context /compact
 /tasks /task /bg /wakeups /wakeup
No /plugins, packages, /skills management, tool controls, or extension commands.
Durable entries use ordinary files (section 30); /config manages settings.
Resolve /resume session-versus-request ambiguity before implementation;
propose /session resume <id> for sessions and /resume for pending requests.
Help must describe routing, no sandbox, explicit context, reset/resume loss,
queues, interrupts, input JSON, and supported provider/capability limitations.
/compact invokes the forced explicit-collapse flow; it is not an invisible
summarizer or permission to discard user tasks.

Status bar: provider/model, effort, worker/loop state, queue depth, context use,
cumulative input/output tokens and cumulative cache-hit tokens. Show unknown
when provider accounting is absent; do not treat cached tokens as extra input
tokens. Label outer versus inner totals; grand total includes both once.
Usage events have request/attempt IDs for deduplication. Cancelled/retried
requests may be billable; only known usage counts as exact. Keep cumulative
session usage on resume; optionally display current-run totals separately.
If cache-hit ratio is shown, define denominator over applicable input tokens.
No invented provider fields, cost estimates or successful-use claims.

## 10. Machine interface
Support output JSONL and input JSONL together, without a server.
Proposed CLI: --json (stdout events), --json-input (stdin commands), both
combinable. "--json-inpout" was a tentative name, not a fixed spelling.
No terminal escape sequences, progress prose or Markdown rendering in JSON.
Each stdout line is a typed/versioned event; diagnostics use stderr.
Input command kinds: submit (text + steering/follow-up), shell, python,
interrupt, stdin_reply, reset, resume_session, context control, quit.
Every command has an ID; emit accepted/rejected and eventual completion.
Duplicate IDs must not dispatch code twice within a session.
Local Rust/Python IPC uses separate framed pipes, not Python stdin/stdout.
Thus print(), os.write(), subprocess output, input() and protocol replies
cannot corrupt one another. Capture FD-level output, not only sys.stdout.
A stdin prompt produces an event; replies are correlated to prompt/operation.
EOF/cancel is explicit. Invalid JSON cannot execute code or crash the loop.
Partial final frames, Unicode, large streams and output backpressure are tested.

## 11. Single-file scope and verification discipline
Target 10,000–15,000 lines MAX for main.rs, including spec, all handwritten
runtime code, embedded Python, fixtures and tests. Aim smaller where possible.
Do not inflate a smaller correct solution to reach 10k. No generated bulk
catalogs/fixtures used to evade the line budget; dependencies are allowed but
not a renamed out-of-tree harness. Minimal Cargo metadata may sit alongside.
E2E tests launch the actual CLI, which launches its embedded worker; no second
handwritten Python implementation. External provider fixtures contain no keys.
If scope cannot fit, narrow v1 with the user rather than splitting silently.
No runtime implementation before the frozen contracts have failing E2E tests.

Process:
1. Review the spec and resolve decisions. Assign stable requirement IDs.
2. Write behavioral tests referencing those IDs; run them and retain meaningful
   failures against missing behavior, not just compile errors.
3. Implement the smallest code that makes tests pass.
4. Run E2E CLI/PTY, provider-fixture, image, crash/recovery and live smoke tests.
5. Audit EVERY normative item for specific test coverage; add missing tests.
6. Mark [x] only after tests pass AND the coverage audit is complete.
7. A changed requirement returns to [ ] until reverified.
Coverage is semantic: naming a requirement in a test is not proof. Check failure
paths, boundary conditions and races. Compile-time presence checks alone do not
prove execution behavior. No skipped/ignored tests counted as verified.
Maintain spec checklist and requirement-to-test map in this very .rs file.
[x] means implemented and verified, not merely "decided" or "written".
Decisions may be approved while their implementation box remains [ ].
Require a reviewable test witness (name, scenario/assertions, latest outcome)
for every leaf requirement; split grouped rows before implementation.

Draft acceptance families (none fully verified):
- FILE: line budget, one source, embedded worker/tests/spec, no plugins/IPython.
- EXEC: persistent values across cells, no expression display, fresh reset,
  ordinary Python syntax, no tool schema, no partial-source execution.
- HISTORY: every code/user/input/output/raw event survives reload byte-exactly;
  FD writes and subprocesses captured; large output bounded in RAM; disk full,
  incomplete operations, torn last line, corruption, locking, secret exception.
- CONTEXT: metadata only by default; H reads/say/llm do not insert payloads;
  explicit text/raw inclusion; exact slicing/counts/capability limits;
  generated source retention; visible/hidden prefixes.
- COLLAPSE: literal-only gate; range/end-text merge, summary-on-summary;
  IDs preserved; originals retrieved via H; image eviction/re-attachment;
  reduction, stale-plan race, forced threshold, reminder cadence, impossible
  reduction stops safely; no execution of invalid forced-mode cells.
- QUEUE: FIFO, both drain modes, steering boundary, follow-up after stop,
  restore with Alt+Up, abort restoration, crash pending state, stop/input races.
- CANCEL: interrupts at model/cell/shell/stdin/inner-LLM phases; escalation;
  late messages rejected; partial output retained; no replay, terminal restored.
- RESUME: restores H, context and usage into fresh namespace; explicit notice;
  old variables absent; no old code/provider request executed on load.
- PROVIDER: pinned auth/catalog/effort/invocation fixtures; cancellation, refresh
  precedence, offline list, error/usage normalization, capability filtering;
  no auth leakage and no request retry causing duplicate Python side effects.
- UI: PTY tests of multiline/paste/history/completion/help/menu/status/Markdown;
  streaming doesn't eat input; all documented commands exercised.
- JSON: schema round trip, command ID deduplication, bad/truncated input,
  stdin correlation, EOF/cancel, no ANSI/prose, backpressure and ordering.
- USAGE: cache tokens counted once, outer+inner cumulative totals, unknown
  partial/cancelled attempts, resume continuity, model/effort display.
- BACKGROUND: isolated jobs, interleaved durable output, responsive controls,
  stop settlement, idle/busy wakeups, draft preservation, crash uncertainty,
  explicit recovered-wakeup confirmation and bounded shutdown (section 29).

Representative tests to write, not green placeholder tests:
  context_print_is_not_insertion
  context_raw_requires_explicit_supported_attachment
  collapse_preserves_end_user_text_and_start_id
  forced_collapse_rejects_python_side_effect
  journal_recovery_restores_exact_bytes_and_context
  resume_starts_fresh_without_replaying_completed_cell
  steering_waits_for_cell_boundary
  ctrl_c_interrupts_worker_and_discards_late_completion
  provider_retry_does_not_repeat_python_side_effect
  json_duplicate_command_id_does_not_execute_twice
  status_cumulative_cache_tokens_do_not_double_count
More leaf tests needed: these names are a plan, not a complete coverage claim.

Draft implementation checklist:
[ ] R01 Single-file layout and line budget.
[ ] R02 Persistent ordinary Python and namespace/IPC isolation.
[ ] R03 Lossless global JSONL history and read-only H.
[ ] R04 Explicit context and metadata-only automatic observations.
[ ] R05 Harness-compatible collapse and forced pressure handling.
[ ] R06 Terminal routing, visible/hidden execution.
[ ] R07 Queues, Ctrl-C and operation-generation safety.
[ ] R08 Fresh-worker resume/reset with restored H/context.
[ ] R09 Pi/Pig-compatible approved provider subset and inner LLM calls.
[ ] R10 Completion/help/Markdown/status and all minimum commands.
[ ] R11 JSON input/output and command deduplication.
[ ] R12 Usage accounting and request-only recovery.
[ ] R13 Requirement-by-requirement passing tests and coverage audit.
[ ] R14 Background-job lifecycle, service pump and one-shot wakeups.
All boxes intentionally remain empty.

## 12. Collapsed provenance (confirmed)
Every collapsed context item renders the retained call exactly once:
  [Collapsed originals: H.events[start:stop], ...]
  agent.context.collapse("start-id", "end-id", "summary")
The summary is present only as the call argument, not as an additional message.
This supersedes any earlier wording implying a separately rendered summary.
Ranges refer to immutable original-history event sequence positions, half-open.
H.events[start:stop] is readable in Python; agent.context.read_text can select
those original text events for model inspection. Selecting a mixed event range
requires explicit text projection; raw attachments require read_raw.

The call, summary argument and provenance are persisted in the collapse event. Never store
only a pointer to another summary. When collapsing previously collapsed items,
union their original provenance ranges with the originals of newly included
items. Sort and merge adjacent/overlapping ranges recursively. Preserve gaps.
Do not replace provenance with just the sequence number of a prior collapse
event; that would hide the actual originals.
The merged end user/summary text described in section 6 remains verbatim and
carries its own provenance. Rendering distinguishes summarized originals from
the preserved end text; later collapse unions both. Re-collapse must not
duplicate end text or lose immutable H.raw references, but neither preserved
text nor recursive provenance makes an old image attachment active again.
Original ranges are immutable across resume. Removed context does not remove
history. Reading originals back adds a new selected context item, not an undo.
Test exact range unions across repeated collapse, adjacent and disjoint ranges,
end-user merging, end-summary merging, image originals, and resume.
No separate `collapsed` variable or shadow archive is needed.

## 13. Frozen decisions and remaining details
Frozen by the user:
- Generated source stays in H.code unchanged and in context until explicit
  collapse. Automatic context removal targets old outputs only.
- ! shell / @ Python are hidden; !! shell / @@ Python are model-visible user
  interactions. All are durably saved. User messages have capped context
  inclusion and complete H.user originals, with refs and counts.
- Providers follow Pi/Pig/current harness support; minimal suitable Rust crates
  are allowed. Do not redefine scope as a text-only single-provider demo.
- Both image input and image generation are required; implementation order is
  free. End-to-end evidence must prove both actually work.
- End-to-end tests, including the interactive CLI, are the acceptance gate.
  No unit-test suite as a substitute for exercising the running product.
- POSIX first; one Rust source plus minimal Cargo metadata/dependencies.
- Human code/stdout/stderr previews show at most 12 lines each, with counts and H refs.
- Explicit reads default to 8,000 characters; accept a configurable maximum
  range and error above it. No silent truncation or implicit payload insertion.
- Durable intent/completion commits; global JSONL sessions; fresh-worker resume.
- agent.context.usage() exposes context/budget accounting to Python.

Remaining details, not grounds to reopen these decisions:
- Exact provider/capability/auth matrix and crate selection, extracted from
  pinned references. Record conflicts explicitly before claiming parity.
- Token estimation/guard band and image size limits.
- Exact idle-key/escalation timing and queue mode defaults.
- Global environment/config file names and startup behavior.
- Catalog refresh policy, credential migration consent, secret-input handling.
- Whether richer completion/editor/clipboard features are needed beyond the
  agreed baseline.
Implementation defaults must be documented/tested, not silently invented.

## 14. Audit ledger format
Before implementation, expand each Rxx family into independently testable leaf
items. Each leaf carries [ ]/[x], stable ID, normative behavior, named tests,
failure scenarios, latest validation evidence and reviewer outcome. Do not mark
an entire family verified while any leaf is uncovered.
The closing audit checks both directions: every spec leaf has behavioral tests,
and every promised behavior in code/help is specified. A green suite with an
uncovered spec item is NOT completion.

## 15. Context transactions and identity
A context item ID identifies a context boundary, not an H array index.
H IDs refer to immutable history records. Never renumber either on collapse.
Provenance uses H event sequence intervals; ranges can span several records
belonging to one logical stream. H.stdout/H.stderr indexes are stable logical
stream indexes, not chunk sequence numbers.
Keep summarized-original provenance distinct from preserved-end provenance.
Union both only when that whole replacement is subsequently summarized.

Model context has an explicit order and revision. A collapse uses the revision
of the request that produced its source; no fourth argument is necessary.
Validate bounds, summary size/reduction, provenance and budget before committing.
Queue arrivals persist in H first; dispatch into context only at a safe boundary.
A queued arrival alone does not mutate the in-flight request's context.
If another explicit operation changes context, reject a stale collapse rather
than guessing which records the model intended to remove.

Construct each provider request from committed context. Successful explicit
reads during a cell become visible to the next request. H retrieval itself
does not trigger a model invocation.
For the retained collapse call, preserve original submitted source in H.code.
Its active-context representation carries original-H provenance and retained
boundary ID. The call's boundary arguments remain historical, not instructions
to replay it. A retained call is context data, never executed on resume.
Prior summaries are already in H through their call records, so no duplicate
summary payload needs a separate archive.
Collapse without stdout does not suppress normal failure-status metadata.

### Error-path accounting
Compile/validation failures still allocate logical stdout and stderr records,
even when stdout is empty. Both references and counts are therefore inspectable.
A runtime exception appends one harness traceback, not a second copy already
emitted by a worker wrapper. A worker crash records incomplete-capture status;
do not fabricate a Python traceback for a killed process.
Metadata counts cover the exact captured stream; failure metadata has no
exception message/type, path, source excerpt, or automatically selected bytes.

### Restricted execution gates
Outside forced mode, ordinary cells run normally. The reserved standalone
collapse form is intercepted as a transaction; a mixed cell containing collapse
is rejected before any preceding statement executes.
Inside forced mode, inspect the syntax before execution. Only the documented
collapse and stderr-read forms are accepted. Comments/whitespace are allowed,
but a second statement is not. No negative/computed indexes, slice steps,
attribute substitutions, f-strings, string concatenation, or expression calls.
Allow nonnegative literal indexes and bounds; reject a reversed slice.
A failed validation writes its diagnostic to H.stderr. That entry can then be
explicitly inspected via the permitted stderr-read form.
These are proposed parser restrictions; confirm if more Python syntax is wanted.

## 16. Draft leaf requirement / behavioral-test ledger
These are PLANNED test names, not implemented tests. All status boxes remain
empty. Pending decisions qualify affected rows; splitting provider/command
parameterized rows further is required once that scope is frozen.
For each row, add latest run/evidence and semantic-review outcome in this file
when tests exist. Maintain the mapping as executable test metadata later.
This draft ledger does not replace the normative prose: audit remaining prose
again before implementation so no requirement is overlooked.
[ ] R01.01 Single handwritten Rust source contains spec, tests and Python bootstrap; no out-of-tree runtime.
      Tests: layout_is_single_source; evidence: not run; review: pending.
[ ] R01.02 Total source stays <=15,000 lines; smaller is allowed.
      Tests: source_line_budget; evidence: not run; review: pending.
[ ] R01.03 No plugin discovery/loading or IPython/Jupyter dependency.
      Tests: no_plugins_or_notebook_runtime; evidence: not run; review: pending.
[ ] R02.01 A variable defined in one ordinary cell survives the next.
      Tests: worker_namespace_persists; evidence: not run; review: pending.
[ ] R02.02 A bare expression has no implicit display/output.
      Tests: ordinary_python_has_no_expression_display; evidence: not run; review: pending.
[ ] R02.03 Compile syntax-invalid cells before execution; no preceding statement runs.
      Tests: syntax_error_executes_nothing; evidence: not run; review: pending.
[ ] R02.04 Runtime failure preserves prior effects; the next cell can execute.
      Tests: runtime_error_preserves_worker_and_effects; evidence: not run; review: pending.
[ ] R02.05 Unrestricted normal filesystem/subprocess operations run as the current user.
      Tests: execution_has_no_permission_broker; evidence: not run; review: pending.
[ ] R02.06 Print/os.write/subprocess streams cannot corrupt separate framed IPC.
      Tests: fd_output_does_not_corrupt_ipc; evidence: not run; review: pending.
[ ] R02.07 Plain input replies/EOF/cancel are correlated to their pending execution.
      Tests: stdin_is_correlated_and_cancellable; evidence: not run; review: pending.
[ ] R02.08 No globals injected beyond the agreed bootstrap H/agent interface.
      Tests: worker_bootstrap_namespace; evidence: not run; review: pending.
[ ] R03.01 Session event sequence and schema persist under ~/.py/sessions/*.jsonl.
      Tests: journal_schema_and_global_location; evidence: not run; review: pending.
[ ] R03.02 H indexes are stable read-only views after append and reload.
      Tests: history_indices_stable_on_reload; evidence: not run; review: pending.
[ ] R03.03 Full stream bytes survive Unicode, non-UTF8 and multi-chunk capture.
      Tests: history_streams_are_lossless; evidence: not run; review: pending.
[ ] R03.04 Cross-stream observed chunk order is journaled without promising physical pipe order.
      Tests: history_observed_order; evidence: not run; review: pending.
[ ] R03.05 Large streams write incrementally; H loading is lazy/bounded in RAM.
      Tests: large_stream_capture_is_bounded; evidence: not run; review: pending.
[ ] R03.06 Intent precedes dispatch; incomplete operation is unknown and never replayed.
      Tests: crash_after_dispatch_does_not_replay; evidence: not run; review: pending.
[ ] R03.07 Disk write failure stops new side effects and marks capture incomplete.
      Tests: journal_write_failure_halts_dispatch; evidence: not run; review: pending.
[ ] R03.08 Recover torn final line; reject middle corruption without modifying original.
      Tests: journal_torn_tail_vs_middle_corruption; evidence: not run; review: pending.
[ ] R03.09 Exclusive session writer lock rejects a second writer.
      Tests: session_has_one_writer; evidence: not run; review: pending.
[ ] R03.10 Session files/directories are owner-only on Unix.
      Tests: session_storage_permissions; evidence: not run; review: pending.
[ ] R03.11 Auth/getpass secrets never enter session payloads; ordinary history is not silently redacted.
      Tests: secret_input_boundary; evidence: not run; review: pending.
[ ] R03.12 All accepted/rejected source and direct/hidden submissions are retained.
      Tests: history_records_all_submissions; evidence: not run; review: pending.
[ ] R04.01 Default observations contain refs/status/counts, never automatic payloads.
      Tests: observations_are_metadata_only; evidence: not run; review: pending.
[ ] R04.02 Syntax/runtime tracebacks go to H.stderr; error observations omit type/message/source.
      Tests: errors_are_history_not_automatic_context; evidence: not run; review: pending.
[ ] R04.03 H slicing/printing and agent.say do not insert payload into context.
      Tests: print_and_say_do_not_select_context; evidence: not run; review: pending.
[ ] R04.04 read_text(H.stderr[i][:n]) inserts exactly the selected text.
      Tests: read_text_accepts_python_slice; evidence: not run; review: pending.
[ ] R04.05 Plain text is never implicitly interpreted as a path or H reference.
      Tests: read_text_string_is_data; evidence: not run; review: pending.
[ ] R04.06 Selected-text context persists exactly on resume; typed refs preserve provenance.
      Tests: selected_text_resume_and_provenance; evidence: not run; review: pending.
[ ] R04.07 Raw attachment needs explicit read_raw and capability/size/decode validation.
      Tests: raw_attachment_is_explicit_and_validated; evidence: not run; review: pending.
[ ] R04.08 Byte/character/line counts and undecodable character counts follow the contract.
      Tests: stream_counts_are_exact; evidence: not run; review: pending.
[ ] R04.09 Every failed execution has inspectable logical stdout/stderr entries, including empty stdout.
      Tests: failure_allocates_stream_entries; evidence: not run; review: pending.
[ ] R04.10 Context selection cannot silently overflow the configured budget.
      Tests: context_read_budget; evidence: not run; review: pending.
[ ] R05.01 Collapse includes start, excludes end and retains the start boundary ID.
      Tests: collapse_half_open_and_stable_id; evidence: not run; review: pending.
[ ] R05.02 User/summary end text is preserved once; marker-only end is discarded.
      Tests: collapse_preserves_end_boundary_text; evidence: not run; review: pending.
[ ] R05.03 Retained call is the sole summary representation; selected payloads disappear.
      Tests: collapse_summary_appears_once; evidence: not run; review: pending.
[ ] R05.04 Success returns None with no receipt/stdout/stderr/extra confirmation item.
      Tests: collapse_success_is_silent; evidence: not run; review: pending.
[ ] R05.05 Collapse rejection preserves selected context and stores its diagnostic in H.stderr.
      Tests: collapse_failure_is_atomic; evidence: not run; review: pending.
[ ] R05.06 Original records and submitted source remain accessible in H; no collapsed global.
      Tests: collapse_originals_live_in_history; evidence: not run; review: pending.
[ ] R05.07 Nested provenance unions adjacent/overlapping intervals and preserves gaps.
      Tests: collapse_provenance_union; evidence: not run; review: pending.
[ ] R05.08 Keep preserved-end provenance separate until whole replacement is re-collapsed.
      Tests: collapse_end_provenance_not_misattributed; evidence: not run; review: pending.
[ ] R05.09 Strict rendered-size reduction includes call, metadata and preserved end text.
      Tests: collapse_requires_actual_reduction; evidence: not run; review: pending.
[ ] R05.10 Stale revision/missing/reversed boundaries reject without deleting queued input.
      Tests: collapse_stale_or_invalid_boundaries; evidence: not run; review: pending.
[ ] R05.11 Collapse atomically detaches images from all consumed IDs, including retained-start and preserved-end IDs; explicit reread creates a fresh attachment.
      Tests: collapse_image_evict_and_reattach, collapse_retained_start_detaches_image, collapse_preserved_end_detaches_image, resume_does_not_resurrect_collapsed_image; evidence: not run; review: pending.
[ ] R05.12 Duplicate commits and resume neither replay collapse nor duplicate retained calls.
      Tests: collapse_idempotent_resume; evidence: not run; review: pending.
[ ] R05.13 Forced mode allows only literal standalone collapse and restricted stderr reads.
      Tests: forced_gate_accepts_only_documented_forms; evidence: not run; review: pending.
[ ] R05.14 Invalid/mixed control code cannot run side effects, even preceding collapse.
      Tests: control_gate_rejects_before_execution; evidence: not run; review: pending.
[ ] R05.15 Explicit stderr read after rejected collapse works but does not exit forced mode.
      Tests: forced_error_inspection_keeps_gate; evidence: not run; review: pending.
[ ] R05.16 Pressure forcing/reminders/markers follow approved threshold/cadence.
      Tests: pressure_threshold_and_cadence; evidence: not run; review: pending.
[ ] R05.17 Over-limit/impossible compaction stops visibly, without silent deletion or retry spin.
      Tests: pressure_impossible_compaction_stops; evidence: not run; review: pending.
[ ] R06.01 Visible !! shell and @@ Python enter as capped user interactions with complete H.user refs.
      Tests: visible_prefix_context; evidence: not run; review: pending.
[ ] R06.02 Hidden !/@ retain history but auto-insert nothing and do not start the outer loop.
      Tests: hidden_prefix_history_only; evidence: not run; review: pending.
[ ] R06.03 Terminal prefixes never become Python cell magic syntax.
      Tests: prefix_routing_is_terminal_only; evidence: not run; review: pending.
[ ] R07.01 Only one outer request/cell runs; UI, output drains and helper IPC stay responsive.
      Tests: single_outer_operation_responsive_loop; evidence: not run; review: pending.
[ ] R07.02 Steering FIFO waits for a safe boundary; follow-ups wait for turn completion.
      Tests: steering_and_followup_boundaries; evidence: not run; review: pending.
[ ] R07.03 One/all drain modes and Alt+Up restoration preserve kinds/order/multiline content.
      Tests: queue_modes_and_editor_restore; evidence: not run; review: pending.
[ ] R07.04 Abort restores pending input visibly; crash reload does not auto-dispatch queues.
      Tests: abort_and_resume_preserve_pending_input; evidence: not run; review: pending.
[ ] R07.05 Committed cancellation wins stop/input races; steering precedes uncommitted stop.
      Tests: stop_steering_cancel_order; evidence: not run; review: pending.
[ ] R07.06 Ctrl-C cancels model/helper calls and rejects late chunks/completions.
      Tests: ctrl_c_provider_cancel_generation; evidence: not run; review: pending.
[ ] R07.07 Ctrl-C signals executing Python/shell groups and retains partial outputs.
      Tests: ctrl_c_execution_retains_partial_output; evidence: not run; review: pending.
[ ] R07.08 Repeated cancellation kills a wedged worker; reset notice precedes reuse.
      Tests: ctrl_c_escalation_restarts_worker; evidence: not run; review: pending.
[ ] R07.09 Ctrl-C at input/menu handles cancellation without leaving the owning operation hung.
      Tests: ctrl_c_prompt_cancellation; evidence: not run; review: pending.
[ ] R07.10 Idle clear/quit and Escape follow the approved key policy; terminal always restores.
      Tests: idle_keys_and_terminal_restore; evidence: not run; review: pending.
[ ] R08.01 Resume restores exact committed H/context/usage into fresh Python.
      Tests: resume_restores_history_context_not_heap; evidence: not run; review: pending.
[ ] R08.02 Resume notice says variables are gone; no old source/request runs on load.
      Tests: resume_notice_without_replay; evidence: not run; review: pending.
[ ] R08.03 Reset preserves session history/context and changes worker generation.
      Tests: reset_preserves_session; evidence: not run; review: pending.
[ ] R08.04 loop.stop waits for cell success; subsequent cell statements run; exceptions win.
      Tests: loop_stop_settles_current_cell; evidence: not run; review: pending.
[ ] R09.01 Each approved provider login/logout/refresh/precedence flow matches pinned fixtures.
      Tests: provider_auth_reference_fixtures; evidence: not run; review: pending.
[ ] R09.02 Model discovery/list/resolution distinguishes known/configured/authorized/capable.
      Tests: model_catalog_resolution_fixtures; evidence: not run; review: pending.
[ ] R09.03 Offline listing works without automatic network or credential probing.
      Tests: model_listing_offline; evidence: not run; review: pending.
[ ] R09.04 Approved provider text/effort/stream/error/usage conversion matches fixtures.
      Tests: provider_invocation_reference_fixtures; evidence: not run; review: pending.
[ ] R09.05 Outer provider requests carry no tools/functions and never execute partial source.
      Tests: outer_model_has_no_tools; evidence: not run; review: pending.
[ ] R09.06 Inner llm returns data, not executable code or automatic outer-context payload.
      Tests: inner_llm_is_data_only; evidence: not run; review: pending.
[ ] R09.07 Image input/output capabilities are distinct; raw model output requires explicit inclusion.
      Tests: model_image_capabilities; evidence: not run; review: pending.
[ ] R09.08 Transport retry/checkpoint resumes requests only, never completed cells/helper effects.
      Tests: provider_retry_does_not_repeat_side_effect; evidence: not run; review: pending.
[ ] R09.09 Explicit cancellation suppresses recovery retries; new input supersedes old checkpoints.
      Tests: cancel_and_new_input_supersede_retry; evidence: not run; review: pending.
[ ] R09.10 Credentials never leak into model requests/history/status; auth persistence is atomic/private.
      Tests: provider_credentials_are_private; evidence: not run; review: pending.
[ ] R10.01 Multiline/paste/history/navigation/highlighting work in PTY scenarios.
      Tests: terminal_editor_basics; evidence: not run; review: pending.
[ ] R10.02 Tab completes commands/args/models/effort/paths/safe names without property execution.
      Tests: terminal_completion_is_safe; evidence: not run; review: pending.
[ ] R10.03 Every documented slash command has a command-dispatch/help behavioral test.
      Tests: terminal_command_coverage; evidence: not run; review: pending.
[ ] R10.04 say renders Markdown without stopping or inserting context; streaming does not eat input.
      Tests: terminal_markdown_streaming_and_input; evidence: not run; review: pending.
[ ] R10.05 Status shows model/provider/effort/state/queues/context and unknown-aware totals.
      Tests: terminal_status_fields; evidence: not run; review: pending.
[ ] R11.01 Versioned JSON input/output round-trips with command acceptance and completion.
      Tests: json_protocol_roundtrip; evidence: not run; review: pending.
[ ] R11.02 Duplicate input command IDs cannot dispatch a side effect twice.
      Tests: json_command_deduplication; evidence: not run; review: pending.
[ ] R11.03 JSON stdout contains no ANSI/prose; diagnostics remain separate.
      Tests: json_output_is_machine_only; evidence: not run; review: pending.
[ ] R11.04 Malformed/truncated JSON never executes code or crashes the event loop.
      Tests: json_bad_input_is_safe; evidence: not run; review: pending.
[ ] R11.05 stdin replies/EOF/cancel are operation-correlated in JSON mode.
      Tests: json_stdin_protocol; evidence: not run; review: pending.
[ ] R11.06 Slow readers/backpressure and large Unicode streams preserve protocol framing.
      Tests: json_backpressure_and_framing; evidence: not run; review: pending.
[ ] R12.01 Outer/inner usage totals count attempts once and do not double-count cached input.
      Tests: usage_deduplicates_cache_and_attempts; evidence: not run; review: pending.
[ ] R12.02 Cancelled/unknown accounting stays explicitly unknown, not falsely zero/exact.
      Tests: usage_unknown_and_partial_attempts; evidence: not run; review: pending.
[ ] R12.03 Session cumulative usage survives resume and model/effort changes.
      Tests: usage_resume_and_model_changes; evidence: not run; review: pending.
[ ] R13.01 Every normative leaf maps to meaningful behavioral tests; all pass without ignored/skipped credit.
      Tests: requirement_test_manifest_audit; evidence: not run; review: pending.
[ ] R13.02 Every [x] has retained passing evidence and semantic review; changes reset verification.
      Tests: verified_requirement_evidence_audit; evidence: not run; review: pending.
[ ] R13.03 Old Python is removed only after approved replacement scope is verified.
      Tests: migration_removal_gate; evidence: not run; review: pending.

## 17. Adversarial review before freezing
A. "Saved in full" is not a durability guarantee by itself. Define commit as
flush + fsync before dispatching an external side effect and before reporting
its durable completion (frozen). Output chunks may be batched; a crash
can leave partial/incomplete capture. Never label an unfinished stream complete.
If history persistence fails, cancellation is best-effort: already running
user code may have performed effects that the harness cannot reverse.
Test crashes before intent, after durable intent/before dispatch, after dispatch,
during stream capture, and after durable completion/before UI acknowledgement.
The unknown-outcome interval cannot be eliminated by tests or JSONL.

B. "No stdout on collapse success" means no collapse-produced output. Normal
zero-length stream records and generic execution metadata still exist. The
single retained call is the summary; the journal stores its source once plus
references/provenance for the context transaction. Resume must not invent a
second success receipt or a second summary.

C. H.stderr slicing is plain data extraction. The model cannot see a local
Python value just because it was computed. read_text explicitly adds it.
The UI previews at most 12 lines of stderr, with counts/H refs; the outer model still
sees only counts until a read. Terminal rendering and model selection must
be independent paths, tested separately.

D. A forced stderr read adds context, so it cannot bypass the provider budget.
Reserve enough room for bounded diagnostics and next model response. If an
error cannot be inspected within that reserve, offer human intervention rather
than looping through increasingly large diagnostic records.
General Python helpers remain prohibited in forced mode; use the literal form.
Do not quietly turn this gate into a sandbox for ordinary execution.

E. Ctrl-C cannot promise all filesystem/network effects stop or undo. Cancellation
of a provider call can leave unknown billing; killing CPython loses variables;
detached processes can outlive it. Tests assert harness lifecycle and observable
children, not impossible global rollback.

F. Tests for provider compatibility must exercise each approved provider and
auth method. Catalog fixtures alone are insufficient. Live smoke tests are
opt-in, use external credentials, and must not be the only deterministic
evidence for a spec item. Record the tested reference revision and capability.

G. The spec uses one runtime source file, not zero dependencies. Choosing POSIX,
a small provider subset and limited image scope is the main way to stay small.
If all providers/OAuth variants/image generation/platforms are required at once,
reassess the 15k budget before implementation, not after duplicating frameworks.

### Additional review witnesses
[ ] R03.13 Durable commit barriers and crash windows follow approved fsync policy.
      Tests: journal_durability_crash_windows; evidence: not run; review: pending.
[ ] R04.11 Human error rendering does not implicitly select model context.
      Tests: human_output_is_not_model_context; evidence: not run; review: pending.
[ ] R05.18 Forced stderr-read budget cannot consume the reserved next-request capacity.
      Tests: forced_read_respects_request_reserve; evidence: not run; review: pending.

## 18. Frozen context usage, previews, read limits and durability
agent.context.usage()
  Returns a structured snapshot: context_revision, item_count, rendered_chars,
  estimated_input_tokens, measured_last_input_tokens (nullable),
  model_context_limit, reserved_output_tokens, remaining_input_tokens,
  forced, force_threshold, and estimator/measurement provenance.
  Estimated and measured counts are labeled separately. Unknown values are
  null, never misleading zeroes. Account for source, user messages, summaries,
  selected text, attached images, system instructions and control metadata.
  Include a small usage snapshot in automatic turn metadata, so the model
  can see pressure without reading payloads. The Python API exposes the same
  accounting used by the status bar and force gate.
  Calling usage does not add arbitrary payloads or bypass forced-mode syntax.

read_text(text_or_ref, ..., max_chars=8000)
  The selected range must fit max_chars and the next-request budget. Raise an
  error if not; never truncate it. max_chars must be a positive integer.
  A caller may explicitly request another maximum, within the request budget.
  A range's size is measured in Unicode code points, not encoded byte length.
  A local helper can select a smaller range before calling read_text.
  The agent has no alternate stdout/display/helper-return path for putting
  payloads in context. Raw reads have their own explicit attachment limits.
  Default user-message context cap is also 8000 characters, configurable.
  Unlike an explicit read, a routed user message may be capped with visible
  omission metadata and full H.user ref. These are distinct contracts.

Human preview:
  Show at most 12 lines EACH of source, stdout and stderr per interaction.
  Include exact total lines/characters/bytes as applicable, completeness,
  omitted counts and the correct H.code/H.user/H.stdout/H.stderr reference.
  Never make preview truncation alter history or select payload for the model.
  A noninteractive JSON client receives typed preview fields/events rather
  than terminal-rendered prose. Full payloads remain available through H.
  Error tracebacks follow the same stderr preview rule, not a special unlimited
  display path. Markdown say rendering remains a separate user-message feature.

Durability:
  Flush and fsync operation intent before dispatching side effects. Flush and
  fsync committed completion/context changes before durable acknowledgement.
  Ensure a newly created journal/directory entry is durable on POSIX as well.
  Append-only logical H messages may assemble immutable events; do not rewrite
  an existing source/message to bolt output onto it.
  Batching output chunks is allowed, but a crash leaves incomplete capture
  visibly incomplete. Recovery never equates durable intent with execution
  success. The harness cannot promise rollback or exactly-once remote effects.

Provider/image acceptance:
  Build a concrete provider/auth/model/effort/capability matrix from the pinned
  Pi/Pig and current harness references before claiming compatibility.
  Reuse minimal suitable crates after API/license review; no package chosen yet.
  Required E2E workflows include text invocation, image-input understanding,
  and generated-image output. Invocation order is not a scope restriction.
  No stub/fixture-only image result counts as proof that live generation works.

### Frozen-decision E2E witnesses
[ ] R04.12 Generated code remains in context/H.code; auto-eviction targets outputs only.
      Tests: e2e_code_retention_and_output_eviction; evidence: not run; review: pending.
[ ] R04.13 usage exposes labeled budget/pressure fields consistent with metadata/status/force gate.
      Tests: e2e_context_usage_consistency; evidence: not run; review: pending.
[ ] R04.14 Explicit reads default to 8000 Unicode characters and error above their selected maximum/budget.
      Tests: e2e_read_limits_reject_without_truncation; evidence: not run; review: pending.
[ ] R06.04 Visible direct messages retain full source/output in H.user with cap/ref/count metadata.
      Tests: e2e_direct_message_history_and_cap; evidence: not run; review: pending.
[ ] R10.06 Source/stdout/stderr human previews each show at most 12 lines without history loss.
      Tests: e2e_twelve_line_previews; evidence: not run; review: pending.
[ ] R09.11 Real image-input and image-generation workflows work via the running CLI/agent bridge.
      Tests: e2e_live_image_input_and_generation; evidence: not run; review: pending.

## 19. E2E acceptance strategy (frozen)
Every acceptance test runs the actual built CLI in a temporary isolated HOME
and workspace. Python is the embedded ordinary-CPython worker. Tests may use
Rust test functions as drivers, but do not test private functions in isolation.
All drivers, scenarios, test metadata and embedded fixture servers live here.
Do not write a second standalone Python test/runtime tree.

Deterministic provider fixtures are LOCAL HTTP endpoints used only by tests,
not a client/server architecture for the harness. They record real request
bodies, stream realistic responses/errors and expose cancellation barriers.
Fixture output drives the real outer loop, real worker IPC, journal and UI.
Use explicit synchronization barriers for races rather than fragile sleeps.
Interactive cases run the real CLI under a PTY; JSON cases use real process
stdin/stdout. Assert journal contents AND actual next provider requests to
catch payload leaks, summary duplication and incorrect source retention.

Live acceptance exercises every approved provider/auth/capability combination
with externally supplied test credentials. Never store credentials in this
source or fixtures. If unavailable, report NOT VERIFIED, not passing/skipped
coverage. Keep deterministic E2E tests for reproducibility, but provider/image
compatibility also needs recorded live witnesses before its boxes become [x].

First red-test batch:
1. JSON CLI runs two cells, proving persistent variables and no implicit display.
2. A syntax failure and runtime failure save stderr but send only counts/refs;
   a following sliced read inserts exactly the selected traceback.
3. !/@ remain hidden; !!/@@ produce capped user messages with full H.user.
4. Model-generated source remains in request context unchanged.
5. Collapse keeps one call/summary, no stdout/receipt, merged original ranges;
   explicit forced stderr reads work, arbitrary forced code cannot run.
6. Resume restores H/context/usage, not variables, and executes no old source.
7. PTY previews cap source/stdout/stderr at 12 lines, with usable refs.
8. Ctrl-C/queued input races retain partial capture and cannot dispatch twice.
9. Context usage/read-budget metadata agrees across API/request/status views.
10. Provider text, image-input and image-generation E2E workflows.

Missing CLI behaviors must produce genuine failing assertions. Compiling this
spec printer is only draft validation, never proof of an acceptance requirement.

## 20. Batched old-output eviction and cache stability
Automatic output eviction is batched, never a sliding per-turn window.
Proposed initial policy, matching this harness: when 20 evictable output
payload items accumulate, replace the oldest items as one batch and retain
the most recent 10. Make trigger/retention configurable, with trigger > retention.
These numeric defaults are proposed; batching itself is required.

Eligible items are context payloads explicitly classified as execution/helper
output. A read_text selection is output unless its typed provenance identifies
protected source/user material. Never automatically evict generated source,
user messages, retained collapse calls/summaries, system instructions or raw
attachments under this text-output policy. In particular, read_raw must not be
classified as ordinary evictable text merely because its context item has a
textual `[image H.raw[n]]` label. H is never truncated or rewritten.
Plain selected strings are journaled before insertion, so they also have a
stable H event reference even if their original source provenance is unknown.

Attachment membership is part of committed context state, not an independent
append-only map that outlives its item. Every context replacement computes the
active attachment set from the resulting items. If any future policy replaces
an attachment-bearing item with a text-only omission reference, it must detach
the binary in that same atomic transaction even when the replacement preserves
the old context ID. Resume replays the latest committed membership and must not
reactivate superseded attachment events.

Replace each eligible old payload in place with a stable compact reference:
H reference, selected offsets where known, original size/counts, and omission
label. Preserve context-item IDs and relative ordering. Do not include the
omitted payload again in a notice. Explicit rereads remain possible.
Compute and durably commit the entire batch in one context transaction at a
safe boundary before constructing the next provider request. Never modify an
in-flight request or partially apply a batch. Resume restores the committed
batch exactly; it does not immediately perform another eviction merely on load.

Between batches, existing context items render identically byte-for-byte.
Do not re-render old counts, timestamps, usage snapshots or omission labels
with changing values each turn. New usage/control metadata is appended near
the request suffix, not inserted into the cacheable historical prefix.
New outputs can append without mutating older items. A successful batch still
changes the prefix starting at its first replaced item and may miss provider
cache there; batching reduces invalidation frequency, not guarantees cache hits.
Provider cache usage is observed, not fabricated from this policy.

Pressure forcing remains separate. If a request would exceed its budget,
perform at most one eligible pending eviction batch at that boundary, then
recompute pressure and enter forced-collapse mode if needed. Do not repeatedly
trim one item at a time or delete protected content to avoid forced mode.
Manual collapse/read operations can intentionally change context independently.

E2E tests use a small configured trigger/retention to inspect consecutive
actual provider request bodies: append-only stable history before the trigger;
one atomic replacement batch at the trigger; no progressive per-turn trimming
afterward; correct retained newest items, stable refs/IDs and full H payloads;
protected source/user/summary survival; exact resume state; budget forcing
after insufficient eviction; and byte-identical unaffected rendered items.
Live cache counters may supplement these assertions, but varying provider
cache hits are not a deterministic pass/fail substitute for request inspection.

### Batched-eviction E2E witnesses
[ ] R04.15 Automatic old-output removal commits in batches at safe request boundaries.
      Tests: e2e_output_eviction_batches; evidence: not run; review: pending.
[ ] R04.16 Between batches, historical rendering is stable; replacement refs preserve IDs and H originals, while raw attachment items remain active and ineligible for text eviction.
      Tests: e2e_output_eviction_cache_prefix_stability, e2e_output_eviction_does_not_reclassify_or_detach_raw; evidence: not run; review: pending.
[ ] R05.19 Insufficient batched eviction enters forced mode rather than incremental protected-content trimming.
      Tests: e2e_eviction_then_forced_collapse; evidence: not run; review: pending.

## 21. say is UI-only (confirmed)
agent.say(text) returns None and emits a dedicated user-interface event.
It never writes to the execution's stdout/stderr, and never inserts a second
copy of its text into model context. The model already sees the source call.
Terminal UI renders the event as Markdown; JSON UI emits a typed say event.
Persist it in H.say, linked to the execution. A source expression computing
text does not authorize automatic insertion of its evaluated result.
E2E assertions check empty execution streams, a user-visible say event, and
no duplicate context payload.
[ ] R10.07 say emits a UI-only event without stdout/stderr or duplicate context insertion.
      Tests: e2e_say_is_ui_only; evidence: not run; review: pending.

## 22. Uniform no-duplication rule (confirmed)
Retained source already shows helper calls and literal arguments. Do not echo
those arguments into automatic context observations. Evaluated helper results,
stdout/stderr, image bytes and diagnostics enter H, not automatic model context.
Automatic observations carry status, counts, usage and stable references only.
Only explicit context reads select new result payloads. UI events do not select
payloads. Preserve the intentional user-routing/collapse/source exceptions.
E2E tests inspect actual provider requests for duplicate helper text and result
leakage across say, shell, inner LLM, image generation and failures.
[ ] R04.17 Helper observations never duplicate call text or auto-include result payloads.
      Tests: e2e_helpers_no_duplicate_payloads; evidence: not run; review: pending.

## 23. Input protocol implementation contract
JSON input_prompt events identify prompt_id, operation and worker_generation.
A stdin_reply must echo all three and have its own unique command ID. Its action
is text (default, with string value), eof or cancel. Unmatched, duplicate and
late replies are rejected, never held for a later prompt. Replies emit accepted
only after linkage, type and ID validation; rejected replies emit no acceptance
or completion. EOF raises EOFError;
cancel/interrupt raises KeyboardInterrupt and commits cancellation of the owning
cell even if Python catches it. No synthetic H.stdin text is made for EOF/cancel.
Prompts, accepted text replies and closures are durable, linked events. H.stdin
restores exact text/indexes on resume. Waiting for input must still drain output
and service Ctrl-C, including partial terminal lines released by Ctrl-D. Ctrl-D
with no buffered text closes that prompt as EOF; partial input is never a reply
until newline/EOF, and is discarded on cancellation. Once cancellation commits,
new helper RPCs from that cell raise KeyboardInterrupt without dispatching new
helper side effects; caught exceptions cannot suspend cancellation escalation.
This is lifecycle cancellation, not rollback or a restriction on ordinary Python
side effects. Worker-generation metadata increases across reset and resume.
Malformed reply action/value types are rejected; only an omitted action defaults
to text. Imported builtins.input uses the same bridge; worker FD 0
is /dev/null, never the harness JSON command pipe (raw stdin reads see EOF).
This defines the initial machine reply spelling; Python getpass is NOT bridged
or verified by this slice. Hidden CLI API-key entry is separately covered below.
Scoped input audit (not approval of the entire replacement): every row below
has a real CLI witness, with event/journal/namespace assertions reviewed. At this
milestone: 51 E2E tests passed, 0 ignored; input PTY passed 10/10 repeats.
Command: cargo build --manifest-path transition/Cargo.toml &&
  PY_HARNESS_BIN="$PWD/transition/target/debug/py" cargo test
  --manifest-path transition/Cargo.toml -- --test-threads=1
Run log: /tmp/py-transition-e2e-input.log (local witness, not a permanent fixture).
Initial red evidence: missing prompt IDs, EOF/cancel timeouts, blocked PTY after
partial Ctrl-D, post-cancel shell hang, malformed-action acceptance, resume
returning generation 1 rather than 3, and acceptance before rejecting late reply.
These failures preceded their respective runtime fixes. Additional crash/FD-0
and escalation witnesses validate existing/shared paths; not claimed as new reds.
All earlier R01-R13 family boxes remain unverified; this audit covers only input.

[x] R11.07 JSON replies must match current prompt/operation/generation; stale/missing links reject without closing the prompt or emitting acceptance/completion.
      Tests: e2e_input_correlation_and_duplicate_replies; evidence: pass; review: missing/wrong links and late/idle replies rejected; correct reply still usable.
[x] R11.10 Reply command IDs dispatch at most once, including across resume.
      Tests: e2e_input_correlation_and_duplicate_replies, e2e_input_generation_and_reply_ids_survive_resume;
      evidence: pass; review: duplicate current/new-prompt/reloaded IDs rejected, H.stdin unchanged.
[x] R11.11 Reply actions/value types validate strictly; omitted action defaults to text.
      Tests: e2e_input_correlation_and_duplicate_replies; evidence: pass; review: boolean/unknown action, missing/numeric value reject; omitted action accepts Unicode text.
[x] R11.08 Interactive input remains responsive through partial lines, EOF and Ctrl-C.
      Tests: e2e_interactive_input_partial_eof_and_ctrl_c; evidence: pass, 10/10 repeats;
      review: actual PTY keystrokes, partial Ctrl-D then newline, partial Ctrl-D then Ctrl-C, standalone Ctrl-D and subsequent Python/shell use.
[x] R07.11 Committed input cancellation rejects subsequent helper dispatch.
      Tests: e2e_input_cancel_does_not_start_blocking_helpers; evidence: pass;
      review: shell/LLM/image/say/text/raw/reset/stop raise KeyboardInterrupt; no helper intents/payloads/reset; empty stderr and explicit caught-exception stdout.
[x] R07.12 Input cancel/interrupt raises KeyboardInterrupt; owning cell remains cancelled even if caught; later input does not reopen.
      Tests: e2e_input_cancel_is_sticky_and_ipc_survives, e2e_input_wait_drains_output_and_sigint;
      evidence: pass; review: JSON cancel/interrupt and SIGINT, one cancellation event/prompt, partial capture and subsequent RPC usable.
[x] R07.13 Input cancellation escalates when Python catches it and spins; fresh worker replaces it, preserving history/context.
      Tests: e2e_input_cancel_escalates_if_python_ignores_it; evidence: pass;
      review: bounded completion, cancelled/incomplete stream flags, generation change and successful next cell using H/context.
[x] R11.09 Explicit and command-pipe EOF raise EOFError without synthetic input text.
      Tests: e2e_input_eof_and_resume; evidence: pass; review: caught/uncaught EOF, continued worker use and channel EOF; exact H.stdin excludes EOF.
[x] R03.14 Prompt/text/closure events commit in order and preserve linkage.
      Tests: e2e_input_correlation_and_duplicate_replies, e2e_input_wait_drains_output_and_sigint;
      evidence: pass; review: prompt < acceptance < indexed text < single closure; capture committed while prompt is unanswered.
[x] R03.16 H.stdin indexes and exact empty/Unicode text restore into fresh Python.
      Tests: e2e_input_eof_and_resume; evidence: pass; review: two text replies reload exactly; EOF contributes no synthetic entries.
[x] R08.05 Worker generations increase across repeated resets/resumes.
      Tests: e2e_input_generation_and_reply_ids_survive_resume; evidence: pass; review: generation 2 -> 3 -> 4, unique prompts and stale-generation rejection.
[x] R02.09 Imported builtins.input uses the same correlated bridge.
      Tests: e2e_input_correlation_and_duplicate_replies, e2e_interactive_input_partial_eof_and_ctrl_c;
      evidence: pass; review: imported input succeeds over JSON and PTY, never reads command FD.
[x] R02.10 Raw worker FD/stdin/subprocess reads cannot consume harness commands.
      Tests: e2e_worker_stdin_is_not_command_stdin; evidence: pass; review: os.read/sys.stdin/subprocess see EOF and next JSON command executes.
[x] R03.15 Crash at an unanswered prompt records unknown outcome; resume never reopens/replays it.
      Tests: e2e_crash_at_input_does_not_reopen_prompt; evidence: pass; review: unknown/replay_allowed=false, no closure/reply fabrication, empty H.stdin, one original source/prompt and continued execution.

## 24. Interactive polish (user-approved direction; witnesses audited below)
Keep orthogonal parts as direct functions/state, not a UI/provider framework.
[x] R10.08 Explicit Tab fuzzy-matches slash commands, their arguments/models/effort,
      paths, shell executables after !/!!, and safe Python names after @/@@.
      Rank exact/prefix and contiguous matches before sparse subsequences;
      deterministic results, case-insensitive matching, bounded directory/PATH scans.
      Completion never evaluates Python properties or runs shell commands.
[x] R10.09 Python completion snapshots safe namespace/module dictionaries only
      between cells; no editor inspection IPC during execution. Multiline/paste
      compiles a complete ordinary cell; no notebook syntax or implicit display.
[x] R10.10 say renders Markdown headings/lists/quotes/code/tables in terminal
      columns. Wrap text by word, split overlong words only when unavoidable;
      count each split as an extra line in table-layout optimization. Allocate
      table widths using the rendered-height/forced-split objective: bounded
      exact search for small tables, deterministic sampled heuristic for large
      tables, not a global optimality claim. Very narrow tables may stack cells.
      Unicode widths/combining marks must fit; terminal controls in payloads
      cannot inject terminal commands. JSON say text remains byte-for-byte text.
[x] R10.11 Human cell previews have clear operation/source/stdout/stderr boundaries,
      status and refs/counts, width-aware word wrapping. Preview/history limits
      remain independent: rendering never modifies H or model context.
[x] R10.12 /model list [query] and /models [query] list matching cached,
      built-in/configured models, refreshing stale supported catalogs per R16;
      /model <id-or-query> resolves an exact ID or unique fuzzy match, rejects
      ambiguity with choices. /effort (/think) validates supported level spellings.
      /login lists available methods/providers rather than silently selecting one;
      /logout, /auth and model/effort/session/config controls have useful help.
      Unsupported OAuth/provider capabilities are explicit, not false parity.
[x] R07.24 Validated effort is sent for supported reasoning models using each
      provider's wire fields (Responses/Chat/Anthropic/Gemini), not merely shown
      in local status. Unsupported or non-reasoning models omit reasoning fields.
[x] R06.24 Terminal API-key entry is hidden, cancellable, never stored in editor
      history, and never silently falls back to echoed input without a terminal.
[x] R07.25 Codex subscription uses Pig-pinned device flow, private locked/merged
      credentials, refresh-before-invoke, Codex-specific SSE terminal validation,
      actual effort and own py client identity. Device remains available; browser
      OAuth is specified and witnessed separately in section 27.
[x] R09.24 /new and /resume <path-or-fuzzy-session> operate between idle cells,
      preserve full old journals, start fresh Python, never replay execution;
      model/effort session changes survive resume without changing defaults.
[x] R10.13 --help/--version are side-effect-free; startup --model/--effort,
      --session/--resume, --no-model, --json/--json-input/--spec are validated.
      Unknown/missing arguments fail before creating a session. Empty editor
      submissions never start a model turn; execution/source routing is unchanged.
## 25. Review-discovered boundary regressions
[x] R05.26 JSON command errors carry the rejected/accepted command ID and permit
      later commands; a malformed auth command cannot expose its key in history.
[x] R08.25 Complete helper exchanges defer SIGINT until their response is read;
      obsolete response frames cannot become subsequent execution commands.
      Pre-start worker failure commits partial/incomplete streams
      and resets without recapturing previous output or replaying code.
[x] R10.25 Table cost preprocessing as well as width-search has bounded work;
      128x4 long-cell tables permit the next operation within a 3-second fixture
      deadline. ZWJ does not collapse the width of arbitrary ASCII neighbors.
[x] R08.26 Ctrl-C/external SIGINT while the editor or JSON CLI is idle cannot
      cancel the next cell; clear idle signal state before dispatch, not during
      active work. Idle JSON interrupt acknowledges no active work, keeps Python.
[x] R06.25 Sessions reload privately locked credential state before provider/image
      invocation and offline auth/model inventory; another session's logout or
      key replacement takes effect without restarting Python or replaying work.
      Invalid Codex effort fails before token refresh/network/provider intent.
Scoped witness audit (pass, assertions reviewed; original families stay unchecked):
- R10.08: completion_e2e e2e_tab_slash_commands_and_model_effort_arguments,
  e2e_tab_fuzzy_python_namespace_and_modules, e2e_tab_never_invokes_python_properties_or_dir,
  e2e_tab_shell_executables_and_unicode_spaced_files,
  e2e_tab_quoted_shell_paths_and_directory_continuation,
  e2e_tab_absolute_tilde_python_cwd_and_visibility. Actual Tab keys and sources.
- R10.09: e2e_multiline_continuation_and_bracketed_paste (actual PTY).
- R10.10: rendering_e2e e2e_say_markdown_wraps_by_word_and_keeps_original,
  e2e_say_table_optimizes_height_and_wraps_columns,
  e2e_say_table_long_word_breaks_are_not_free,
  e2e_say_narrow_table_unicode_and_controls, e2e_markdown_real_pty_width_and_unicode.
- R10.11: e2e_cell_preview_boundaries_wrap_without_changing_history,
  e2e_preview_controls_are_escaped_but_history_is_exact,
  ux_e2e e2e_help_cell_shell_status_and_word_wrapped_errors.
- R10.12: ux_e2e e2e_model_list_fuzzy_selection_and_ambiguity,
  e2e_effort_validation_command_errors_do_not_exit,
  e2e_login_inventory_and_unknown_provider_are_nonblocking;
  flags_e2e e2e_tab_effort_capabilities_and_unlisted_login_provider.
- R07.24: effort_e2e e2e_effort_responses_reasoning_metadata_and_clamping,
  e2e_effort_chat_compatibility_and_binary_thinking,
  e2e_effort_anthropic_adaptive_and_budget_reservation,
  e2e_effort_google_budget_levels_and_off,
  e2e_effort_unknown_and_nonreasoning_omit_wire_fields,
  e2e_effort_override_is_call_local_and_invalid_never_sends,
  e2e_effort_invalid_options_restore_default_and_caps_stay_valid,
  e2e_effort_image_transform_stays_intact. Fields/budgets validated on wire.
  off means omit defaults where disabling is not proven; no universal disable claim.
- R06.24: secret_e2e e2e_secret_api_key_unicode_hidden_private_and_not_journaled,
  e2e_secret_ctrl_c_and_eof_restore_terminal_without_login,
  e2e_secret_sigint_while_polling_is_cancellable,
  e2e_secret_no_tty_refuses_instead_of_echo_fallback.
- R07.25: codex_e2e e2e_codex_device_login_pending_and_protocol_sse,
  e2e_codex_device_denied_expired_and_slowdown,
  e2e_codex_device_pending_404_and_json_error_code,
  e2e_codex_inventory_is_offline_and_initial_device_fields_validate,
  e2e_codex_refresh_once_then_offline_model_inventory,
  e2e_codex_parallel_refresh_is_serialized,
  e2e_auth_two_live_sessions_merge_updates,
  e2e_codex_invalid_login_and_failed_refresh_preserve_credentials,
  e2e_codex_cancel_during_device_wait_and_network,
  e2e_codex_sse_deltas_crlf_split_and_image_input,
  e2e_codex_terminal_failures_and_truncated_sse_never_succeed,
  e2e_codex_http_failures_hide_bodies_and_never_retry,
  e2e_codex_outer_loop_executes_source_from_completed_sse. Local protocol fixtures.
- R09.24: ux_e2e e2e_session_model_effort_resume_and_new_never_replay;
  flags_e2e e2e_resume_alias_overrides_settings_without_replay.
- R10.13: flags_e2e e2e_help_version_spec_never_create_home,
  e2e_bad_flags_and_values_fail_before_session_creation,
  e2e_json_startup_fuzzy_model_effort_and_configured_exact,
  e2e_empty_editor_lines_never_start_a_model_request. ready follows startup overrides.
- R05.26: boundary_e2e e2e_json_command_errors_are_correlated_without_auth_leaks.
- R08.25: rpc_e2e e2e_rpc_input_reply_sigint_window_and_repeated_interrupts,
  e2e_rpc_stale_reply_is_discarded_between_execution_commands,
  e2e_rpc_stale_reply_is_not_a_helper_result_or_command,
  e2e_worker_dead_before_send_does_not_recapture_previous_output,
  e2e_worker_pre_started_failure_captures_partial_and_resets.
- R10.25: responsiveness_e2e e2e_large_repeated_table_is_responsive_and_history_exact,
  e2e_large_unique_table_is_responsive_and_history_exact,
  e2e_large_mixed_cluster_table_is_responsive,
  e2e_invalid_zwj_ascii_wraps_but_emoji_joins_stay_together; supplementary
  prepared_table_costs_match_rendered_wrap checks the optimized scoring objective.
  Measured debug fixtures ~0.38–0.48s including CLI startup/next operation;
  objective budget 2M sampled-cell probes, preprocessing 16K sampled characters.
- R08.26: boundary_e2e e2e_idle_sigint_never_cancels_next_cell,
  e2e_json_idle_interrupt_preserves_namespace.
- R06.25: boundary_e2e e2e_two_sessions_reload_api_auth_and_logout_before_network
  (API and image headers, auth/model inventory, namespace retained),
  e2e_invalid_codex_effort_never_refreshes.
Meaningful reds: literal/non-working fuzzy Tab; separate cells on multiline paste;
property/dir inspection; unwrapped Markdown/table rows and injected controls;
invalid command ends editor; unknown model/effort silently accepted; /login chooses
OpenAI without asking; session settings lost/resume command absent; exposed no-tty
getpass fallback; device/API mismatch and no SSE; effort absent on wire; flags
ignored; stale initial ready metadata; input reply/SIGINT corrupts next RPC/cell;
large-table preprocessing exceeds 3s; ASCII ZWJ undercounts columns; idle SIGINT
cancels future work; credential replacement/logout invisible to other sessions.
Local logs: /tmp/py-{ux,flags,ready,boundary,json-idle,auth-inventory}-red.log,
/tmp/py-{rpc,render-responsive,render-zwj}-red.log and scoped fork logs.
Final validation command is the same build + PY_HARNESS_BIN cargo test above;
117 tests pass, 0 ignored (116 actual-CLI witnesses, 1 arithmetic equivalence test).
Latest full logs: /tmp/py-polish-final.log (debug), /tmp/py-polish-release.log (release).
Release suite also passed 5/5 full repeated runs with deliberately poisoned
fixture-only credential/config environment; /tmp/py-polish-repeat-{1..5}.log.
Clippy all targets with -D warnings passed; /tmp/py-clippy-final.log.
The test_command factory removes inherited credential/home/model/Codex overrides;
fixture-owned values are explicit. env_e2e e2e_test_fixture_environment_is_sanitized
checks removal metadata and actual CLI/Python absence, without reading key values.
Live credentials were not inspected/imported.
At this historical milestone browser PKCE, Anthropic/Google OAuth and unsupported
native APIs were unverified or unimplemented. Section 27 supersedes the browser
and terminal-style exclusions for its witnessed paths. Live provider parity
still needs live smoke tests. Markdown remains a compact subset, not CommonMark;
Unicode joining is conservative, not exhaustive terminal shaping. No persistent
fullscreen application, extension framework, notebook semantics or automatic
context compaction added.

## 27. Browser auth, selection, and semantic terminal presentation (new scope)
[x] R06.27 /login anthropic and /login codex support PKCE browser OAuth using
      explicitly pinned provider endpoints/scopes: Codex loopback callbacks and
      Anthropic's hosted code#state callback, with state validation and hidden
      manual paste fallback. Codex device flow remains an
      explicit method. Cryptographic verifier/state come from OS randomness;
      authorize URLs may be shown but codes/tokens never reach H/editor journals.
      Cancel/timeout/bad callback leave existing credentials and terminal intact.
[x] R07.27 Anthropic OAuth refreshes privately before invoke and uses Bearer and
      required protocol beta headers; API-key auth remains supported explicitly.
      Provider-specific system/header compatibility must be documented, not
      silently advertised as live-tested. Browser opener failure permits manual
      opening/callback paste. Credential writes merge under the existing lock.
[x] R10.27 Tab opens a live fuzzy picker across completions: typing refilters,
      up/down and Tab cycle, Enter selects, Escape/Ctrl-C close without submitting
      or changing the original input. All command/argument/Python/file candidates
      remain safe and bounded. No-match queries may be edited to recover. Single
      candidate insertion remains quick. JSON/non-TTY output has no UI escapes.
[x] R10.28 Interactive presentation has semantic idle/thinking/running/input/login
      states, minimal prompt, and a compact width-safe status line, with the model
      shown during thinking (not repeated in every prompt). Color/styles enhance
      headings, boundaries/status and Markdown without leaking payload controls.
      NO_COLOR and non-TTY output disable generated styling; JSON remains exact.
      State transitions cover success, exceptions, cancellation, worker reset and
      provider failure; terminal state/cursor always restore on close/error.
[x] R10.29 Shift+Enter inserts a newline (never submits) using supported Kitty
      CSI-u/xterm modified-key reports; Ctrl-J is a fallback for terminals that
      cannot distinguish it. Continuation lines align with source text after the
      minimal > prompt. Do not strip leading source whitespace or invent indent.
[x] R10.30 Each Python/shell execution has a monotonic session cell number and
      source/operation/history refs, including no-output/error/interrupt cells;
      resume retains numbering, new session restarts it, nested cells retain
      outer attribution. End-of-cell status gives result and elapsed time.
[x] R06.28 Codex uses the upstream ChatGPT login page; social sign-in buttons
      (e.g. Google) are provider-managed, never separate Google credentials in py.
      Do not invent undocumented identity-provider OAuth parameters or claim the
      page's Google button can be forced. Browser selection can use a user-owned
      executable wrapper without changing PKCE/state/callback semantics.
Scoped witness audit (reviewed assertions, not test-name coverage alone):
- R06.27: browser_auth_e2e e2e_codex_browser_pkce_callback_validation_and_private_merge,
  e2e_browser_opener_failure_still_allows_callback_and_login_fields_redact,
  e2e_codex_browser_failure_timeout_cancel_never_overwrite,
  e2e_browser_manual_timeout_bind_fallback_and_exchange_cancel,
  e2e_anthropic_browser_manual_pkce_state_and_hidden_secret,
  e2e_browser_manual_bad_state_cancel_and_codex_redirect_hidden.
  browser_boundary_e2e e2e_browser_commit_lock_inherits_timeout_and_cancel and
  e2e_browser_wrong_path_denial_does_not_consume_valid_callback cover commit locks
  and wrong-route denials. Timeout includes the final credential lock; successful
  atomic write is the commit boundary, not a promise to roll back afterward.
- R07.27: browser_auth_e2e e2e_anthropic_failed_refresh_and_parallel_rotation_preserve_store,
  e2e_anthropic_refresh_bearer_protocol_and_api_key_unchanged; browser_boundary_e2e
  e2e_anthropic_interrupt_requires_unique_nonempty_command_id and
  e2e_codex_malformed_refresh_and_loaded_tokens_leave_store_unchanged.
  Explicit wire exception: Anthropic subscription OAuth prepends exactly
  "You are Claude Code, Anthropic's official CLI for Claude." before the untouched
  original system instructions, and sends the pinned Claude Code compatibility
  beta/client headers plus py's own client marker. It does not rename py's UI,
  change protected context or add tools. API-key requests do not get this marker.
  This pin-specific compatibility is shown before login; no live parity claim.
- R10.27: picker_e2e seven actual-PTY witnesses exercise commands, nonfirst model
  choices, effort/provider arguments, safe Python symbols, PATH executables,
  quoted Unicode filenames, no-match/edit/cancel/narrow/restore/non-TTY cases.
  picker_boundary_e2e e2e_picker_bracketed_paste_never_selects_or_submits,
  e2e_picker_pasted_query_requires_separate_physical_selection_and_submit,
  e2e_picker_oversized_paste_is_discarded_and_recoverable and
  e2e_picker_login_methods_resolve_fuzzy_provider prove nested paste is atomic,
  bounded, never executes pasted Enter, and fuzzy-dependent arguments agree with
  execution. Styled selection is in styled_e2e minimal_prompt witness below.
- R10.28: ui_state_e2e eight actual-CLI/PTY witnesses plus styled_e2e
  e2e_styled_markdown_cells_status_and_plain_fallback,
  e2e_styled_minimal_prompt_and_aligned_continuation,
  e2e_styled_thinking_shows_model_only_in_thinking_status,
  e2e_json_output_tty_editor_and_slash_commands_never_mix_ui,
  e2e_json_tty_input_without_ui_terminal_has_no_generated_escapes.
  Styles are applied after Unicode column layout/control escaping; NO_COLOR
  presence (even empty), TERM=dumb and non-TTY disable SGR. Status is a compact
  transition line, not an always-pinned fullscreen footer. JSON-mode editor UI
  writes to stderr only, sizes from its actual terminal, and is silent if stderr
  is not a terminal; stdout remains JSONL even for slash commands and tty input.
- R10.29: editor_e2e e2e_editor_shift_enter_kitty_and_xterm_one_cell,
  e2e_editor_shift_enter_plain_text_and_reports_never_leak,
  e2e_editor_extended_printable_keys_ctrl_j_and_release; newline_e2e three
  Ctrl-J witnesses and editor_e2e six additional editing/undo/history/paste/
  word-wrap/resize/responsiveness/restoration witnesses. Supported modified Enter
  includes CSI-u, xterm modifyOtherKeys and legacy modified Enter; ordinary Enter
  accepts a complete cell, Shift+Enter/Ctrl-J inserts one literal newline. Raw
  editor prompts have two columns and continuation indentation is exactly two.
- R10.30: ui_state_e2e e2e_state_numbered_empty_error_and_nested_shell_cells,
  e2e_state_resume_numbering_without_replay_and_new_session,
  e2e_state_worker_crash_and_prestart_shell_error_get_cell_end,
  e2e_state_human_cell_end_status_refs_and_wrap; remaining four state witnesses
  cover input, reset, HTTP/login failure and sticky caught cancellation. Cell
  start/end are durable; state notifications are not added to H/model context.
  Shell nesting restores outer attribution. Preview no longer duplicates the
  legacy operation-ID cell header; operation/source refs remain in numbered start.
- R06.28: browser PKCE fixtures assert originator/client/state/challenge/callback;
  opener-failure witness proves browser wrapper selection does not alter OAuth.
  Official https://developers.openai.com/codex/auth documents Google/social login
  as OpenAI account sign-in; upstream codex-rs/login/src/server.rs authorize URL
  has no documented Google/login_hint selector. No Google-password/key handling
  or invented force-Google URL option is provided. Page/browser UX is external.
- Private-login regression: styled_e2e
  e2e_login_unknown_method_never_echoes_or_journals_accidental_secret. Unsupported
  method strings are not echoed or recorded, including queued auth commands;
  only known method spellings plus id/kind/provider are retained. API keys,
  callback codes and extra login fields stay out of editor history/journals.
  env_e2e now sanitizes all new OAuth/browser/Anthropic fixture overrides too.
Meaningful reds: browser first five 0/5 /tmp/py-browser-auth-red.log; picker
six failures /tmp/py-picker-red.log; modified Enter failed six before raw editor
/tmp/py-editor-red.log; semantic states first five 0/5 /tmp/py-ui-state-red.log;
auth boundary 0/4 /tmp/py-browser-boundary-red.log; nested-picker-paste 0/3
/tmp/py-picker-boundary-red.log; styles/strict JSON tty 0/4 /tmp/py-styled-red.log;
unknown-method secrecy /tmp/py-login-method-red.log; JSON tty output-fd sizing
/tmp/py-json-tty-width-red.log. Renderer width tests explicitly use NO_COLOR when
asserting no generated controls; styled tests strip only generated SGR and keep
control-injection assertions. Stream witnesses concatenate stdout chunks rather
than assuming print's payload and newline arrive in a single pipe read.
The previous 117-test milestone is historical. Current section27 validation
logs: /tmp/py-web-ui-full.log (debug), /tmp/py-web-ui-release.log (release),
/tmp/py-web-ui-clippy.log. 166 tests pass in both builds, 0 failed/ignored;
165 are actual-CLI/PTY witnesses, one checks bounded-table cost equivalence.
Clippy all targets with -D warnings passes. Full release suite additionally
passes 3/3 repeated runs with poisoned fixture-only credentials and OAuth
endpoint/browser overrides: /tmp/py-web-ui-repeat-{1..3}.log. Browser-manual
state/cancel witness passes 20/20 repeats: /tmp/py-browser-url-repeat-{1..20}.log.
Its asynchronous fake-opener fixture now appends URL records and waits for the
exact URL displayed by the current flow; it cannot pick a stale preceding flow's
state or depend on child-process completion order. Existing state validation
correctly rejected the stale URL before this fixture-readiness fix.
No credentials were inspected or imported; protocol tests use only explicit
local fixture credentials/endpoints.
This supersedes browser-OAuth/no-style exclusions only for the witnessed paths.
No live subscription login/invocation, Google/Gemini browser OAuth, exhaustive
terminal/grapheme/vendor proof, configurable theme framework, reverse-search or
external editor is claimed. Full R01-R13 migration acceptance remains unchecked.

## 28. Inline completion menus (current user request)
[x] R10.31 Ambiguous Tab completion renders a compact live fuzzy menu immediately
      below the complete editable prompt, on the normal terminal screen. Keep
      input and preceding output visible; never switch to the alternate screen
      or clear the whole display for completion. Filtering/navigation/selection,
      paste bounds and cancellation semantics are unchanged. Allocate bounded
      menu rows; scrolling to reserve space at the bottom must preserve input.
      Selection/cancel/error clears only the temporary menu and restores the
      editable source/caret. Multiline and middle-of-input completion, narrow
      terminals and resize must remain usable. JSON stdout stays machine-only;
      JSON interactive menus use the existing UI terminal channel. Tiny screens
      may decline an ambiguous menu when input plus choices cannot fit.
Actual CLI/PTY screen-level witnesses (inline_picker_e2e):
- e2e_inline_prompt_output_and_selection_cancel_coexist: recent output, source
  prompt and filtered choices coexist in the normal screen; Enter chooses but
  does not submit, cancellation clears the menu without changing the input.
- e2e_inline_multiline_middle_caret_select_and_cancel: menu follows ALL visible
  source lines, not the caret's line; both selected and cancelled completion
  restore the exact middle-line caret and preserve closing syntax/suffixes.
- e2e_inline_bottom_narrow_resize_and_repeated_close: reserve rows by natural
  scrolling, clip to the UI terminal width, preserve the input across resize,
  repeated Ctrl-C and subsequent execution. No alternate-screen/full-clear codes.
- e2e_inline_height_shrink_closes_without_changing_source: a height shrink that
  cannot hold the reserved menu cancels it, restores source/caret, and leaves
  editing/submission usable. Width changes redraw clipped choices. Terminal
  reflow is vendor-controlled; exhaustive reflow/terminal proof is not claimed.
- e2e_inline_tiny_terminal_declines_without_losing_input: two-row terminal keeps
  the prompt instead of opening an unusable menu or switching screen buffers.
- e2e_inline_json_menu_uses_ui_terminal_not_stdout: inline menu uses stderr's
  terminal while stdout remains JSONL, including the selected /models command.
Tests include a small test-only VT screen model to check actual visible ordering,
context retention and caret position, rather than merely matching menu labels.
Post-restart baseline witness: the five initial tests all fail against the old
release (alternate screen/input lost/JSON picker absent), 0/5 in
/tmp/py-inline-baseline-red.log. New scope passes in /tmp/py-inline-edge.log.
Existing CLI/PTY drivers now wait for menu-region cleanup/repaint instead of the
old alternate-buffer exit; original source, selection, paste and control-safety
assertions remain. Menu rows are bounded by eight and half the screen, leaving
space for source and recent output where possible; preceding output scrolls
naturally, never gets intentionally erased. Selection filtering is still bounded.
Final validation: 172 tests pass in debug and release, 0 failed/ignored;
171 actual-CLI/PTY witnesses and one bounded-table equivalence test. Inline
scope passes 50/50 repeated six-test runs (300 CLI/PTY trials). Screen snapshots
wait for completed menu/editor repaint frames, not just disappearance of labels
or receipt of an earlier cursor marker; caret/source assertions are unchanged.
Logs: /tmp/py-inline-full.log, /tmp/py-inline-release.log,
/tmp/py-inline-repeat-{1..50}.log; Clippy all targets with -D warnings passes in
/tmp/py-inline-clippy.log. This row supersedes only the alternate-screen picker;
all section27 browser, privacy, semantic-state and source-routing rules remain.

## 29. Background jobs and one-shot wakeups (approved scope)
### Ownership, execution and history
Background jobs are session-owned and managed only while this harness process
is alive. They are not a daemon, sandbox, persistent Python cells or parallel
model subcalls. Keep one foreground execution pipeline. Initial maximum is
four live jobs per session; reject excess starts before dispatch. A task has a
stable session-scoped ID, kind, optional name, state, source/intent reference,
start/end times, exit/signal metadata, timeout and stdout/stderr references.
States distinguish starting/running, succeeded/failed, cancelling/cancelled,
killed/timed_out and outcome_unknown; never report cancellation as success.

run validates literal option types before side effects. source is a string;
kind is shell/python; cwd is an optional directory path; env is an optional
string-to-string mapping merged into the inherited environment; timeout is an
optional finite positive number, not bool. name is optional bounded text.
Shell jobs use the same shell/cwd conventions as agent.sh. Python jobs use the
configured Python executable in unbuffered mode with fresh globals, without
agent/H bridge or main-worker heap sharing. Process isolation is lifecycle
management, not security: jobs share filesystem/network/user permissions.
Each job has a separate process group, stdin /dev/null, and private output
capture. Reserve stable H.stdout/H.stderr entries at creation, including empty
streams. Counts/snapshots may grow while live; stored byte chunks are immutable.
Final completion/completeness distinguishes drained final capture from partial
capture after failure/crash. No automatic stdout/stderr/exception/image payload
selection into model context, including completion notifications and logs UI.

Rust remains the single journal writer. Commit task intent before spawn,
record task linkage and stream references durably, then commit terminal state
once after output drain/child reap. Spawn failure is inspectable failure, not a
running orphan or consumed concurrency slot. Concurrent capture must preserve
separate task/stream attribution and global observed-event ordering; background
activity must not overwrite foreground active-cell/UI state. Bound per-cycle
capture work so continuous output cannot starve controls, input or deadlines.
Disk persistence failure prevents new starts and triggers best-effort owned
child cleanup; never assert complete capture or rollback of external effects.

kill sends TERM then escalates to KILL after a bounded grace period; force
sends KILL immediately. Timeout uses the same bounded termination machinery but
retains timed_out status. Repeated cancellation is idempotent. Descendants that
escape process groups are not guaranteed to die. Foreground Ctrl-C retains its
existing priority; killing an unrelated background job requires its task ID.

### Shared service pump and controls
Use a host-owned bounded service pump for task output, child status, deadlines,
queued completion notices and wakeups. It runs while idle and during worker
IPC, foreground shell, provider HTTP/streaming, input-prompt and editor waits.
Do not call run_agent recursively from a helper/poll checkpoint. Dispatch an
outer continuation only at a safe boundary, never alongside an existing outer
request or unsettled foreground cell. Task management remains responsive while
foreground execution is busy; it must not simply queue behind a blocked cell.
Editor servicing preserves partially typed multiline source, cursor and draft
without submitting it or losing terminal modes. Asynchronous notices coordinate
with redraw; JSON stdout remains machine-only. Both idle input modes must
service timers without waiting for another submitted command.

Python API is in section 5. User-local slash commands are:
  /tasks [running|finished|all]
  /task <id>
  /task logs <id> [stdout|stderr|both]
  /task kill <id> [--force]
  /bg shell <command>
  /bg python <source>
  /wakeups
  /wakeup cancel <id>
  /wakeup run <id>
Tasks list/inspection reports metadata; logs uses bounded human previews and
full H references, never selects output into context. Background launches from
local /bg controls are hidden interactions by default and never initiate model
activity merely because they finish. Completion/help include IDs, states and
valid subcommands. Versioned JSON input exposes equivalent task launch/list/
inspect/log/kill and wakeup list/cancel/run controls with command ID validation,
acceptance, deduplication and completion/error linkage. Helper and slash/JSON
controls share one task registry, lifecycle rules and validation. JSON kinds:
  bg_run {source, task_kind="shell"|"python", options={...}}
  task_list {state?}; task_get/task_logs/task_kill {task_id, stream?/force?}
  wakeup_list; wakeup_cancel/wakeup_run {wakeup_id}
  new/resume {cancel_tasks?, session?}; quit {cancel_tasks?}
Every command also carries a unique string id. Immediate task/wakeup controls
are available over JSON during foreground waits; slash controls run at the
interactive command boundary (a Python input() prompt retains ownership of tty
input). Logs remain bounded user-only previews with full H references.

### Wakeup scheduling, dispatch and stop settlement
agent.loop.stop(wakeup=(seconds, reason)) stages an optional one-shot timer.
seconds must be a finite positive non-bool number at most 604800 (7 days);
reason is nonempty bounded text (at most 512 characters, without controls). Invalid options reject without
replacing an existing valid staged request. Repeated valid stop calls replace
that cell's staged request. Following statements run normally. Exceptions,
worker failure and committed cancellation discard staged control effects;
steering wins over stop and also discards that stop's timer. Only a successful
cell and actual turn stop commits/arms the timer, atomically with stop settlement.
The due time is measured from that settlement, not the helper invocation.
A plain stop does not cancel unrelated previously committed wakeups.

run(..., wakeup_reason=reason) explicitly opts into one continuation on terminal
task completion, including failure/timeout/cancellation, with bounded nonempty
reason. Default None is notification-only and does not wake the outer model.
Completion wakeup creation and task completion commit atomically. A wakeup has a
stable ID, originating operation/task, reason, due time and durable lifecycle
(scheduled/pending/dispatching/consumed/cancelled or outcome_unknown). Its reason
is an intentional bounded control-metadata selection; task outputs stay in H.
Continuation context contains wakeup IDs/reasons and task status/counts/H refs,
not repeated source or output. Counts/references must identify the actual task.

Use monotonic deadlines while live plus durable wall-clock due times for
recovery. Due events queue while busy. Coalesce simultaneously ready wakeups
into one outer continuation; deliver each event ID at most once. Preserve
existing user steering/follow-up ordering and give foreground user work priority.
Forced-collapse gating still applies to requests triggered by wakeups. In
--no-model mode mark due wakeups consumed with a UI notification, never invoke
a provider. Wakeup cancellation is idempotent; missing IDs reject; /wakeup run
explicitly authorizes a pending/scheduled wakeup for the next safe boundary.

Commit dispatch intent before invoking a provider. Deduplicated local dispatch
is not a guarantee of exactly-once remote execution/billing across crashes.
An interrupted dispatch has unknown outcome and must not automatically retry.
On explicit session resume restore unfinished tasks as outcome_unknown and
unfired wakeups as pending notices requiring explicit /wakeup run confirmation;
never automatically run jobs, overdue timers, or old cells. The harness is not
a daemon and cannot wake itself while stopped. Never signal a PID reconstructed
from an old journal: PID reuse makes that unsafe.

### Session transitions and shutdown
Refuse explicit /quit, /new, /resume or equivalent JSON/session transitions
while owned jobs remain live unless the user explicitly requests cancellation.
Cancellation authorization performs bounded termination/drain/reap before exit
or replacement of Host. Handle terminal/JSON EOF and unavoidable shutdown with
bounded best-effort cleanup and preservation of committed partial output.
Fresh-worker /reset does not restart or otherwise mutate isolated background
jobs. Preserve all pending wakeups/tasks as session history across transitions;
loading reconstructs metadata without dispatching side effects.

### Acceptance requirements (automated CLI/PTY witnesses; coverage review pending)
Focused verification: PY_HARNESS_BIN=target/debug/py cargo test -j 4 background
-- --test-threads=2. Fixtures use fake local providers, never live credentials.
[ ] R14.01 Shell/Python jobs run concurrently with foreground work; isolated Python has no main globals/agent bridge; live limit and typed validation reject before spawn.
      Tests: e2e_bgtasks_shell_python_isolation_and_limit; evidence: CLI witness passed; review: pending.
[ ] R14.02 Immediate stable empty/nonempty stream refs, continuous capture and terminal counts/status are correct across interleaved task/foreground output without payload leakage.
      Tests: e2e_bgtasks_stream_refs_and_context_privacy; evidence: CLI witness passed, including multi-megabyte capture; review: pending.
[ ] R14.03 Task kill/timeout escalates, preserves partial output, reaps children and remains responsive during blocked foreground execution; duplicates are harmless.
      Tests: e2e_bgtasks_busy_kill_timeout_and_idempotence; evidence: CLI Python/shell busy witnesses passed; review: pending.
[ ] R14.04 Python/slash/JSON task and wakeup controls share validation, registry, command deduplication, completion linkage, help and completion coverage.
      Tests: e2e_bgtasks_controls_and_command_deduplication, e2e_bgtasks_slash_controls_and_user_only_logs, background_completion_catalog_covers_ids_and_flags; evidence: CLI/unit witnesses passed; review: pending.
[ ] R14.05 Timers fire while JSON/terminal input is idle; interactive multiline draft/cursor and terminal modes survive servicing and wakeup dispatch.
      Tests: e2e_idle_json_services_background_completion_and_wakeup, e2e_idle_tty_wakeup_preserves_multiline_draft_and_cursor; evidence: JSON/PTY witnesses passed; review: pending.
[ ] R14.06 Stop stages/replaces timers, arms only on successful actual stop, and obeys exception/cancellation/steering priority without killing the worker.
      Tests: e2e_stop_wakeup_settlement_and_order; evidence: CLI exception/cancellation/replacement/steering witnesses passed; review: pending.
[ ] R14.07 Opt-in task completion and coalesced timers queue at safe boundaries during Python/shell/provider/input waits; no competing outer turns, source/output leakage or no-model provider invocation.
      Tests: e2e_bgtasks_wakeup_safe_boundaries_and_privacy; evidence: local provider wait/coalescing and Python-input boundary witnesses passed; review: pending.
[ ] R14.08 Crash/recovery never replays task effects, signals stale PIDs or automatically dispatches recovered wakeups; dispatch uncertainty stays unknown.
      Tests: e2e_bgtasks_wakeup_crash_resume_without_replay; evidence: crash/resume, stale PID and interrupted provider-dispatch witnesses passed; review: pending.
[ ] R14.09 Session/quit guards, explicit cancellation, EOF and persistence-failure cleanup terminate owned groups with bounded partial capture and no false success.
      Tests: e2e_bgtasks_session_shutdown_and_failure_cleanup, e2e_bgtasks_controls_and_command_deduplication; evidence: EOF, spawn failure, real journal-write failure, guarded quit/new and authorized cancellation witnesses passed; review: pending.

## 30. Durable entries, initialization snapshots and active memory curation (approved scope)
### One ordinary-file system
Use one global directory, ${PY_HOME}/skills/ (default ~/.py/skills/), for
memories, skills and reusable Python. Entries are ordinary UTF-8 Markdown
files. The user and model create, edit, rename, split, merge and delete them
using ordinary filesystem operations. There is no agent.skills API, CRUD
helper, /skills command, plugin registration, executable hook discovery or
project-environment manager. Rust only discovers, validates, snapshots,
renders and initializes entries according to this section. This is a narrow
exception to earlier exclusions of skills, not adoption of Pi/Pig skills or
an extension framework. Other plugin/provider-hook exclusions remain intact.

Initially discovery is flat: direct regular *.md files only, sorted by filename
in deterministic lexical order. No recursion or symlink following. A discovered
*.md symlink or non-regular file rejects initialization with its path; unrelated
files are ignored. A missing directory is an empty collection; normal first
startup creates the private directory. No mandatory per-project hierarchy.
Filename stem is entry identity and title; a rename changes identity. There is
no separate title, person, project or mandatory subject metadata field.

Each file begins with --- front matter and ends that header with --- on its own
line. Version 1 accepts a documented YAML scalar subset, not arbitrary YAML:
exactly one key/value line for each required field, with no nested structures,
anchors, duplicate keys, multiline scalars or unknown fields. Text values may
be plain single-line text or JSON double-quoted strings with JSON escaping.
core must be the unquoted literal true or false. Required fields are:
  kind: memory | skill | python
  created: UTC creation timestamp
  updated: UTC last-update timestamp
  origin: agent | user
  description: nonempty short, single-line discovery text
  core: true | false
Timestamps are valid Gregorian YYYY-MM-DDTHH:MM:SSZ values (years 0001-9999); updated must not
precede created. The writer maintains timestamps and valid metadata, including
on manual edits. origin records original authorship, not authentication, trust
or execution permission; editing does not require changing original authorship.
Description and filename must not contain terminal/control characters.

Example:
  ---
  kind: memory
  created: 2026-10-10T09:00:00Z
  updated: 2026-10-10T09:00:00Z
  origin: user
  description: Explicit user preferences for review responses.
  core: true
  ---
  Prefer concise review summaries, with blockers first.

memory and skill bodies are ordinary Markdown. A python entry contains exactly
one fenced block opened by ```python and closed by ``` on their own lines
(trailing whitespace and CRLF line endings are accepted). That block is the complete executable source; surrounding Markdown documents
its scope and helpers. Never execute Markdown wholesale, concatenate inferred
snippets or execute other fenced blocks. Missing, unterminated or multiple
Python blocks reject initialization, including for non-core Python entries.

### Immutable session initialization snapshot
On /new or first new-session creation, load configuration, read each discovered
file once into a bounded buffer, validate metadata/source and budgets, and
construct a session-owned snapshot before dispatching startup Python or any
outer provider request. Persist the exact effective system string, ordered
inventory metadata and content hashes, configured skill limits/estimator, and
exact core Python source. Core bodies are preserved in the rendered string.
Rust commits the snapshot as a journal event (skills_snapshot) before execution;
subsequent requests use the stored string, not a live directory-backed view.

Core entries of all three kinds are rendered in full, including identifying
wrappers and full Python source. Non-core entries contribute only filename,
kind, description, core=false and path to the inventory. Non-core bodies and
Python never execute or enter model payload automatically. Include a clear
user-maintained section after harness instructions; entry content cannot alter
harness execution/context/control rules. There is one outer system snapshot;
inner agent.llm calls retain their own explicit system settings and receive no
automatic entry injection.

Files created, changed, deleted or renamed after initialization do not modify
the current prompt, inventory or startup-source snapshot. /new observes current
files and skill configuration. /resume restores the original snapshot even if
files are missing or changed; /reset preserves that snapshot. Resume/reset do
not silently refresh entries or render with newer metadata/defaults. A legacy
session without a snapshot receives a frozen SYSTEM-only snapshot with empty
inventory and no startup entries; it never loads current files. /new opts into
the current entry collection. Record this compatibility snapshot in the journal.

Ordinary explicit reads of newer files may append selected content via existing
agent.context.read_text rules. Merely returning/printing a file's contents does
not select them. New user/source/control items still append normally. This
section freezes the system prefix and historical renderings, not the ability
to add context. Collapse and approved output-payload eviction remain the only
mechanisms that replace selected historical content; automatic eviction never
discards arbitrary old user messages, source, summaries or this protected system
snapshot. Control/usage updates append rather than mutate its text in place.

### Core Python initialization and failure semantics
Initialize only the main foreground worker, in its ordinary global namespace,
after agent and H are available. Use the frozen snapshot's filename ordering.
Precompile every core Python block before executing any block: a syntax error
in a later file prevents all startup-source dispatch. Metadata/size/token
validation likewise finishes before startup side effects. This does not make
ordinary Python execution a sandbox or guarantee absence of interpreter/import
side effects. Core source runs unrestricted as the current user.

For each dispatched block, Rust durably commits source/identity/hash, generation
and execution intent first, then captures complete stdout/stderr and commits
completion/status separately. Full diagnostics remain inspectable through H
and local error UI; only status/counts/refs are automatically model-visible.
Startup output, exceptions, say-like events and helper results do not become
alternative model-payload channels. Never label a partially initialized worker
ready or invoke the outer agent after initialization fails. A resumed blocked
session emits initialization_blocked instead of ready, with startup_ready=false;
local commands remain available for inspection and explicit reset.

During startup, agent/H bridge operations are limited to read-only history
access (history and history_len). Reject context mutation, shell/provider/
background calls, interactive input and lifecycle controls with captured
diagnostics. This prevents startup from recursively invoking the outer loop,
resetting itself, requesting input or silently selecting payloads. Ordinary
Python file/network/subprocess operations remain unrestricted; the bridge
restriction is lifecycle policy, not security.

Every intentionally fresh main worker runs the frozen core source once: initial
creation, explicit reset and successful explicit resume. This is declared
initialization, not replay of prior user/model cells. Do not run it twice during
one transition; background Python jobs never load it. Help and lifecycle notices
must disclose that initializer side effects can repeat, and recommend imports/
definitions rather than irreversible actions. After unexpected worker failure,
replace the dead worker but block core initialization and the outer agent until
explicit /reset; journal startup_blocked so resume cannot silently retry it.
Previous ordinary variables remain undefined; only initialization deliberately
defines new globals.

A runtime error may leave prior initializer effects/globals; no rollback,
automatic retry or automatic continuation. An interrupted intent has unknown
outcome. startup_begin/startup_end record initialization settlement. A failed or
unknown startup blocks the outer agent. On resume, do not automatically execute
such startup again: preserve the blocked state, expose captured diagnostics and
uncertainty, and require /reset to explicitly authorize a fresh initialization
attempt using the frozen source. Corrected files/configuration take effect only
in an explicitly new session. Initial startup errors may abort new-session
creation; do not claim that initialization succeeded or dispatch outer requests.
Snapshot/config failure before dispatch must not claim that any Python entry ran.

### Configuration, budgets and user-local management
Configuration lives in ${PY_HOME}/config.json (default ~/.py/config.json).
On normal first startup create documented defaults if missing, atomically with
owner-only permissions; do not overwrite existing files. --help/--version
remain side-effect-free. Credentials stay in separate private auth.json and
are not manageable or displayed through generic /config controls. Reject nested
credential fields in config loading and writes, including parent-object or
array writes; do not persist /config commands in editor history.

Initial skills defaults:
  {"skills":{"enabled":true,"max_system_tokens":8000,
    "max_core_entry_tokens":2000,"max_inventory_entry_tokens":128,
    "max_entries":128,"max_file_bytes":65536}}
Missing individual keys use documented defaults. enabled is boolean; each
limit is an integer in 1..=16777216, never bool. Unknown keys inside skills reject
configuration; unrelated top-level/provider settings are preserved.
With enabled=false, do not discover, validate or execute entries and do not
instruct the agent to maintain this disabled store. Still persist a system
snapshot for stable new-session/resume behavior.

Token values are estimates, not claims of provider-tokenizer equality. Version
1 uses ceil(Unicode-code-point-count / 3), labeled unicode-chars/3-v1, with explicit
warning that the estimate is not a guaranteed upper bound across text/models.
The aggregate skills allowance charges all added guidance, wrappers, full core
bodies and non-core inventory. Per-core and per-inventory allowances charge the
complete rendered entry including wrappers. File byte/count limits apply
independently, including non-core bodies. Do not silently truncate, omit entries,
mark oversized entries non-core or execute a budget-rejected initializer.
Reject overflow before startup with path, estimated usage, limit and remedies.

Overall context accounting must include the complete effective system prompt
as well as history/control metadata and existing attachment allowances. Do not
shrink or replace the snapshot when switching models or entering pressure mode;
report an unsatisfiable protected-prefix budget rather than collapse it. Leave
room for the existing 4096-token output reserve and an additional 1024 estimated
tokens for useful continuation at initialization. Resolve CLI model/effort
and /new inherited overrides before startup admission or core execution.
/new inherits live model/effort only when their corresponding configured defaults
have not changed since this session loaded them; changed defaults take precedence.

User-local commands:
  /config                          effective configuration, path and pending status
  /config get <dotted-key>          effective value, including defaults
  /config set <dotted-key> <JSON>   validate and atomically save an explicit value
  /config unset <dotted-key>        remove explicit value; restore its default
  /config reload                   validate externally edited configuration
Before saving, detect configuration changes since the loaded version; reject
conflicting writes instead of losing external edits. Preserve unrelated keys,
never partially apply invalid input, and use owner-only atomic replacement.
Display errors locally, not automatically as model context. Version 1 config
commands change loading defaults only, not live model/effort/session settings;
use existing /model and /effort commands for immediate session changes. All
config loading changes apply on /new. Skills reload/set/unset never change an
existing snapshot or reset/resume source. Configuration commands manage
configuration only; entry creation and curation remain ordinary file operations.

### Required system-prompt explanation and active curation guidance
Explain the actual skills directory, exact metadata/body format, budgets,
core behavior, frozen inventory and next-session activation rule. Explain
ordinary file inspection and explicit context selection for non-core entries.
No special entry-management functions are implied or exposed.

Instruct the model to actively maintain useful durable knowledge: explicitly
stated user preferences, recurring corrections, stable project conventions,
reusable procedures and durable discoveries. Distinguish explicit statements
from inferences; do not promote temporary task instructions into permanent
preferences. Avoid secrets, unsupported personal facts and transient execution
state. Scope information to where it is relevant.

Actively curate, not merely accumulate: update stale information, remove obsolete
or redundant entries, split unrelated/unwieldy entries and merge overlapping
entries. Preserve still-relevant user preferences, useful details and scope;
do not discard them merely to save space. Prefer a small, coherent, accurate
collection rather than a growing archive.

Prefer small focused entries, each covering one coherent subject: a person,
project, preference, convention, procedure or Python helper. Avoid catch-all
files and excessive fragmentation. Use descriptive filenames and identify
person/project subject and applicability in the description/body. These are
prompt conventions, not enforced kinds, fields, folder structures or a mandatory
project schema. kind remains memory, skill or python. Use core sparingly for
broadly useful knowledge. Warn explicitly that core Python executes automatically
and prefer definitions/imports over side effects. origin is not a trust signal.

### Acceptance requirements (verified 2026-10-10)
Evidence: cargo build -j 8; PY_HARNESS_BIN=$PWD/target/debug/py cargo test -j 8
-- --test-threads=8. Full suite: 212 passed, including 26 skills tests. Actual
local-provider requests, filesystem effects and journal records are asserted;
this does not claim live-provider parity or that a model always follows curation.
[x] R15.01 Flat ordinary-file discovery, required metadata/scalar grammar, dates, UTF-8, filenames and Python-block validation reject malformed entries without dispatch.
      Tests: skills_metadata_and_date_validation, skills_python_single_block, e2e_skills_invalid_metadata_and_python_fences_reject_initialization, e2e_skills_defaults_privacy_help_version_and_discovery_validation; review: negative paths inspected.
[x] R15.02 Core bodies are fully present in actual outer provider requests; non-core bodies are absent, with complete bounded inventory and curation guidance.
      Tests: e2e_skills_provider_wire_snapshot_edit_reset_resume_new, e2e_skills_full_core_and_inventory_only_noncore; review: wire body assertions inspected.
[x] R15.03 File edits never mutate the current prompt; /new refreshes while reset/resume restore exact snapshots after edits/deletion.
      Tests: e2e_skills_provider_wire_snapshot_edit_reset_resume_new, e2e_skills_resume_deleted_files_uses_original_snapshot, e2e_skills_file_edit_freezes_session_until_new; review: exact prompt/snapshot comparisons inspected.
[x] R15.04 Core Python precompiles globally, initializes ordered main globals once per fresh generation, and never initializes isolated background Python.
      Tests: e2e_skills_core_python_order_global_namespace_and_reset, e2e_skills_precompile_all_before_any_startup_side_effects, e2e_skills_inner_and_background_do_not_inherit_entries; review: globals, effects and isolated subprocess output inspected.
[x] R15.05 Startup journals intent/source/completion and full diagnostics without payload selection; failed/unknown startup blocks invocation and never silently retries.
      Tests: e2e_skills_core_python_order_global_namespace_and_reset, e2e_skills_runtime_failure_blocks_resume_and_requires_explicit_reset, e2e_skills_unknown_startup_intent_is_not_automatically_replayed, e2e_skills_worker_crash_does_not_repeat_initialization_implicitly; review: source/status journals and repeat-count assertions inspected.
[x] R15.06 Aggregate/per-entry/file/count budgets reject before startup, never truncate; full protected system text participates in context accounting and model-pressure checks.
      Tests: e2e_skills_budget_rejection_precedes_python_execution, e2e_skills_inventory_aggregate_byte_and_count_budgets_precede_execution, e2e_skills_protected_prefix_admission_model_override_and_accounting, e2e_skills_collapse_cannot_expand_history_using_system_allowance, e2e_forced_gate_blocks_side_effects_and_allows_stderr_read; review: pre-dispatch effect markers and protected accounting inspected.
[x] R15.07 /config get/set/unset/reload validate, persist, preserve unrelated keys, detect external-edit conflicts and keep skills changes pending for /new.
      Tests: e2e_skills_config_set_get_unset_persistence_and_invalid_value, e2e_skills_config_reload_affects_new_session_not_existing_snapshot, e2e_skills_config_conflicts_reload_and_private_atomic_save, e2e_skills_config_changed_model_effort_defaults_apply_on_new, e2e_skills_config_nested_credentials_refused_without_disclosure; review: persistence, conflicts and activation inspected.
[x] R15.08 Defaults/private directories/files and side-effect-free help/version behave correctly; disabled storage neither loads files nor requests curation.
      Tests: e2e_skills_defaults_privacy_help_version_and_discovery_validation, e2e_skills_disabled_ignores_entries_and_startup; review: permissions, absent directories and malformed disabled entry inspected.
[x] R15.09 Prompt guidance explains active preference/project/person curation, stale removal, splitting/merging, small scoped entries and ordinary filesystem-only management.
      Tests: skills_curation_prompt_contract, e2e_skills_provider_wire_snapshot_edit_reset_resume_new; review: instruction text and actual wire inclusion inspected.

## 31. Refreshable model catalogs
Source layout stays single-file: implementation, specification and tests live
in main.rs. Model metadata is data, not code or durable skill instructions.

Commands:
- /model list [query] and /models [query]: list built-in/configured/cached models.
- /model list refresh [provider] and /models refresh [provider]: force discovery,
  even when the cache is fresh or automatic refresh is disabled. Omitted provider
  means the active provider; codex is an alias for openai-codex.
- JSON models commands accept refresh:true and optional provider. Ordinary JSON
  models and agent.llm.list remain offline/cache-only introspection.

Discovery currently supports Codex subscriptions and the OpenAI API. Other
providers retain their built-in/configured inventory and report unsupported
explicit refresh; there is no guessed compatible endpoint.
Codex GET /codex/models uses authorization, account routing and client_version;
OpenAI GET /models proves available IDs, not their unknown capabilities.
Source: openai/codex models-manager, codex-api endpoint/models and protocol
openai_models.rs (reviewed 2026-10-10). Native context_window takes precedence over
max_context_window; the effective input allowance uses the native percentage.
Do not reuse public API context maxima as native subscription defaults.

Configuration under model_catalog, manageable with /config:
  auto_refresh: true
  refresh_interval_seconds: 3600
  retry_interval_seconds: 300
  codex_client_version: "0.155.0"
Catalog refresh preferences apply immediately; they never rewrite session
prompts or startup snapshots. Refresh is activity-triggered on login/model
selection/listing (and implicit Codex default selection), not a background timer.
Only stale, credential-backed supported catalogs fetch automatically; recent
failed attempts back off. Forced refresh bypasses both intervals. The Codex
catalog compatibility revision is separate from py's package version and is
included in cache scope; do not send py 0.1.0 as an obsolete Codex revision.
PY_MODEL_CATALOG_AUTO_REFRESH=0/1 is a user-owned override, also used by fixtures
so routine tests never accidentally probe external endpoints.

Cache: ~/.py/models.json (PY_HOME respected), versioned and private, atomically
written under a cancellable lock. Entries are bound to provider, endpoint,
credential/principal and compatibility revision using hashes, not plaintext
credentials/account IDs. Codex principal identity survives ordinary token
rotation but changes with account/subject/plan; API keys have distinct scopes.
Failed refreshes preserve good descriptors and record an attempt timestamp;
concurrent failures/older responses cannot overwrite newer good scoped data.
Invalid caches are ignored safely. Offline use starts with cached/bundled data.

Remote responses are bounded to 8 MiB/2048 entries. Normalize whitelisted metadata
only; never execute/import/inject remote instructions, messages, tools or plugins.
Reject malformed schemas atomically and reject reflected credential data. Native
reasoning presets are limited to implemented levels; ultra is not supported.
Unknown API IDs, including fine-tunes, are visible but cannot be selected or
invoked without explicit API/context/capability metadata in config.json. Explicit
user declarations override discovery; discovery never fabricates capabilities.
Account catalogs mark absent built-ins unavailable without destroying legacy
session IDs. HTTP errors are bounded/redacted, never silently retried for inference.

Refreshing does not select a model, rebuild Python, change globals, modify the
frozen system prompt or select discovery payloads into model context. It can update
capability/pressure limits. Genuine cancellation retains the prior cache; an old
agent.loop.stop is not cancellation of a later metadata operation.

Acceptance evidence: cargo build -j 8; PY_HARNESS_BIN=$PWD/target/debug/py cargo
 test -j 8 -- --test-threads=4. 233 tests pass, including 16 catalog tests;
local real-binary fixtures verify HTTP wire/caches, not live account compatibility.
[x] R16.01 Native/API endpoints, normalization, fresh IDs/aliases and private
    persistent cache work. Tests: e2e_catalog_force_refresh_native_metadata_namespace_prompt_and_private_cache; catalog_native_limits_fields_aliases_and_efforts.
[x] R16.02 Stale auto refresh, fresh suppression, explicit bypass, failure backoff,
    disabled auto and endpoint forms work. Tests: e2e_catalog_automatic_ttl_force_fresh_bypass_and_expiry; e2e_catalog_failure_backoff_preserves_stale_cache_and_manual_retry; e2e_catalog_auto_disabled_manual_refresh_and_prefix_endpoint_forms.
[x] R16.03 Malformed/oversized/error responses are atomic, redacted and cannot
    import instructions or reflected credentials. Tests: e2e_catalog_rejections_are_atomic_bounded_redacted_and_instruction_free; catalog_native_rejects_malformed_response_atomically; catalog_cache_validation_strips_instructions_and_rejects_unsafe_files.
[x] R16.04 Account/endpoint/token scope and unknown-ID capability gates work.
    Tests: e2e_catalog_principal_endpoint_rotation_and_logout_scope; e2e_catalog_openai_unknown_ids_need_declared_capabilities_and_key_scope; catalog_api_known_metadata_unknown_ids_and_safe_field_selection.
[x] R16.05 Active model, namespace and exact wire prompt survive refresh; prior
    turn stop does not abort refresh; genuine interruption preserves cache.
    Tests: e2e_catalog_force_refresh_native_metadata_namespace_prompt_and_private_cache; e2e_catalog_interrupt_is_operation_local_and_retains_exact_cache.
[x] R16.06 Configuration/default validation and unsupported/missing-credential
    requests fail without probes. Tests: catalog_config_defaults_overrides_and_invalid_values; e2e_catalog_missing_credentials_unsupported_provider_and_bad_refresh_flag_do_not_fetch.

## 26. Running this transition
Build: cargo build --release --manifest-path transition/Cargo.toml
Run local: transition/target/release/py --no-model
Run model harness: transition/target/release/py
Start with /login to list methods. /login codex and /login anthropic use browser
subscription login; /login codex device is the headless alternative. Anthropic
shows a hosted code#state to paste into the hidden prompt; Codex returns via a
local callback, with /login codex manual for hidden redirect-URL paste.
/login anthropic api-key and /login openai use hidden API keys, never CLI keys.
Tab opens a fuzzy picker; type to filter, arrows/Tab to move, Enter to choose,
then Enter again to submit. Escape cancels without modifying the original line.
Shift+Enter inserts a newline on supported enhanced-key terminals; Ctrl-J is a
fallback. Continuations align with the two-column > prompt. Semantic status
lines show the model only while thinking, and numbered cells end with status,
elapsed time and history refs. NO_COLOR disables generated styles. /model list filters
cached/built-in/configured models and refreshes stale supported catalogs; /model
list refresh forces discovery. /model codex/sol61 selects the current Sol alias.
Config lives in ~/.py/config.json (or PY_HOME); credentials in private auth.json.
Current Sol uses gpt-6.1-sol; /model codex/sol61 resolves its known alias.
Current Codex/API metadata is checked against official model documentation:
https://developers.openai.com/codex/models and /api/docs/models/gpt-6.1-sol
(2026-10-10), not inferred from the old pinned adapter catalog. Sol/Astra/Luna
and GPT-5.6 variants expose documented reasoning levels, including max; unsupported
levels fail locally. Catalog membership is not account/workspace access proof.
Successful explicit login selects a model from that provider only if the active
model is unusable. Usable selections are preserved; global defaults are not changed.
On a new process, saved Codex credentials may select the current recommended Codex
model only when no CLI/environment/config/session model was explicitly chosen.
Initial resolved settings are journaled separately from user setting changes and
restored on resume, even if global defaults have since changed. Input admission,
pressure and remaining-input accounting respect separate input caps: public API
metadata uses 922000/1050000 input/combined tokens, while the current native Codex
fallback uses 258400/272000 and remote discovery supplies authoritative values.
Provider tokens are still
estimated, not counted exactly.
Codex HTTP rejections include bounded, credential-redacted service diagnostics;
there is still no automatic retry or model fallback after rejection.
Legacy gpt-5.2/gpt-5.3 Codex subscription entries are marked deprecated; their
availability is not implied by this offline catalog.
Example for a user-declared custom ID (not a claim this fixture model exists):
  {"providers":{"openai-codex":{"models":[{"id":"fixture-6.1",
    "api":"openai-codex-responses","context_limit":512000,"reasoning":true}]}}}
Then /model codex/fixture61 and its Tab completion resolve that configured model.
No Pi/Pig config or credentials are implicitly imported. Endpoint overrides are
user-owned and may receive credentials; PY_CODEX_AUTH_BASE_URL and
PY_CODEX_DEVICE_TIMEOUT_SECONDS exist for controlled protocol fixtures, not
remote configuration discovery. Browser fixtures may override
PY_OAUTH_CALLBACK_PORT, PY_OAUTH_TIMEOUT_SECONDS, PY_ANTHROPIC_AUTH_BASE_URL and
PY_ANTHROPIC_AUTHORIZE_URL. Never accept these from an untrusted project.
PY_OAUTH_BROWSER is a user-owned executable (not a shell command string); it
receives the authorize URL as one argument. Use a wrapper executable to select a
browser/profile. OpenAI owns the Google/social sign-in buttons; py cannot force
one through a documented OAuth option. Execution is unrestricted ordinary Python.
Use @print('hello') for a hidden cell and @@print('hello') for a visible one;
@ prefixes are routing delimiters only, source whitespace is not rewritten.
!printf 'hello\n' is a hidden shell command; !! makes the interaction visible.
Only explicit agent.read_text/read_raw select output payloads for the model.
agent.say('...') renders human Markdown without silently adding context payloads.
/resume starts fresh Python and restores H/context/settings; /new starts fresh H.
Use --help for flags and /help for editor/commands. JSON mode:
  transition/target/release/py --json --json-input --no-model
  {"id":"cell1","kind":"python","source":"print(42)"}
Keep the old Python harness unchanged: this is still a transition, not full
R01–R13 acceptance.
"####;


// Fixtures must not inherit developer credentials or endpoint/model overrides.
// Explicit .env(...) fixture values can still be set by callers after this helper.
#[cfg(test)]
fn test_command(program:impl AsRef<std::ffi::OsStr>)->std::process::Command{
    let mut command=std::process::Command::new(program);
    for (_,_,key) in PROVIDERS{command.env_remove(key);}
    command.env("PY_MODEL_CATALOG_AUTO_REFRESH","0");
    for key in ["PY_HOME","PY_MODEL","PY_CONTEXT_LIMIT","PY_CODEX_AUTH_BASE_URL",
        "PY_CODEX_DEVICE_TIMEOUT_SECONDS","PY_OAUTH_BROWSER","PY_OAUTH_TIMEOUT_SECONDS",
        "PY_OAUTH_CALLBACK_PORT","PY_ANTHROPIC_AUTH_BASE_URL","PY_ANTHROPIC_AUTHORIZE_URL"]{command.env_remove(key);}
    command
}

#[cfg(test)]
mod e2e {
    use std::{process::Stdio, io::Write, path::PathBuf};
    use serde_json::{Value, json};
    fn run(commands: Vec<Value>) -> (Vec<Value>, PathBuf) {
        let home = std::env::temp_dir().join(format!("py-e2e-{}-{}", std::process::id(),
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()));
        std::fs::create_dir_all(&home).unwrap();
        let bin = std::env::var("PY_HARNESS_BIN").expect("set PY_HARNESS_BIN to the built CLI");
        let mut child = crate::test_command(bin).args(["--json", "--json-input", "--no-model"])
            .env("PY_HOME", &home).stdin(Stdio::piped()).stdout(Stdio::piped())
            .stderr(Stdio::piped()).spawn().unwrap();
        let mut stdin = child.stdin.take().unwrap();
        for c in commands { writeln!(stdin, "{}", c).unwrap(); }
        drop(stdin);
        let out = child.wait_with_output().unwrap();
        assert!(out.status.success(), "CLI failed: {}", String::from_utf8_lossy(&out.stderr));
        let events = String::from_utf8(out.stdout).unwrap().lines().map(|s|
            serde_json::from_str(s).expect("stdout must be JSONL")).collect::<Vec<Value>>();
        (events, home)
    }

    fn model_run(codes: Vec<&str>, commands: Vec<Value>) -> (Vec<Value>,PathBuf,Vec<Value>) {
        use std::net::TcpListener;
        use std::io::{Read,BufRead,BufReader};
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();
        let address=listener.local_addr().unwrap();
        let codes:Vec<String>=codes.into_iter().map(str::to_string).collect();
        let server=std::thread::spawn(move||{
            let mut requests=vec![];
            for code in codes{
                let (mut socket,_)=listener.accept().unwrap();
                socket.set_read_timeout(Some(std::time::Duration::from_secs(10))).unwrap();
                let mut reader=BufReader::new(socket.try_clone().unwrap());
                let mut line=String::new();reader.read_line(&mut line).unwrap();
                let mut length=0;
                loop{
                    line.clear();reader.read_line(&mut line).unwrap();
                    if line=="\r\n"{break;}
                    if line.to_lowercase().starts_with("content-length:"){
                        length=line.split(':').nth(1).unwrap().trim().parse::<usize>().unwrap();
                    }
                }
                let mut bytes=vec![0;length];reader.read_exact(&mut bytes).unwrap();
                let request:Value=serde_json::from_slice(&bytes).unwrap();
                let code=if code=="FIXTURE_COLLAPSE_ACTUAL_BOUNDARIES"{
                    let messages=request["messages"].as_array().unwrap();
                    let start=messages.iter().filter_map(|m|m["content"].as_str())
                        .find(|s|s.contains("large user task")).unwrap();
                    let end=messages.iter().filter_map(|m|m["content"].as_str())
                        .find(|s|s.ends_with("\nend-marker")).unwrap();
                    let boundary=|s:&str|s.lines().next().unwrap()
                        .strip_prefix("[boundary ").unwrap().strip_suffix(']').unwrap().to_string();
                    format!("agent.context.collapse({:?}, {:?}, 'unique-condensed-summary')",boundary(start),boundary(end))
                }else{code};
                requests.push(request);
                let body=json!({"choices":[{"message":{"content":code}}],
                    "usage":{"prompt_tokens":20,"completion_tokens":10,"prompt_tokens_details":{"cached_tokens":5}}}).to_string();
                write!(socket,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",body.len(),body).unwrap();
            }
            requests
        });
        let home=std::env::temp_dir().join(format!("py-model-e2e-{}-{}",std::process::id(),
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()));
        std::fs::create_dir_all(&home).unwrap();
        let bin=std::env::var("PY_HARNESS_BIN").unwrap();
        let mut child=crate::test_command(bin).args(["--json","--json-input"])
            .env("PY_HOME",&home).env("OPENAI_BASE_URL",format!("http://{address}/v1"))
            .env("OPENAI_API_KEY","fixture-not-secret").env("PY_MODEL","openai/fixture")
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
        let mut stdin=child.stdin.take().unwrap();
        for c in commands{writeln!(stdin,"{}",c).unwrap();}drop(stdin);
        let out=child.wait_with_output().unwrap();
        assert!(out.status.success(),"{}",String::from_utf8_lossy(&out.stderr));
        let events=String::from_utf8(out.stdout).unwrap().lines().map(|s|serde_json::from_str(s).unwrap()).collect();
        (events,home,server.join().unwrap())
    }
    #[test]
    fn e2e_actual_requests_keep_code_and_hide_outputs() {
        let (_,h,r)=model_run(vec![
            "print('sensitive-output')",
            "agent.context.read_text(H.stdout[0][:4000])",
            "agent.say('finished')\nagent.loop.stop()"],
            vec![json!({"id":"a","kind":"submit","text":"inspect"})]);
        assert_eq!(r.len(),3);
        assert!(r[1]["messages"].to_string().contains("print('sensitive-output')"));
        assert!(!r[1]["messages"].as_array().unwrap().iter().any(|m|
            m["role"]=="user"&&m["content"].as_str().unwrap_or("").contains("sensitive-output")));
        assert!(r[2]["messages"].as_array().unwrap().iter().any(|m|
            m["role"]=="user"&&m["content"].as_str().unwrap_or("").contains("sensitive-output")));
        assert!(r.iter().all(|v|v.get("tools").is_none()));
        let usage=journal(&h).into_iter().rfind(|v|v["kind"]=="usage").unwrap();
        assert_eq!(usage["payload"]["input_tokens"],60);
        assert_eq!(usage["payload"]["cache_hit_tokens"],15);
    }
    #[test]
    fn e2e_direct_prefix_visibility_and_full_user_history(){
        let (_,h,r)=model_run(vec!["agent.loop.stop()"],vec![
            json!({"id":"h","kind":"python","source":"print('hidden-result')"}),
            json!({"id":"v","kind":"python","source":"print('visible-result')","visible":true}),
            json!({"id":"a","kind":"submit","text":"go"})]);
        let text=r[0]["messages"].to_string();
        assert!(!text.contains("hidden-result"));
        assert!(text.contains("visible-result"));
        assert!(journal(&h).iter().any(|v|v["kind"]=="user"&&
            v["payload"]["text"].as_str().unwrap_or("").contains("[stdout]\nvisible-result")));
    }
    #[test]
    fn e2e_collapse_retains_one_call_silently(){
        let (_,h,r)=model_run(vec![
            "agent.context.read_text('large-original-'*200)\nagent.context.read_text('end-marker')",
            "FIXTURE_COLLAPSE_ACTUAL_BOUNDARIES",
            "agent.loop.stop()"],
            vec![json!({"id":"a","kind":"submit","text":"large user task ".repeat(100)})]);
        // Find whether the requested bounds were accepted; assertions require real collapse.
        let log=journal(&h);
        assert!(log.iter().any(|v|v["kind"]=="context_replace"&&v["payload"]["summary"]=="unique-condensed-summary"),
            "collapse must commit: {:?}",log.iter().filter(|v|v["kind"]=="context_add").collect::<Vec<_>>());
        let messages=r[2]["messages"].to_string();
        assert_eq!(messages.matches("unique-condensed-summary").count(),1);
        assert!(!messages.contains("large-original-"));
        assert_eq!(messages.matches("end-marker").count(),1);
        assert!(!messages.contains("Collapse succeeded"));
    }


    #[test]
    fn e2e_resume_fresh_namespace_and_history(){
        let (_,home)=run(vec![py("a","x=123\nagent.context.read_text('persisted selection')")]);
        let session=std::fs::read_dir(home.join("sessions")).unwrap().next().unwrap().unwrap().path();
        let mut child=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap())
            .args(["--json","--json-input","--no-model","--session",session.to_str().unwrap()])
            .env("PY_HOME",&home).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
        writeln!(child.stdin.take().unwrap(),"{}",py("b","print('x' in globals())\nprint(H.stdout[0])\nprint(len(agent.context.items()))")).unwrap();
        let out=child.wait_with_output().unwrap();
        assert!(out.status.success());
        let ev:Vec<Value>=String::from_utf8(out.stdout).unwrap().lines().map(|l|serde_json::from_str(l).unwrap()).collect();
        assert_eq!(done(&ev,"b")["status"],"ok");
        assert!(ev.iter().any(|v|v["kind"]=="notice"&&v["text"].as_str().unwrap().contains("variables are undefined")));
        let j=journal(&home);
        assert!(j.iter().any(|v|v["kind"]=="stream"&&v["payload"]["text"].as_str().unwrap_or("").starts_with("False\n")));
        assert_eq!(j.iter().filter(|v|v["kind"]=="code"&&v["payload"]["source"].as_str().unwrap_or("").contains("x=123")).count(),1);
    }
    #[test]
    fn e2e_batched_output_eviction_preserves_originals(){
        let source=(0..20).map(|i|format!("agent.context.read_text('output-{i}')")).collect::<Vec<_>>().join("\n");
        let (_,home)=run(vec![py("a",&source),py("b","print(len(H.events))")]);
        let j=journal(&home);
        let batches:Vec<_>=j.iter().filter(|v|v["kind"]=="context_replace"&&v["payload"]["reason"]=="output_batch").collect();
        assert_eq!(batches.len(),1);
        let items=batches[0]["payload"]["items"].as_array().unwrap();
        assert_eq!(items.iter().filter(|i|i["output"]==true).count(),10);
        assert!(items[0]["text"].as_str().unwrap().contains("omitted"));
        assert!(j.iter().any(|v|v["kind"]=="context_add"&&v["payload"]["text"]=="output-0"));
    }
    #[test]
    fn e2e_stdin_reply_is_not_protocol_corruption(){
        let mut c=InputClient::new(None);
        c.send(py("a","print(input('name?'))"));
        let prompt=c.until("input_prompt",None);
        c.send(input_command("reply",&prompt,"text",Some("Ada")));
        assert_eq!(c.until("completed",Some("a"))["stdout"]["chars"],4);
        c.finish();
    }

    // Real CLI driver with bounded event waits; failed assertions reap the child.
    struct InputClient {
        child:std::process::Child, input:Option<std::process::ChildStdin>,
        output:std::sync::mpsc::Receiver<Value>, home:PathBuf,
    }
    impl InputClient {
        fn new(resume:Option<(PathBuf,PathBuf)>)->Self{
            use std::io::BufRead;
            let (home,session)=match resume{Some((h,s))=>(h,Some(s)),None=>(
                std::env::temp_dir().join(format!("py-input-e2e-{}-{}",std::process::id(),
                    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos())),None)};
            let mut cmd=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap());
            cmd.args(["--json","--json-input","--no-model"]).env("PY_HOME",&home)
                .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::inherit());
            if let Some(session)=session{cmd.arg("--session").arg(session);}
            let mut child=cmd.spawn().unwrap();
            let input=child.stdin.take();let stdout=child.stdout.take().unwrap();
            let(tx,output)=std::sync::mpsc::channel();
            std::thread::spawn(move||{for line in std::io::BufReader::new(stdout).lines(){
                let event=serde_json::from_str(&line.unwrap()).unwrap();
                if tx.send(event).is_err(){break;}
            }});
            let mut client=Self{child,input,output,home};client.until("ready",None);client
        }
        fn send(&mut self,v:Value){writeln!(self.input.as_mut().unwrap(),"{v}").unwrap();}
        fn until(&mut self,kind:&str,id:Option<&str>)->Value{
            loop{
                let v=self.output.recv_timeout(std::time::Duration::from_secs(5))
                    .expect("CLI must remain responsive");
                if kind=="rejected"&&id.is_some_and(|id|v["command_id"]==id){
                    assert_ne!(v["kind"],"accepted","invalid replies must not be accepted first");
                    assert_ne!(v["kind"],"completed","invalid replies must not complete");
                }
                if v["kind"]==kind&&id.is_none_or(|id|v["command_id"]==id){return v;}
            }
        }
        fn finish(mut self)->PathBuf{
            self.input.take();
            let deadline=std::time::Instant::now()+std::time::Duration::from_secs(5);
            loop{
                if let Some(status)=self.child.try_wait().unwrap(){assert!(status.success());break;}
                assert!(std::time::Instant::now()<deadline,"CLI must exit after EOF");
                std::thread::sleep(std::time::Duration::from_millis(10));
            }
            self.home.clone()
        }
    }
    impl Drop for InputClient {fn drop(&mut self){let _=self.child.kill();let _=self.child.wait();}}
    fn input_command(id:&str,p:&Value,action:&str,value:Option<&str>)->Value{
        json!({"id":id,"kind":"stdin_reply","prompt_id":p["prompt_id"],"operation":p["operation"],
            "worker_generation":p["worker_generation"],"action":action,"value":value})
    }
    #[test]
    fn e2e_input_correlation_and_duplicate_replies(){
        let mut c=InputClient::new(None);
        c.send(py("cell","import builtins\na=input('first?')\nb=builtins.input('second?')\nassert (a,b)==('λ🙂','')\nassert H.stdin[:]==['λ🙂','']"));
        let first=c.until("input_prompt",None);
        assert!(first["prompt_id"].is_string());assert_eq!(first["operation"],"cell");
        for (id,field,value) in [("bad-prompt","prompt_id",json!("wrong")),
            ("bad-op","operation",json!("other")),("bad-generation","worker_generation",json!(999))]{
            let mut reply=input_command(id,&first,"text",Some("poison"));reply[field]=value;
            c.send(reply);c.until("rejected",Some(id));
        }
        for (id,action,value) in [("bool-action",json!(true),json!("poison")),
            ("unknown-action",json!("other"),json!("poison")),("missing-value",json!("text"),Value::Null),
            ("wrong-value",json!("text"),json!(42))]{
            let mut reply=input_command(id,&first,"text",Some("poison"));
            reply["action"]=action;reply["value"]=value;c.send(reply);c.until("rejected",Some(id));
        }
        c.send(json!({"id":"missing-link","kind":"stdin_reply","value":"poison"}));
        c.until("rejected",Some("missing-link"));
        let mut reply=input_command("one",&first,"text",Some("λ🙂"));
        reply.as_object_mut().unwrap().remove("action"); // omitted action defaults to text
        c.send(reply);
        c.until("completed",Some("one"));
        let second=c.until("input_prompt",None);assert_ne!(first["prompt_id"],second["prompt_id"]);
        c.send(input_command("one",&second,"text",Some("duplicate")));c.until("rejected",Some("one"));
        c.send(input_command("late",&first,"text",Some("late")));c.until("rejected",Some("late"));
        c.send(input_command("two",&second,"text",Some("")));c.until("completed",Some("two"));
        assert_eq!(c.until("completed",Some("cell"))["status"],"ok");
        c.send(input_command("after",&second,"text",Some("late")));c.until("rejected",Some("after"));
        let h=c.finish();let j=journal(&h);
        let inputs:Vec<_>=j.iter().filter(|v|v["kind"]=="stdin").collect();
        assert_eq!(inputs.len(),2);
        for (index,prompt,reply) in [(0,&first,"one"),(1,&second,"two")]{
            let input=inputs[index];assert_eq!(input["payload"]["prompt_id"],prompt["prompt_id"]);
            assert_eq!(input["payload"]["operation"],"cell");assert_eq!(input["payload"]["index"],index);
            let opened=j.iter().find(|v|v["kind"]=="input_prompt"&&v["payload"]["prompt_id"]==prompt["prompt_id"]).unwrap();
            let accepted=j.iter().find(|v|v["kind"]=="accepted"&&v["payload"]["command_id"]==reply).unwrap();
            let closed:Vec<_>=j.iter().filter(|v|v["kind"]=="input_closed"&&v["payload"]["prompt_id"]==prompt["prompt_id"]).collect();
            assert_eq!(closed.len(),1);assert_eq!(closed[0]["payload"]["action"],"text");
            assert!(opened["seq"].as_u64()<accepted["seq"].as_u64());
            assert!(accepted["seq"].as_u64()<input["seq"].as_u64());
            assert!(input["seq"].as_u64()<closed[0]["seq"].as_u64());
        }
    }
    #[test]
    fn e2e_input_eof_and_resume(){
        let mut c=InputClient::new(None);
        c.send(py("saved","assert input()=='retained🙂'\nassert input()==''"));let p=c.until("input_prompt",None);
        c.send(input_command("reply",&p,"text",Some("retained🙂")));c.until("completed",Some("reply"));
        let p=c.until("input_prompt",None);c.send(input_command("reply-empty",&p,"text",Some("")));
        c.until("completed",Some("reply-empty"));assert_eq!(c.until("completed",Some("saved"))["status"],"ok");
        c.send(py("eof","try:\n input()\nexcept EOFError:\n pass\nelse:\n raise AssertionError('expected EOF')\nassert H.stdin[:]==['retained🙂','']"));
        let p=c.until("input_prompt",None);c.send(input_command("eof-reply",&p,"eof",None));
        c.until("completed",Some("eof-reply"));assert_eq!(c.until("completed",Some("eof"))["status"],"ok");
        c.send(py("uncaught","input()"));let p=c.until("input_prompt",None);
        c.send(input_command("eof-uncaught",&p,"eof",None));c.until("completed",Some("eof-uncaught"));
        assert_eq!(c.until("completed",Some("uncaught"))["status"],"error");
        let h=c.finish();let session=std::fs::read_dir(h.join("sessions")).unwrap().next().unwrap().unwrap().path();
        let mut c=InputClient::new(Some((h,session)));
        c.send(py("restored","assert H.stdin[:]==['retained🙂','']\nassert 'p' not in globals()"));
        assert_eq!(c.until("completed",Some("restored"))["status"],"ok");
        c.send(py("channel-eof","try:\n input()\nexcept EOFError:\n pass\nelse:\n raise AssertionError('expected EOF')"));
        c.until("input_prompt",None);c.input.take();
        assert_eq!(c.until("completed",Some("channel-eof"))["status"],"ok");c.finish();
    }
    #[test]
    fn e2e_input_generation_and_reply_ids_survive_resume(){
        let mut c=InputClient::new(None);
        for id in ["reset1","reset2"]{c.send(json!({"id":id,"kind":"reset"}));c.until("completed",Some(id));}
        c.send(py("first","input()"));let mut previous=c.until("input_prompt",None);
        assert_eq!(previous["worker_generation"],2);
        c.send(input_command("reply0",&previous,"text",Some("initial")));c.until("completed",Some("reply0"));
        c.until("completed",Some("first"));let mut home=c.finish();
        for generation in [3,4]{
            let session=std::fs::read_dir(home.join("sessions")).unwrap().next().unwrap().unwrap().path();
            let mut c=InputClient::new(Some((home,session)));
            let id=format!("cell{generation}");
            c.send(py(&id,"assert H.stdin[0]=='initial'\ninput()"));let p=c.until("input_prompt",None);
            assert_eq!(p["worker_generation"],generation);
            assert_ne!(p["prompt_id"],previous["prompt_id"]);
            c.send(input_command("reply0",&p,"text",Some("duplicate")));c.until("rejected",Some("reply0"));
            c.send(input_command("stale",&previous,"text",Some("late")));c.until("rejected",Some("stale"));
            let reply=format!("reply{generation}");c.send(input_command(&reply,&p,"eof",None));
            c.until("completed",Some(&reply));assert_eq!(c.until("completed",Some(&id))["status"],"error");
            previous=p;home=c.finish();
        }
    }
    #[test]
    fn e2e_worker_stdin_is_not_command_stdin(){
        let(ev,_)=run(vec![py("raw","import os,sys,subprocess\nassert os.read(0,4096)==b''\nassert sys.stdin.read()==''\nassert subprocess.check_output(['python3','-c','import sys; print(repr(sys.stdin.read()))'])==b\"''\\n\""),
            py("next","print('commands preserved')")]);
        assert_eq!(done(&ev,"raw")["status"],"ok");assert_eq!(done(&ev,"next")["status"],"ok");
    }
    #[test]
    fn e2e_crash_at_input_does_not_reopen_prompt(){
        let mut c=InputClient::new(None);c.send(py("unknown","input('unanswered')"));
        let p=c.until("input_prompt",None);let home=c.home.clone();
        c.child.kill().unwrap();c.child.wait().unwrap();drop(c);
        let session=std::fs::read_dir(home.join("sessions")).unwrap().next().unwrap().unwrap().path();
        let mut c=InputClient::new(Some((home,session)));
        c.send(json!({"id":"recovery","kind":"recovery"}));let r=c.until("recovery",Some("recovery"));
        assert!(r["operations"].as_array().unwrap().iter().any(|v|v["operation"]=="unknown"&&v["state"]=="unknown"&&v["replay_allowed"]==false));
        c.send(input_command("late",&p,"text",Some("late")));c.until("rejected",Some("late"));
        c.send(py("next","assert len(H.stdin)==0\nassert 'input' not in globals()"));
        assert_eq!(c.until("completed",Some("next"))["status"],"ok");let j=journal(&c.finish());
        assert_eq!(j.iter().filter(|v|v["kind"]=="input_prompt").count(),1);
        assert_eq!(j.iter().filter(|v|v["kind"]=="code"&&v["payload"]["source"]=="input('unanswered')").count(),1);
        assert!(!j.iter().any(|v|v["kind"]=="input_closed"&&v["payload"]["prompt_id"]==p["prompt_id"]));
    }
    #[test]
    fn e2e_input_cancel_is_sticky_and_ipc_survives(){
        for action in ["interrupt","cancel"]{
            let mut c=InputClient::new(None);
            c.send(py("owner","try:\n input('cancel?')\nexcept KeyboardInterrupt:\n pass\ntry:\n input('must-not-reopen')\nexcept KeyboardInterrupt:\n pass\nagent.loop.stop()"));
            let p=c.until("input_prompt",None);
            c.send(if action=="interrupt"{json!({"id":"cancel","kind":"interrupt"})}
                else{input_command("cancel",&p,"cancel",None)});
            c.until("completed",Some("cancel"));
            assert_eq!(c.until("completed",Some("owner"))["status"],"cancelled");
            c.send(input_command("late",&p,"text",Some("late")));c.until("rejected",Some("late"));
            c.send(py("next","assert H.stdin[:]==[]\nagent.say('ipc alive')"));
            assert_eq!(c.until("completed",Some("next"))["status"],"ok");
            let j=journal(&c.finish());
            assert_eq!(j.iter().filter(|v|v["kind"]=="input_prompt").count(),1);
            assert_eq!(j.iter().filter(|v|v["kind"]=="cancel").count(),1);
        }
    }
    #[test]
    fn e2e_input_cancel_does_not_start_blocking_helpers(){
        for helper in ["agent.sh('sleep 30')","agent.llm('must not dispatch')",
            "agent.llm.image('must not generate',model='openai/gpt-image-1')",
            "agent.say('must not say')","agent.context.read_text('must not select')",
            "agent.context.read_raw(b'must not attach')","agent.loop.reset_python()","agent.loop.stop()"]{
            let mut c=InputClient::new(None);
            c.send(py("owner",&format!("try:\n input()\nexcept KeyboardInterrupt:\n try:\n  {helper}\n except KeyboardInterrupt:\n  print('HELPER-CANCELLED')\n else:\n  raise AssertionError('helper executed')")));
            let p=c.until("input_prompt",None);c.send(input_command("cancel",&p,"cancel",None));
            c.until("completed",Some("cancel"));
            let done=c.until("completed",Some("owner"));
            assert_eq!(done["status"],"cancelled");assert_eq!(done["stderr"]["bytes"],0);
            let j=journal(&c.finish());
            assert_eq!(j.iter().filter(|v|v["kind"]=="stream"&&v["payload"]["collection"]=="stdout")
                .map(|v|v["payload"]["text"].as_str().unwrap()).collect::<String>(),"HELPER-CANCELLED\n");
            assert!(!j.iter().any(|v|["say","context_add","raw","request","worker_reset"].contains(&v["kind"].as_str().unwrap_or(""))));
            assert!(!j.iter().any(|v|v["kind"]=="intent"&&["shell","provider","image"].contains(&v["payload"]["type"].as_str().unwrap_or(""))));
        }
    }
    #[test]
    fn e2e_input_cancel_escalates_if_python_ignores_it(){
        let mut c=InputClient::new(None);
        c.send(py("owner","try:\n input()\nexcept KeyboardInterrupt:\n while True: pass"));
        let p=c.until("input_prompt",None);c.send(input_command("cancel",&p,"cancel",None));
        c.until("completed",Some("cancel"));let result=c.until("completed",Some("owner"));
        assert_eq!(result["status"],"cancelled");assert_eq!(result["stdout"]["complete"],false);
        c.send(py("next","assert H.stdin[:]==[]\nassert len(agent.context.items())>0"));
        assert_eq!(c.until("completed",Some("next"))["status"],"ok");
        assert!(journal(&c.finish()).iter().any(|v|v["kind"]=="worker_reset"&&v["payload"]["generation"]==1));
    }
    #[test]
    fn e2e_input_wait_drains_output_and_sigint(){
        let mut c=InputClient::new(None);
        c.send(py("owner","import os\nos.write(1,b'prefix-before-input')\ninput('wait?')"));
        c.until("input_prompt",None);
        let deadline=std::time::Instant::now()+std::time::Duration::from_secs(2);
        let mut captured=false;
        while std::time::Instant::now()<deadline{
            if journal(&c.home).iter().any(|v|v["kind"]=="stream"&&v["payload"]["text"]=="prefix-before-input"){
                captured=true;break;
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        unsafe{libc::kill(c.child.id() as i32,libc::SIGINT);}
        assert_eq!(c.until("completed",Some("owner"))["status"],"cancelled");
        assert!(captured,"waiting for input must not suspend stream commits");
        c.send(py("next","assert H.stdout[0]=='prefix-before-input'\nassert len(H.stdin)==0"));
        assert_eq!(c.until("completed",Some("next"))["status"],"ok");c.finish();
    }
    #[test]
    fn e2e_interrupt_active_python_without_replay(){
        let (ev,h)=run(vec![py("a","import time\nprint('started',flush=True)\ntime.sleep(30)"),
            json!({"id":"cancel","kind":"interrupt"}),py("b","print('alive')")]);
        assert_eq!(done(&ev,"a")["status"],"cancelled");
        assert_eq!(done(&ev,"b")["status"],"ok");
        assert_eq!(journal(&h).iter().filter(|v|v["kind"]=="code"&&v["payload"]["source"].as_str().unwrap_or("").contains("time.sleep")).count(),1);
    }


    #[test]
    fn e2e_image_generation_then_explicit_image_input(){
        use std::net::TcpListener;
        use std::io::{Read,BufRead,BufReader};
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();
        let address=listener.local_addr().unwrap();
        let png={
            let mut out=std::io::Cursor::new(Vec::new());
            image::DynamicImage::new_rgb8(2,2).write_to(&mut out,image::ImageFormat::Png).unwrap();
            base64::Engine::encode(&base64::engine::general_purpose::STANDARD,out.into_inner())
        };
        let server=std::thread::spawn(move||{
            let mut bodies=vec![];
            for step in 0..4{
                let(mut socket,_)=listener.accept().unwrap();
                let mut reader=BufReader::new(socket.try_clone().unwrap());
                let mut line=String::new();reader.read_line(&mut line).unwrap();
                let mut length=0;
                loop{line.clear();reader.read_line(&mut line).unwrap();if line=="\r\n"{break;}
                    if line.to_lowercase().starts_with("content-length:"){length=line.split(':').nth(1).unwrap().trim().parse::<usize>().unwrap();}}
                let mut bytes=vec![0;length];reader.read_exact(&mut bytes).unwrap();
                let request:Value=serde_json::from_slice(&bytes).unwrap();bodies.push(request);
                let body=if step==1{json!({"data":[{"b64_json":png}]})}else{
                    let code=match step{0=>"r=agent.llm.image('draw a square',model='openai/gpt-image-1')",
                        2=>"agent.context.read_raw(H.raw[0])",_=>"agent.loop.stop()"};
                    json!({"choices":[{"message":{"content":code}}]})
                }.to_string();
                write!(socket,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",body.len(),body).unwrap();
            }bodies
        });
        let home=std::env::temp_dir().join(format!("py-image-e2e-{}",super::now_ms()));
        std::fs::create_dir_all(&home).unwrap();
        let mut child=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap()).args(["--json","--json-input"])
            .env("PY_HOME",&home).env("OPENAI_BASE_URL",format!("http://{address}/v1")).env("OPENAI_API_KEY","fixture")
            .env("PY_MODEL","openai/fixture")
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
        writeln!(child.stdin.take().unwrap(),"{}",json!({"id":"a","kind":"submit","text":"make then inspect image"})).unwrap();
        let out=child.wait_with_output().unwrap();assert!(out.status.success());
        let bodies=server.join().unwrap();
        assert!(!bodies[2]["messages"].to_string().contains("data:image"));
        assert!(bodies[3]["messages"].to_string().contains("data:image/png;base64,"));
        assert!(journal(&home).iter().any(|v|v["kind"]=="raw"));
    }
    #[test]
    fn e2e_invalid_raw_image_is_rejected_without_attachment(){
        let(ev,h)=run(vec![py("a","agent.context.read_raw(b'not an image')")]);
        assert_eq!(done(&ev,"a")["status"],"error");
        assert!(!journal(&h).iter().any(|v|v["kind"]=="attachment"));
    }


    #[test]
    fn e2e_interactive_cli_help_prefixes_and_completion(){
        let home=std::env::temp_dir().join(format!("py-pty-e2e-{}",super::now_ms()));
        let script=r#"
import os,pty,select,subprocess,time,sys,fcntl,termios
master,slave=pty.openpty()
def controlling():
    os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
p=subprocess.Popen([sys.argv[1],'--no-model'],stdin=slave,stdout=slave,stderr=slave,
    env=dict(os.environ,PY_HOME=sys.argv[2],TERM='xterm-256color'),preexec_fn=controlling)
os.close(slave)
def collect(seconds=.15):
    end=time.monotonic()+seconds; data=b''
    while time.monotonic()<end:
        if select.select([master],[],[],.02)[0]:
            try:data+=os.read(master,65536)
            except OSError:break
    return data
data=b''
def wait_for(token,start=0):
    global data
    deadline=time.monotonic()+5
    while token not in data[start:]:
        data+=collect(.03)
        assert time.monotonic()<deadline,(token,data)
wait_for(b'\x1b[?2004h')
os.write(master,b'/he\t');wait_for(b'Fuzzy select')
os.write(master,b'\r');wait_for(b'\x1b[0J')
# First Enter selects /help from /help and /hotkeys; second submits it.
os.write(master,b'\r');wait_for(b'H stores full history')
start=len(data);os.write(master,b"@print('pty-result')\r");wait_for(b'status: ok',start)
os.write(master,b'/quit\r');data+=collect()
try:p.wait(timeout=3)
except subprocess.TimeoutExpired:p.kill();sys.stderr.buffer.write(data);raise
os.close(master)
sys.stdout.buffer.write(data)
"#;
        let out=crate::test_command("python3").args(["-c",script,
            &std::env::var("PY_HARNESS_BIN").unwrap(),home.to_str().unwrap()]).output().unwrap();
        assert!(out.status.success(),"{}",String::from_utf8_lossy(&out.stderr));
        let text=String::from_utf8_lossy(&out.stdout);
        assert!(text.contains("H stores full history"),"Tab completion/help failed: {text}");
        assert!(text.contains("pty-result"));
        assert!(journal(&home).iter().any(|v|v["kind"]=="stream"&&v["payload"]["text"]=="pty-result\n"));
    }
    #[test]
    fn e2e_interactive_input_partial_eof_and_ctrl_c(){
        let home=std::env::temp_dir().join(format!("py-pty-input-{}-{}",std::process::id(),super::now_ms()));
        let script=r#"
import os,pty,select,subprocess,time,sys,json,fcntl,termios
master,slave=pty.openpty()
def child_terminal():
 os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
p=subprocess.Popen([sys.argv[1],'--no-model'],stdin=slave,stdout=slave,stderr=slave,
 env=dict(os.environ,PY_HOME=sys.argv[2],TERM='xterm-256color'),preexec_fn=child_terminal)
os.close(slave)
transcript=bytearray();cursor=0
def until(needle, timeout=4):
 global cursor
 deadline=time.monotonic()+timeout
 while needle not in transcript[cursor:]:
  assert time.monotonic()<deadline,('waiting for',needle,bytes(transcript))
  if select.select([master],[],[],.05)[0]:
   transcript.extend(os.read(master,65536))
 cursor=transcript.index(needle,cursor)+len(needle)
def wait_prompt(text):
 # Terminal echo also contains source literals. Synchronize on the real durable
 # prompt event rather than treating echoed code as evidence of an active prompt.
 deadline=time.monotonic()+4
 while time.monotonic()<deadline:
  for name in os.listdir(os.path.join(sys.argv[2],'sessions')):
   with open(os.path.join(sys.argv[2],'sessions',name)) as f:
    for line in f:
     if not line.endswith('\n'):continue
     event=json.loads(line)
     if event['kind']=='input_prompt' and event['payload']['prompt']==text:return
  if select.select([master],[],[],.02)[0]:transcript.extend(os.read(master,65536))
 raise AssertionError(('no actual input prompt',text,bytes(transcript)))
def command(text):os.write(master,text.encode()+b'\r')
try:
 # Editor-ready reports, unlike echoed/redrawn prompt strings, cannot be
 # mistaken for the next raw editor while a canonical input is still closing.
 until(b'\x1b[?2004h')
 command('/help');until(b'\x1b[?2004h')
 command("@answer=input('interactive-name? '); print('ANSWER='+answer)")
 wait_prompt('interactive-name? ');os.write(master,'Ada🙂\n'.encode());until(b'\r\nANSWER=Ada');until(b'\x1b[?2004h')
 command("@import builtins; print('IMPORTED='+builtins.input('imported-name? '))")
 wait_prompt('imported-name? ');os.write(master,b'Grace\n');until(b'\r\nIMPORTED=Grace');until(b'\x1b[?2004h')
 command("@print('PARTIAL='+input('partial-then-newline? '))")
 wait_prompt('partial-then-newline? ');os.write(master,b'part\x04')
 time.sleep(.1);os.write(master,b'tail\n');until(b'\r\nPARTIAL=parttail');until(b'\x1b[?2004h')
 command("@print('FD0='+repr(__import__('os').read(0,32)))")
 until(b"\r\nFD0=b''");until(b'\x1b[?2004h')
 command("@print('BEFORE-INPUT',flush=True); input('partial-input? ')")
 wait_prompt('partial-input? ')
 # Canonical Ctrl-D releases partial text without a newline: poll readability
 # must not turn into a blocking read_line that swallows cancellation.
 os.write(master,b'partial\x04')
 time.sleep(.1);os.write(master,b'\x03')
 until(b'\x1b[?2004h')
 command("@print('AFTER-CANCEL'); print(H.stdin[:])")
 until(b'\r\nAFTER-CANCEL\r\n');until(b'\x1b[?2004h')
 command("@exec(\"try:\\n input('eof-input? ')\\nexcept EOFError: print('PYTHON-EOF')\")")
 wait_prompt('eof-input? ');os.write(master,b'\x04');until(b'\r\nPYTHON-EOF\r\n');until(b'\x1b[?2004h')
 command("!printf shell-interactive");until(b'shell-interactive');until(b'\x1b[?2004h')
 command('/quit');p.wait(timeout=4)
 assert p.returncode==0
finally:
 if p.poll() is None:p.kill();p.wait()
 os.close(master)
 sys.stdout.buffer.write(transcript)
"#;
        let out=crate::test_command("python3").args(["-c",script,&std::env::var("PY_HARNESS_BIN").unwrap(),
            home.to_str().unwrap()]).output().unwrap();
        assert!(out.status.success(),"PTY failed:\n{}\n{}",String::from_utf8_lossy(&out.stderr),String::from_utf8_lossy(&out.stdout));
        let j=journal(&home);
        let input:Vec<_>=j.iter().filter(|v|v["kind"]=="stdin").map(|v|v["payload"]["text"].clone()).collect();
        assert_eq!(input,vec![json!("Ada🙂"),json!("Grace"),json!("parttail")]);
        assert!(j.iter().any(|v|v["kind"]=="completion"&&v["payload"]["status"]=="cancelled"));
        assert!(j.iter().any(|v|v["kind"]=="input_closed"&&v["payload"]["action"]=="eof"));
        assert!(j.iter().any(|v|v["kind"]=="stream"&&v["payload"]["text"].as_str().unwrap_or("").contains("AFTER-CANCEL")));
    }
    #[test]
    fn e2e_forced_gate_blocks_side_effects_and_allows_stderr_read(){
        let home=std::env::temp_dir().join(format!("py-forced-e2e-{}",super::now_ms()));
        let mut child=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap())
            .args(["--json","--json-input","--no-model"])
            .env("PY_HOME",&home).env("PY_CONTEXT_LIMIT","10000")
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
        let mut stdin=child.stdin.take().unwrap();
        for c in [
            // Leave room for the now-accounted immutable system prefix while
            // crossing the forcing threshold, then permit a bounded stderr read.
            py("a","agent.context.read_text('large selected output '*520,max_chars=17000)"),
            py("b","side_effect=1"),
            py("c","agent.context.read_text(H.stderr[1][:500])"),
            py("d","print('side_effect' in globals())")
        ]{writeln!(stdin,"{}",c).unwrap();}drop(stdin);
        let out=child.wait_with_output().unwrap();assert!(out.status.success());
        let ev:Vec<Value>=String::from_utf8(out.stdout).unwrap().lines().map(|s|serde_json::from_str(s).unwrap()).collect();
        assert_eq!(done(&ev,"b")["status"],"error");
        assert_eq!(done(&ev,"c")["status"],"ok");
        assert_eq!(done(&ev,"d")["status"],"error");
        assert_eq!(done(&ev,"b")["context_usage"]["forced"],true);
    }


    #[test]
    fn e2e_steering_is_delivered_at_cell_boundary(){
        let (_,h,r)=model_run(vec!["print('first')","agent.loop.stop()"],vec![
            json!({"id":"a","kind":"submit","text":"first task"}),
            json!({"id":"s","kind":"submit","text":"steer-at-boundary","mode":"steering"})]);
        assert_eq!(r.len(),2);
        assert!(!r[0]["messages"].to_string().contains("steer-at-boundary"));
        assert!(r[1]["messages"].to_string().contains("steer-at-boundary"));
        assert!(journal(&h).iter().any(|v|v["kind"]=="queue_delivered"&&v["payload"]["command_id"]=="s"));
    }
    #[test]
    fn e2e_followup_waits_for_stop(){
        let (_,_,r)=model_run(vec!["print('first')","agent.loop.stop()","agent.loop.stop()"],vec![
            json!({"id":"a","kind":"submit","text":"first task"}),
            json!({"id":"f","kind":"submit","text":"followup-after-stop","mode":"followup"})]);
        assert!(!r[1]["messages"].to_string().contains("followup-after-stop"));
        assert!(r[2]["messages"].to_string().contains("followup-after-stop"));
    }


    #[test]
    fn e2e_terminal_ctrl_c_interrupts_active_cell(){
        let home=std::env::temp_dir().join(format!("py-pty-cancel-{}",super::now_ms()));
        let script=r#"
import os,pty,select,subprocess,time,sys,signal
master,slave=pty.openpty()
p=subprocess.Popen([sys.argv[1],'--no-model'],stdin=slave,stdout=slave,stderr=slave,
 env=dict(os.environ,PY_HOME=sys.argv[2],TERM='xterm'),start_new_session=True)
os.close(slave)
def collect(t):
 end=time.monotonic()+t;out=b''
 while time.monotonic()<end:
  if select.select([master],[],[],.02)[0]:
   try:out+=os.read(master,65536)
   except OSError:break
 return out
data=collect(.15)
os.write(master,b"@import time; print('before',flush=True); time.sleep(30)\r")
data+=collect(.15);os.kill(p.pid,signal.SIGINT);data+=collect(.2)
os.write(master,b"@print('after-cancel')\r");data+=collect(.2)
os.write(master,b"/quit\r");data+=collect(.1)
try:p.wait(timeout=3)
except subprocess.TimeoutExpired:p.kill();raise
sys.stdout.buffer.write(data)
"#;
        let out=crate::test_command("python3").args(["-c",script,
            &std::env::var("PY_HARNESS_BIN").unwrap(),home.to_str().unwrap()]).output().unwrap();
        assert!(out.status.success(),"{}",String::from_utf8_lossy(&out.stderr));
        let j=journal(&home);
        assert!(j.iter().any(|v|v["kind"]=="completion"&&v["payload"]["status"]=="cancelled"));
        assert!(j.iter().any(|v|v["kind"]=="stream"&&v["payload"]["text"]=="after-cancel\n"));
    }


    #[test]
    fn e2e_session_lock_rejects_second_writer(){
        let home=std::env::temp_dir().join(format!("py-lock-e2e-{}",super::now_ms()));
        let mut first=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap())
            .args(["--json","--json-input","--no-model"]).env("PY_HOME",&home)
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
        let mut reader=std::io::BufReader::new(first.stdout.take().unwrap());
        use std::io::BufRead;
        let mut line=String::new();reader.read_line(&mut line).unwrap();
        let ready:Value=serde_json::from_str(&line).unwrap();
        let session=ready["session"].as_str().unwrap();
        let second=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap())
            .args(["--json","--json-input","--no-model","--session",session])
            .env("PY_HOME",&home).stdin(Stdio::null()).output().unwrap();
        assert!(!second.status.success());
        assert!(String::from_utf8_lossy(&second.stderr).contains("already open"));
        writeln!(first.stdin.take().unwrap(),"{}",json!({"id":"q","kind":"quit"})).unwrap();
        assert!(first.wait().unwrap().success());
    }
    #[test]
    fn e2e_torn_tail_recovery_keeps_original_backup(){
        use std::fs::OpenOptions;
        let (_,home)=run(vec![py("a","print('persisted')")]);
        let session=std::fs::read_dir(home.join("sessions")).unwrap().next().unwrap().unwrap().path();
        writeln!(OpenOptions::new().append(true).open(&session).unwrap(),"{{broken").unwrap();
        let out=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap())
            .args(["--json","--json-input","--no-model","--session",session.to_str().unwrap()])
            .env("PY_HOME",&home).stdin(Stdio::null()).output().unwrap();
        // A terminated bad line is middle/committed corruption, not a torn tail.
        assert!(!out.status.success());
        let content=std::fs::read(&session).unwrap();
        std::fs::write(&session,&content[..content.len()-1]).unwrap();
        let out=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap())
            .args(["--json","--json-input","--no-model","--session",session.to_str().unwrap()])
            .env("PY_HOME",&home).stdin(Stdio::null()).output().unwrap();
        assert!(out.status.success(),"{}",String::from_utf8_lossy(&out.stderr));
        assert!(std::fs::read_dir(home.join("sessions")).unwrap().any(|p|
            p.unwrap().path().to_string_lossy().contains("torn-")));
    }
    #[test]
    fn e2e_worker_system_exit_does_not_exit_harness(){
        let(ev,_)=run(vec![py("a","raise SystemExit(2)"),py("b","print('alive')")]);
        assert_eq!(done(&ev,"a")["status"],"error");
        assert_eq!(done(&ev,"b")["status"],"ok");
    }
    #[test]
    fn e2e_shell_helper_is_metadata_not_payload(){
        let (_,_,r)=model_run(vec![
            "r=agent.sh(\"printf shell-private\")",
            "agent.loop.stop()"],vec![json!({"id":"a","kind":"submit","text":"do work"})]);
        assert!(!r[1]["messages"].as_array().unwrap().iter().any(|m|
            m["role"]=="user"&&m["content"].as_str().unwrap_or("").contains("shell-private")));
    }


    #[test]
    fn e2e_cancel_pending_provider_request(){
        use std::net::TcpListener;
        use std::io::{BufRead,BufReader};
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();
        let address=listener.local_addr().unwrap();
        let server=std::thread::spawn(move||{
            let(mut socket,_)=listener.accept().unwrap();
            socket.set_read_timeout(Some(std::time::Duration::from_secs(5))).unwrap();
            let mut reader=BufReader::new(socket.try_clone().unwrap());
            let mut line=String::new();reader.read_line(&mut line).unwrap();
            std::thread::sleep(std::time::Duration::from_millis(150));
            let body=json!({"choices":[{"message":{"content":"print('must-not-run')"}}]}).to_string();
            let _=write!(socket,"HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n{}",body.len(),body);
        });
        let home=std::env::temp_dir().join(format!("py-http-cancel-{}",super::now_ms()));
        let mut child=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap())
            .args(["--json","--json-input"]).env("PY_HOME",&home)
            .env("OPENAI_BASE_URL",format!("http://{address}/v1")).env("OPENAI_API_KEY","fixture")
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
        let mut stdin=child.stdin.take().unwrap();
        writeln!(stdin,"{}",json!({"id":"a","kind":"submit","text":"go"})).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(80));
        writeln!(stdin,"{}",json!({"id":"c","kind":"interrupt"})).unwrap();
        writeln!(stdin,"{}",py("b","print('worker-still-alive')")).unwrap();
        drop(stdin);
        let out=child.wait_with_output().unwrap();server.join().unwrap();
        assert!(out.status.success());
        let j=journal(&home);
        assert!(!j.iter().any(|v|v["kind"]=="code"&&v["payload"]["source"]=="print('must-not-run')"));
        assert!(j.iter().any(|v|v["kind"]=="stream"&&v["payload"]["text"]=="worker-still-alive\n"));
        assert!(j.iter().any(|v|v["kind"]=="provider_cancelled"));
    }


    #[test]
    fn e2e_configured_model_discovery_and_key_login(){
        let home=std::env::temp_dir().join(format!("py-config-e2e-{}",super::now_ms()));
        std::fs::create_dir_all(&home).unwrap();
        std::fs::write(home.join("config.json"),json!({"model":"local/custom",
            "providers":{"local":{"base_url":"http://127.0.0.1:1/v1","api":"openai-completions",
                "models":[{"id":"custom","context_limit":32000,"image_input":true}]}}}).to_string()).unwrap();
        let script=r#"
import os,subprocess,sys,json
p=subprocess.run([sys.argv[1],'--json','--json-input','--no-model'],env=dict(os.environ,PY_HOME=sys.argv[2]),
 input='\n'.join(json.dumps(x) for x in [
 {'id':'m','kind':'models'},
 {'id':'s','kind':'status'}])+'\n',text=True,capture_output=True)
print(p.stdout,end='')
assert p.returncode==0,p.stderr
"#;
        let out=crate::test_command("python3").args(["-c",script,&std::env::var("PY_HARNESS_BIN").unwrap(),
            home.to_str().unwrap()]).output().unwrap();
        assert!(out.status.success());
        let ev:Vec<Value>=String::from_utf8(out.stdout).unwrap().lines().map(|s|serde_json::from_str(s).unwrap()).collect();
        let models=ev.iter().find(|v|v["kind"]=="models").expect("models event");
        assert!(models["models"].as_array().unwrap().iter().any(|v|v["id"]=="local/custom"&&v["configured"]==true));
        let status=ev.iter().find(|v|v["kind"]=="status").unwrap();
        assert_eq!(status["model"],"local/custom");
        assert_eq!(status["context_usage"]["model_context_limit"],32000);
    }


    #[test]
    fn e2e_key_auth_is_private_and_not_in_session(){
        let home=std::env::temp_dir().join(format!("py-auth-e2e-{}",super::now_ms()));
        std::fs::create_dir_all(&home).unwrap();
        let input=json!({"id":"a","kind":"login","provider":"openai","key":"sensitive-api-key"}).to_string()+"\n"+
            &json!({"id":"b","kind":"auth"}).to_string()+"\n";
        let mut child=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap())
            .args(["--json","--json-input","--no-model"]).env("PY_HOME",&home)
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
        child.stdin.take().unwrap().write_all(input.as_bytes()).unwrap();
        let out=child.wait_with_output().unwrap();assert!(out.status.success());
        assert!(!String::from_utf8_lossy(&out.stdout).contains("sensitive-api-key"));
        let stored:Value=serde_json::from_slice(&std::fs::read(home.join("auth.json")).unwrap()).unwrap();
        assert_eq!(stored["openai"]["key"],"sensitive-api-key");
        assert!(!journal(&home).iter().any(|v|v.to_string().contains("sensitive-api-key")));
        use std::os::unix::fs::PermissionsExt;
        assert_eq!(std::fs::metadata(home.join("auth.json")).unwrap().permissions().mode()&0o777,0o600);
    }


    #[test]
    fn e2e_large_unicode_stream_counts_and_slice(){
        let(ev,h)=run(vec![py("a","import os\nos.write(1,('a'*65535+'é'+'z'*1000).encode())"),
            py("b","agent.context.read_text(H.stdout[0][65534:65538])")]);
        assert_eq!(done(&ev,"a")["stdout"]["chars"],66536);
        assert_eq!(done(&ev,"b")["status"],"ok");
        assert!(journal(&h).iter().any(|v|v["kind"]=="context_add"&&v["payload"]["text"]=="aézz"));
    }
    #[test]
    fn e2e_direct_shell_has_preview_and_usable_helper_refs(){
        let(ev,h)=run(vec![json!({"id":"s","kind":"shell","command":"printf shell-preview"}),
            py("a","r=agent.sh('printf helper-result')\nagent.context.read_text(H.stdout[r['stdout']['index']][:4000])")]);
        assert_eq!(done(&ev,"s")["status"],"ok");
        assert!(ev.iter().any(|v|v["kind"]=="preview"&&v["stdout"]["preview"]=="shell-preview"));
        assert!(journal(&h).iter().any(|v|v["kind"]=="context_add"&&v["payload"]["text"]=="helper-result"));
    }
    #[test]
    fn e2e_read_max_must_be_integer_and_nonzero(){
        let(ev,_)=run(vec![py("a","agent.context.read_text('abc',max_chars=0)"),
            py("b","agent.context.read_text('abc',max_chars=True)"),
            py("c","agent.context.read_text('abc',max_chars=2)")]);
        for id in ["a","b","c"]{assert_eq!(done(&ev,id)["status"],"error");}
    }


    #[test]
    fn e2e_copilot_device_login_keeps_tokens_out_of_history(){
        use std::net::TcpListener;
        use std::io::{BufRead,BufReader,Read};
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();
        let address=listener.local_addr().unwrap();
        let server=std::thread::spawn(move||{
            for step in 0..2{
                let(mut socket,_)=listener.accept().unwrap();
                let mut reader=BufReader::new(socket.try_clone().unwrap());
                let mut line=String::new();reader.read_line(&mut line).unwrap();
                assert!(line.contains(if step==0{"/login/device/code"}else{"/login/oauth/access_token"}));
                let mut length=0;
                loop{line.clear();reader.read_line(&mut line).unwrap();if line=="\r\n"{break;}
                    if line.to_lowercase().starts_with("content-length:"){length=line.split(':').nth(1).unwrap().trim().parse::<usize>().unwrap();}}
                let mut bytes=vec![0;length];reader.read_exact(&mut bytes).unwrap();
                let body=if step==0{
                    json!({"device_code":"secret-device-code","user_code":"ABCD",
                        "verification_uri":"https://github.com/login/device","interval":0,"expires_in":30})
                }else{json!({"access_token":"secret-oauth-token","token_type":"bearer"})}.to_string();
                write!(socket,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",body.len(),body).unwrap();
            }
        });
        let home=std::env::temp_dir().join(format!("py-device-e2e-{}",super::now_ms()));
        let mut child=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap())
            .args(["--json","--json-input","--no-model"]).env("PY_HOME",&home)
            .env("PY_GITHUB_AUTH_URL",format!("http://{address}"))
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
        writeln!(child.stdin.take().unwrap(),"{}",json!({"id":"a","kind":"login",
            "provider":"github-copilot","method":"oauth"})).unwrap();
        let out=child.wait_with_output().unwrap();server.join().unwrap();
        assert!(out.status.success());
        let text=String::from_utf8(out.stdout).unwrap();
        assert!(text.contains("ABCD"));
        assert!(!text.contains("secret-oauth-token")&&!text.contains("secret-device-code"));
        let auth:Value=serde_json::from_slice(&std::fs::read(home.join("auth.json")).unwrap()).unwrap();
        assert_eq!(auth["github-copilot"]["refresh"],"secret-oauth-token");
        assert!(!journal(&home).iter().any(|v|v.to_string().contains("secret-oauth-token")));
    }


    #[test]
    fn e2e_shell_interrupt_kills_process_group_and_keeps_worker(){
        let(ev,h)=run(vec![
            json!({"id":"s","kind":"shell","command":"sleep 0.8; printf must-not-run"}),
            json!({"id":"cancel","kind":"interrupt"}),py("b","print('alive')")]);
        assert_eq!(done(&ev,"s")["status"],"cancelled");
        assert_eq!(done(&ev,"b")["status"],"ok");
        assert!(!journal(&h).iter().any(|v|v["kind"]=="stream"&&v["payload"]["text"]=="must-not-run"));
    }
    #[test]
    fn e2e_shell_timeout_preserves_prefix_and_reaps_descendants(){
        let(ev,h)=run(vec![py("a","r=agent.sh('printf prefix; sleep 0.8; printf forbidden', timeout=0.1)\nassert r['status']=='timeout'\nagent.context.read_text(H.stdout[r['stdout']['index']])")]);
        assert_eq!(done(&ev,"a")["status"],"ok");
        assert!(journal(&h).iter().any(|v|v["kind"]=="context_add"&&v["payload"]["text"]=="prefix"));
    }


    fn provider_fixture(provider:&str,api:&str,response:Value,command:Value)->(Vec<Value>,PathBuf,String,Value){
        use std::net::TcpListener;
        use std::io::{Read,BufRead,BufReader};
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();
        let address=listener.local_addr().unwrap();
        let server=std::thread::spawn(move||{
            let(mut socket,_)=listener.accept().unwrap();
            socket.set_read_timeout(Some(std::time::Duration::from_secs(5))).unwrap();
            let mut reader=BufReader::new(socket.try_clone().unwrap());
            let mut route=String::new();reader.read_line(&mut route).unwrap();
            let mut length=0;let mut line=String::new();
            loop{line.clear();reader.read_line(&mut line).unwrap();if line=="\r\n"{break;}
                if line.to_lowercase().starts_with("content-length:"){
                    length=line.split(':').nth(1).unwrap().trim().parse::<usize>().unwrap();}}
            let mut bytes=vec![0;length];reader.read_exact(&mut bytes).unwrap();
            let request:Value=serde_json::from_slice(&bytes).unwrap();
            let body=response.to_string();
            write!(socket,"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",body.len(),body).unwrap();
            (route,request)
        });
        let home=std::env::temp_dir().join(format!("py-wire-e2e-{}",super::now_ms()));
        std::fs::create_dir_all(&home).unwrap();
        std::fs::write(home.join("config.json"),json!({"providers":{provider:{
            "base_url":format!("http://{address}"),"api":api,
            "models":[{"id":"fixture","api":api,"context_limit":100000}]}}}).to_string()).unwrap();
        let mut child=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap())
            .args(["--json","--json-input","--no-model"]).env("PY_HOME",&home)
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
        writeln!(child.stdin.take().unwrap(),"{command}").unwrap();
        let out=child.wait_with_output().unwrap();
        assert!(out.status.success(),"{}",String::from_utf8_lossy(&out.stderr));
        let events=String::from_utf8(out.stdout).unwrap().lines().map(|l|serde_json::from_str(l).unwrap()).collect();
        let(route,request)=server.join().unwrap();
        (events,home,route,request)
    }
    #[test]
    fn e2e_responses_wire_and_all_output_text_blocks(){
        let(ev,h,route,request)=provider_fixture("fixture","openai-responses",
            json!({"output":[{"type":"reasoning","summary":[]},{"type":"message","content":[
                {"type":"output_text","text":"hello "},{"type":"output_text","text":"world"}]}],
                "usage":{"input_tokens":11,"output_tokens":4,"input_tokens_details":{"cached_tokens":3}}}),
            py("a","r=agent.llm('question', model='fixture/fixture')\nassert r=='hello world'"));
        assert_eq!(done(&ev,"a")["status"],"ok");
        assert!(route.contains("/responses"));
        assert_eq!(request["input"][0]["role"],"system");
        assert_eq!(request["input"][1]["role"],"user");
        assert_eq!(request["input"][1]["content"],"question");
        assert!(request.get("tools").is_none());
        assert_eq!(journal(&h).iter().rev().find(|v|v["kind"]=="usage").unwrap()["payload"]["cache_hit_tokens"],3);
    }


    #[test]
    fn e2e_inner_llm_images_anthropic_and_google_wire(){
        let mut output=std::io::Cursor::new(Vec::new());
        image::DynamicImage::new_rgb8(2,2).write_to(&mut output,image::ImageFormat::Png).unwrap();
        let data=base64::Engine::encode(&base64::engine::general_purpose::STANDARD,output.into_inner());
        for(provider,api,response) in [
            ("fixture-a","anthropic-messages",json!({"content":[{"type":"thinking","thinking":"private"},
                {"type":"text","text":"one"},{"type":"text","text":"two"}],
                "usage":{"input_tokens":10,"output_tokens":3,"cache_read_input_tokens":2}})),
            ("fixture-g","google-generative-ai",json!({"candidates":[{"content":{"parts":[
                {"thought":true,"text":"private"},{"text":"one"},{"text":"two"}]}}],
                "usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":3,"cachedContentTokenCount":2}}))
        ]{
            let code=format!("r=agent.llm('inspect',model='{provider}/fixture',system='image instructions',images=[{{'base64':'{data}'}}])\nassert r=='onetwo'");
            let(ev,h,_,request)=provider_fixture(provider,api,response,py("a",&code));
            assert_eq!(done(&ev,"a")["status"],"ok");
            if api=="anthropic-messages"{
                assert_eq!(request["system"],"image instructions");
                assert_eq!(request["messages"][0]["content"][1]["source"]["data"],data);
                assert_eq!(request["messages"][0]["content"][1]["source"]["media_type"],"image/png");
            }else{
                assert_eq!(request["systemInstruction"]["parts"][0]["text"],"image instructions");
                assert_eq!(request["contents"][0]["parts"][1]["inlineData"]["data"],data);
            }
            assert!(request.get("tools").is_none());
            let entries=journal(&h);
            let usage=&entries.iter().rev().find(|v|v["kind"]=="usage").unwrap()["payload"];
            assert_eq!(usage["input_tokens"],10);assert_eq!(usage["cache_hit_tokens"],2);
            assert!(!entries.iter().any(|v|v["kind"]=="context_add"&&v["payload"]["text"]=="onetwo"));
        }
    }


    #[test]
    fn e2e_auth_failure_response_secret_is_not_logged(){
        use std::net::TcpListener;
        use std::io::{BufRead,BufReader,Read};
        let listener=TcpListener::bind("127.0.0.1:0").unwrap();
        let address=listener.local_addr().unwrap();
        let server=std::thread::spawn(move||{
            let(mut socket,_)=listener.accept().unwrap();
            let mut reader=BufReader::new(socket.try_clone().unwrap());
            let mut line=String::new();reader.read_line(&mut line).unwrap();
            let mut length=0;
            loop{line.clear();reader.read_line(&mut line).unwrap();if line=="\r\n"{break;}
                if line.to_lowercase().starts_with("content-length:"){
                    length=line.split(':').nth(1).unwrap().trim().parse::<usize>().unwrap();}}
            let mut bytes=vec![0;length];reader.read_exact(&mut bytes).unwrap();
            let body=json!({"error":"invalid_client","access_token":"secret-in-error-body"}).to_string();
            write!(socket,"HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",body.len(),body).unwrap();
        });
        let home=std::env::temp_dir().join(format!("py-auth-error-e2e-{}",super::now_ms()));
        let mut child=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap())
            .args(["--json","--json-input","--no-model"]).env("PY_HOME",&home)
            .env("PY_GITHUB_AUTH_URL",format!("http://{address}"))
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
        writeln!(child.stdin.take().unwrap(),"{}",json!({"id":"a","kind":"login",
            "provider":"github-copilot","method":"oauth"})).unwrap();
        let out=child.wait_with_output().unwrap();server.join().unwrap();
        let text=String::from_utf8(out.stdout).unwrap();
        assert!(text.contains("401"));
        assert!(!text.contains("secret-in-error-body"));
        assert!(!String::from_utf8_lossy(&out.stderr).contains("secret-in-error-body"));
        assert!(!journal(&home).iter().any(|v|v.to_string().contains("secret-in-error-body")));
    }


    #[test]
    fn e2e_history_readonly_and_python_slice_semantics(){
        let(ev,_)=run(vec![py("a","print('first')"),py("b","print('second')"),
            py("c","assert H.stdout[-2:]==['first\\n','second\\n']\nassert H.stdout[::-1]==['second\\n','first\\n']\nassert H.stdout[2:1]==[]\nassert H.stdout[-1]=='second\\n'"),
            py("d","H.stdout='replaced'"),py("e","H.stdout.name='stderr'"),
            py("f","H.stdout[0]='replaced'"),py("g","assert H.stdout[0]=='first\\n'")]);
        assert_eq!(done(&ev,"c")["status"],"ok");
        for id in ["d","e","f"]{assert_eq!(done(&ev,id)["status"],"error");}
        assert_eq!(done(&ev,"g")["status"],"ok");
    }


    #[test]
    fn e2e_streams_committed_before_python_and_shell_finish(){
        for shell in [false,true]{
            let home=std::env::temp_dir().join(format!("py-live-stream-{}-{shell}",super::now_ms()));
            let mut child=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap())
                .args(["--json","--json-input","--no-model"]).env("PY_HOME",&home)
                .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
            let mut input=child.stdin.take().unwrap();
            let command=if shell{json!({"id":"a","kind":"shell","command":"printf prefix; sleep 30"})}
                else{py("a","import os,time\nos.write(1,b'prefix')\ntime.sleep(30)")};
            writeln!(input,"{command}").unwrap();
            let deadline=std::time::Instant::now()+std::time::Duration::from_secs(3);
            let mut seen=false;
            while std::time::Instant::now()<deadline{
                if home.join("sessions").exists(){
                    let events=journal(&home);
                    if events.iter().any(|v|v["kind"]=="stream"&&v["payload"]["text"]=="prefix"){
                        assert!(!events.iter().any(|v|v["kind"]=="completion"||v["kind"]=="shell_completion"));
                        seen=true;break;
                    }
                }
                std::thread::sleep(std::time::Duration::from_millis(20));
            }
            writeln!(input,"{}",json!({"id":"cancel","kind":"interrupt"})).unwrap();
            drop(input);
            let out=child.wait_with_output().unwrap();
            assert!(out.status.success(),"{}",String::from_utf8_lossy(&out.stderr));
            assert!(seen,"stream must be fsynced before execution completes (shell={shell})");
            let events=journal(&home);
            assert_eq!(events.iter().filter(|v|v["kind"]=="stream"&&
                v["payload"]["collection"]=="stdout").map(|v|v["payload"]["text"].as_str().unwrap()).collect::<String>(),"prefix");
        }
    }


    #[test]
    fn e2e_crash_preserves_partial_stream_and_never_replays(){
        let home=std::env::temp_dir().join(format!("py-crash-stream-{}",super::now_ms()));
        std::fs::create_dir_all(&home).unwrap();
        let pidfile=home.join("pid");let marker=home.join("effects");
        let code=format!("import os,time\nopen({:?},'w').write(str(os.getpid()))\nopen({:?},'a').write('effect\\n')\nos.write(1,b'partial')\ntime.sleep(30)",pidfile.to_str().unwrap(),marker.to_str().unwrap());
        let mut child=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap())
            .args(["--json","--json-input","--no-model"]).env("PY_HOME",&home)
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
        writeln!(child.stdin.as_mut().unwrap(),"{}",py("crashed",&code)).unwrap();
        let deadline=std::time::Instant::now()+std::time::Duration::from_secs(3);
        let mut seen=false;
        while std::time::Instant::now()<deadline{
            if home.join("sessions").exists() && journal(&home).iter().any(|v|
                v["kind"]=="stream"&&v["payload"]["text"]=="partial"){seen=true;break;}
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        child.kill().unwrap();child.wait().unwrap();
        if let Ok(pid)=std::fs::read_to_string(&pidfile){
            unsafe{libc::kill(-pid.parse::<i32>().unwrap(),libc::SIGKILL);}
        }
        assert!(seen,"partial output wasn't durably captured");
        let session=std::fs::read_dir(home.join("sessions")).unwrap().next().unwrap().unwrap().path();
        let mut resumed=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap())
            .args(["--json","--json-input","--no-model","--session",session.to_str().unwrap()])
            .env("PY_HOME",&home).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
        let input=resumed.stdin.take().unwrap();
        let mut input=input;
        writeln!(input,"{}",json!({"id":"r","kind":"recovery"})).unwrap();
        writeln!(input,"{}",py("read","assert H.stdout[0]=='partial'\nassert 'os' not in globals()")).unwrap();
        drop(input);
        let out=resumed.wait_with_output().unwrap();assert!(out.status.success());
        let ev:Vec<Value>=String::from_utf8(out.stdout).unwrap().lines().map(|l|serde_json::from_str(l).unwrap()).collect();
        assert_eq!(done(&ev,"read")["status"],"ok");
        let recovery=ev.iter().find(|v|v["kind"]=="recovery").expect("recovery status event");
        assert!(recovery["operations"].as_array().unwrap().iter().any(|v|
            v["operation"]=="crashed"&&v["state"]=="unknown"&&v["replay_allowed"]==false));
        assert!(recovery["streams"].as_array().unwrap().iter().any(|v|
            v["operation"]=="crashed"&&v["collection"]=="stdout"&&v["complete"]==false));
        assert_eq!(std::fs::read_to_string(&marker).unwrap(),"effect\n");
        assert_eq!(journal(&home).iter().filter(|v|v["kind"]=="code"&&v["payload"]["source"]==code).count(),1);
    }


    #[test]
    fn e2e_queued_arrival_is_durable_before_dispatch_and_not_replayed(){
        let home=std::env::temp_dir().join(format!("py-queue-crash-{}",super::now_ms()));
        let mut child=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap())
            .args(["--json","--json-input","--no-model"]).env("PY_HOME",&home)
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
        let mut input=child.stdin.take().unwrap();
        writeln!(input,"{}",py("active","import os,time\nprint(os.getpid(),flush=True)\ntime.sleep(30)")).unwrap();
        writeln!(input,"{}",json!({"id":"queued","kind":"submit","text":"queued\nmultiline\ntext","mode":"followup"})).unwrap();
        let deadline=std::time::Instant::now()+std::time::Duration::from_secs(2);
        let mut seen=false;
        while std::time::Instant::now()<deadline{
            if home.join("sessions").exists(){
                let events=journal(&home);
                if events.iter().any(|v|v["kind"]=="queue_arrival"&&v["payload"]["command_id"]=="queued"){
                    assert!(events.iter().any(|v|v["kind"]=="user"&&v["payload"]["text"]=="queued\nmultiline\ntext"));
                    assert!(!events.iter().any(|v|v["kind"]=="context_add"&&v["payload"]["text"].as_str().unwrap_or("").contains("queued\nmultiline")));
                    seen=true;break;
                }
            }
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        let events=journal(&home);
        let pid=events.iter().find(|v|v["kind"]=="stream"&&v["payload"]["collection"]=="stdout")
            .and_then(|v|v["payload"]["text"].as_str()).and_then(|s|s.trim().parse::<i32>().ok());
        child.kill().unwrap();child.wait().unwrap();drop(input);
        if let Some(pid)=pid{unsafe{libc::kill(-pid,libc::SIGKILL);}}
        assert!(seen,"queued arrival must be durable while the active cell runs");
        let session=std::fs::read_dir(home.join("sessions")).unwrap().next().unwrap().unwrap().path();
        let mut resumed=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap())
            .args(["--json","--json-input","--no-model","--session",session.to_str().unwrap()])
            .env("PY_HOME",&home).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped()).spawn().unwrap();
        writeln!(resumed.stdin.take().unwrap(),"{}",json!({"id":"inspect","kind":"recovery"})).unwrap();
        let out=resumed.wait_with_output().unwrap();assert!(out.status.success());
        let ev:Vec<Value>=String::from_utf8(out.stdout).unwrap().lines().map(|s|serde_json::from_str(s).unwrap()).collect();
        let recovery=ev.iter().find(|v|v["kind"]=="recovery").unwrap();
        assert!(recovery["queues"].as_array().unwrap().iter().any(|v|v["command_id"]=="queued"&&v["state"]=="pending"));
        let events=journal(&home);
        assert_eq!(events.iter().filter(|v|v["kind"]=="user"&&v["payload"]["text"]=="queued\nmultiline\ntext").count(),1);
        assert!(!events.iter().any(|v|v["kind"]=="context_add"&&v["payload"]["text"].as_str().unwrap_or("").contains("queued\nmultiline")));
    }


    #[test]
    fn e2e_worker_crash_keeps_partial_capture_and_restarts(){
        let(ev,h)=run(vec![py("crash","import os\nos.write(1,b'last-output')\nos._exit(17)"),
            py("next","assert 'os' not in globals()\nassert H.stdout[0]=='last-output'\nassert H.stderr[0]==''"),
            json!({"id":"r","kind":"recovery"})]);
        assert_eq!(done(&ev,"crash")["status"],"worker_crashed");
        assert_eq!(done(&ev,"crash")["exit_code"],17);
        assert_eq!(done(&ev,"crash")["stdout"]["complete"],false);
        assert_eq!(done(&ev,"next")["status"],"ok");
        assert_eq!(done(&ev,"crash")["stderr"]["bytes"],0);
        let crash=ev.iter().position(|v|v["kind"]=="completed"&&v["command_id"]=="crash").unwrap();
        let notice=ev.iter().position(|v|v["kind"]=="notice").unwrap();
        assert!(notice<crash);
        let recovery=ev.iter().find(|v|v["kind"]=="recovery").unwrap();
        assert!(recovery["operations"].as_array().unwrap().iter().any(|v|
            v["operation"]=="crash"&&v["state"]=="unknown"));
        assert!(journal(&h).iter().any(|v|v["kind"]=="stream_closed"&&
            v["payload"]["operation"]=="crash"&&v["payload"]["metadata"]["complete"]==false));
    }

    fn py(id: &str, source: &str) -> Value { json!({"id":id,"kind":"python","source":source}) }
    fn done(events: &[Value], id: &str) -> Value {
        events.iter().find(|v| v["kind"]=="completed" && v["command_id"]==id)
            .unwrap_or_else(|| panic!("no completion for {id}: {events:?}")).clone()
    }
    fn journal(home: &std::path::Path) -> Vec<Value> {
        let p = std::fs::read_dir(home.join("sessions")).unwrap().next().unwrap().unwrap().path();
        std::fs::read_to_string(p).unwrap().lines().map(|l| serde_json::from_str(l).unwrap()).collect()
    }

    #[test]
    fn e2e_say_is_ui_only() {
        let (ev,h)=run(vec![py("a","agent.say('hello **user**')")]);
        let d=done(&ev,"a");
        assert_eq!(d["stdout"]["chars"],0);
        assert_eq!(d["stderr"]["chars"],0);
        assert!(ev.iter().any(|v|v["kind"]=="say"&&v["text"]=="hello **user**"));
        assert!(!journal(&h).iter().any(|v|v["kind"]=="context_add"&&v["payload"]["text"]=="hello **user**"));
    }
    #[test]
    fn e2e_persistent_python_and_no_display() {
        let (ev,h) = run(vec![py("a","x=40\nprint(x+2)"),py("b","x+2")]);
        assert_eq!(done(&ev,"a")["status"],"ok");
        assert_eq!(done(&ev,"a")["stdout"]["chars"],3);
        assert_eq!(done(&ev,"b")["stdout"]["chars"],0);
        let log=journal(&h);
        let stdout:String=log.iter().filter(|v|v["kind"]=="stream"&&v["payload"]["collection"]=="stdout")
            .filter_map(|v|v["payload"]["text"].as_str()).collect();
        assert!(stdout.contains("42\n"),"stream chunk boundaries are not line boundaries: {stdout:?}");
    }
    #[test]
    fn e2e_errors_and_explicit_slice() {
        let (ev,h)=run(vec![py("a","x=10\nif"),py("b","print('x' in globals())"),
            py("c","y=7\nraise ValueError('private-diagnostic')"),
            py("d","agent.context.read_text(H.stderr[2][:8000])\nprint(y)")]);
        assert_eq!(done(&ev,"a")["status"],"error");
        assert_eq!(done(&ev,"c")["status"],"error");
        assert_eq!(done(&ev,"d")["status"],"ok");
        let j=journal(&h);
        assert!(j.iter().any(|v|v["kind"]=="stream"&&v["payload"]["text"]=="False\n"));
        assert!(j.iter().any(|v|v["kind"]=="context_add"&&v["payload"]["text"].as_str().unwrap_or("").contains("private-diagnostic")));
        let meta=done(&ev,"c").to_string();
        assert!(!meta.contains("private-diagnostic") && !meta.contains("ValueError"));
    }
    #[test]
    fn e2e_read_limit_and_silent_success() {
        let (ev,_) = run(vec![py("a","agent.context.read_text('x'*8001)"),
            py("b","agent.context.read_text('abc')"),
            py("c","print(agent.context.usage()['rendered_chars'])")]);
        assert_eq!(done(&ev,"a")["status"],"error");
        assert_eq!(done(&ev,"b")["stdout"]["chars"],0);
        assert_eq!(done(&ev,"c")["status"],"ok");
    }
    #[test]
    fn e2e_json_duplicate_id_never_executes_twice() {
        let (ev,h)=run(vec![py("a","print('once')"),py("a","print('twice')")]);
        assert!(ev.iter().any(|v|v["kind"]=="rejected"&&v["command_id"]=="a"));
        assert!(!journal(&h).iter().any(|v|v["kind"]=="stream"&&v["payload"]["text"]=="twice\n"));
    }
    #[test]
    fn e2e_preview_twelve_lines_and_lossless_capture() {
        let (ev,h)=run(vec![py("a","import os\nos.write(1, b'line\\n'*30)\nos.write(2,b'\\xff\\x00')")]);
        let d=done(&ev,"a");
        assert_eq!(d["stdout"]["lines"],30);
        let preview=ev.iter().find(|v|v["kind"]=="preview"&&v["command_id"]=="a").unwrap();
        assert_eq!(preview["stdout"]["preview"].as_str().unwrap().lines().count(),12);
        assert_eq!(d["stderr"]["bytes"],2);
        assert!(journal(&h).iter().any(|v|v["kind"]=="stream"&&v["payload"]["base64"]=="/wA="));
    }
}

const PYTHON: &str = r####"
import os,sys,json,socket,traceback,types,ast,subprocess,codeop,keyword
# Editor requests run only between cells, never in the user's namespace. Preserve
# the builtin operations used for safe dictionary inspection before user code.
_ide_type=type
_ide_dict=dict
_ide_module=types.ModuleType
_ide_getset=types.GetSetDescriptorType
_ide_builtin=__import__('builtins')
_ide_compile=codeop.compile_command
_ide_getcwd=os.getcwd
_ide_keywords=tuple(keyword.kwlist)
_ide_str=str
_sock=socket.socket(fileno=int(sys.argv[1]))
_wire=_sock.makefile('rwb',buffering=0)
def _send(v):
    _wire.write((json.dumps(v,ensure_ascii=True)+'\n').encode())
def _recv():
    line=_wire.readline()
    if not line: raise EOFError('host disconnected')
    return json.loads(line)
_rpc_signal=__import__('signal')
_rpc_sequence=0
def _rpc(op,**kw):
    global _rpc_sequence
    _rpc_sequence+=1
    request_id=_rpc_sequence
    # Defer SIGINT across the whole wire transaction: an interrupted readline
    # can lose a partial frame, and an unread reply must never become the next
    # helper's return value. The host closes input/cancels network/shell waits;
    # escalation still uses SIGKILL if a worker refuses to cooperate.
    previous=_rpc_signal.pthread_sigmask(_rpc_signal.SIG_BLOCK,{_rpc_signal.SIGINT})
    try:
        _send(dict(kind='rpc',rpc_id=request_id,op=op,**kw))
        while True:
            v=_recv()
            if v.get('kind')!='rpc_reply':raise RuntimeError('invalid host RPC response')
            if v.get('rpc_id')==request_id:break
            # An old reply is obsolete, not a value or an execution command.
    finally:
        _rpc_signal.pthread_sigmask(_rpc_signal.SIG_SETMASK,previous)
    if not v.get('ok'):
        if v.get('exception')=='EOFError': raise EOFError()
        if v.get('exception')=='KeyboardInterrupt': raise KeyboardInterrupt()
        raise RuntimeError(v.get('error','host operation failed'))
    return v.get('value')
class _History:
    __slots__=('_name',)
    def __init__(self,name): object.__setattr__(self,'_name',name)
    def __setattr__(self,name,value): raise AttributeError('H views are read-only')
    def __delattr__(self,name): raise AttributeError('H views are read-only')
    def __getitem__(self,key):
        import operator
        if isinstance(key,slice):
            indices=range(*key.indices(len(self)))
            return [_rpc('history',collection=self._name,index=i) for i in indices]
        return _rpc('history',collection=self._name,index=operator.index(key))
    def __len__(self): return _rpc('history_len',collection=self._name)
class _H:
    __slots__=()
    def __setattr__(self,name,value): raise AttributeError('H is read-only')
    def __delattr__(self,name): raise AttributeError('H is read-only')
    def __getattr__(self,name):
        if name not in ('code','user','stdout','stderr','stdin','events','raw','say','requests','responses','usage'):
            raise AttributeError(name)
        return _History(name)
class _Context:
    def read_text(self,text,*,max_chars=8000,start=0,stop=None):
        if not isinstance(text,str): raise TypeError('read_text expects text')
        if not isinstance(max_chars,int) or isinstance(max_chars,bool) or max_chars<=0:
            raise ValueError('max_chars must be a positive integer')
        return _rpc('read_text',text=text[start:stop],max_chars=max_chars)
    def read_raw(self,data):
        import base64
        if isinstance(data,dict) and 'base64' in data:
            return _rpc('read_raw',base64=data['base64'])
        if isinstance(data,str):
            with open(data,'rb') as f: data=f.read()
        return _rpc('read_raw',base64=base64.b64encode(data).decode())
    def usage(self): return _rpc('context_usage')
    def items(self): return _rpc('context_items')
    def collapse(self,*args): raise RuntimeError('collapse must be a standalone literal call')
class _Loop:
    def stop(self, *, wakeup=None):
        if wakeup is not None:
            if not isinstance(wakeup,(tuple,list)) or len(wakeup)!=2:
                raise ValueError('wakeup must be (seconds, reason)')
            if isinstance(wakeup[0],bool) or not isinstance(wakeup[0],(int,float)):
                raise ValueError('wakeup duration must be a number')
            import math
            if not math.isfinite(wakeup[0]) or not 0 < wakeup[0] <= 604800:
                raise ValueError('wakeup duration must be finite and positive, at most 7 days')
            if not isinstance(wakeup[1],str): raise ValueError('wakeup reason must be a string')
        return _rpc('stop',wakeup=wakeup)
    def reset_python(self): _rpc('reset')
class _BgTasks:
    def run(self,source,*,kind='shell',cwd=None,env=None,timeout=None,name=None,wakeup_reason=None):
        if not isinstance(source,str) or not isinstance(kind,str): raise TypeError('source and kind must be strings')
        for value in (cwd,name,wakeup_reason):
            if value is not None and not isinstance(value,str): raise TypeError('cwd, name and wakeup_reason must be strings or None')
        if env is not None and (not isinstance(env,dict) or not all(isinstance(k,str) and isinstance(v,str) for k,v in env.items())):
            raise TypeError('env must map strings to strings')
        if timeout is not None:
            import math
            if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or not 0 < timeout <= 604800:
                raise ValueError('timeout must be finite and positive, at most 7 days')
        return _rpc('bg_run',source=source,task_kind=kind,options=dict(cwd=cwd,env=env,timeout=timeout,name=name,wakeup_reason=wakeup_reason))
    def list(self,state=None):
        if state is not None and not isinstance(state,str): raise TypeError('state must be a string or None')
        return _rpc('task_list',state=state)
    def get(self,task_id):
        if not isinstance(task_id,str): raise TypeError('task_id must be a string')
        return _rpc('task_get',task_id=task_id)
    def kill(self,task_id,*,force=False):
        if not isinstance(task_id,str) or not isinstance(force,bool): raise TypeError('task_id must be a string and force must be boolean')
        return _rpc('task_kill',task_id=task_id,force=force)
class _LLM:
    def __call__(self,prompt,**kwargs):
        import base64,io
        images=[]
        for data in kwargs.get('images',()):
            if isinstance(data,dict) and 'base64' in data:
                images.append(data); continue
            if isinstance(data,str):
                with open(data,'rb') as f: data=f.read()
            if hasattr(data,'save'):
                out=io.BytesIO(); data.save(out,format='PNG'); data=out.getvalue()
            images.append({'base64':base64.b64encode(data).decode()})
        kwargs['images']=images
        return _rpc('llm',prompt=prompt,options=kwargs)
    def list(self,**kwargs): return _rpc('models',options=kwargs)
    def image(self,prompt,**kwargs): return _rpc('image',prompt=prompt,options=kwargs)
agent=types.ModuleType('agent')
agent.context=_Context()
agent.loop=_Loop()
agent.bgtasks=_BgTasks()
agent.llm=_LLM()
agent.say=lambda text:_rpc('say',text=str(text))
agent.sh=lambda command,**kwargs:_rpc('sh',command=command,options=kwargs)
sys.modules['agent']=agent
H=_H()
_ns={'__name__':'__main__','agent':agent,'H':H}
def _input(prompt=''):
    return _rpc('input',prompt=str(prompt))
__import__('builtins').input=_input
_ns['__builtins__']=__import__('builtins')
def _path(n):
    if isinstance(n,ast.Name): return n.id
    if isinstance(n,ast.Attribute): return _path(n.value)+'.'+n.attr
    return ''
def _control(tree,forced):
    if len(tree.body)==1 and isinstance(tree.body[0],ast.Expr) and isinstance(tree.body[0].value,ast.Call):
        call=tree.body[0].value
        name=_path(call.func)
        if name=='agent.context.collapse':
            if call.keywords or len(call.args)!=3 or not all(isinstance(a,ast.Constant) and isinstance(a.value,str) for a in call.args):
                raise ValueError('collapse requires three literal strings')
            _rpc('collapse',start=call.args[0].value,end=call.args[1].value,summary=call.args[2].value,source=ast.get_source_segment(_source,call))
            return True
        if forced and name=='agent.context.read_text':
            if call.keywords or len(call.args)!=1: raise ValueError('forced read accepts one sliced stderr argument')
            node=call.args[0]; sl=None
            if isinstance(node,ast.Subscript) and isinstance(node.slice,ast.Slice):
                sl=node.slice; node=node.value
            if not isinstance(node,ast.Subscript) or _path(node.value)!='H.stderr':
                raise ValueError('only H.stderr reads allowed in forced mode')
            def integer(n):
                if n is None:return None
                if isinstance(n,ast.Constant) and type(n.value) is int and n.value>=0:return n.value
                raise ValueError('literal nonnegative integer required')
            index=integer(node.slice)
            start=integer(sl.lower) if sl else None
            stop=integer(sl.upper) if sl else None
            if sl and sl.step is not None: raise ValueError('slice steps forbidden')
            if start is not None and stop is not None and start>stop: raise ValueError('reversed slice')
            agent.context.read_text(H.stderr[index][start:stop])
            return True
    if forced: raise ValueError('forced mode allows only collapse or literal stderr read')
    if any(isinstance(n,ast.Call) and _path(n.func)=='agent.context.collapse' for n in ast.walk(tree)):
        raise ValueError('collapse must be standalone')
    return False
def _ide_dictionary(obj):
    if _ide_type(obj) is _ide_module:
        return _ide_module.__getattribute__(obj,'__dict__')
    if _ide_type(obj) is _ide_type:
        return _ide_type.__getattribute__(obj,'__dict__')
    cls=_ide_type(obj)
    if _ide_type(cls) is not _ide_type:return {}
    # Only a genuine builtin instance-dictionary descriptor may be invoked.
    # A user property called __dict__, custom metaclass, __dir__ or __getattr__
    # must never run merely because somebody pressed Tab.
    for base in _ide_type.__getattribute__(cls,'__mro__'):
        descriptor=_ide_type.__getattribute__(base,'__dict__').get('__dict__')
        if descriptor is not None:
            if _ide_type(descriptor) is _ide_getset:
                value=descriptor.__get__(obj,cls)
                if _ide_type(value) is _ide_dict:return value
            return {}
    return {}
def _ide_snapshot():
    names=set();roots=[]
    def add_dictionary(prefix,d,limit,collect=False):
        count=0
        for name in d:
            count+=1
            if count>limit or len(names)>=8192:break
            if _ide_type(name) is _ide_str and name.isidentifier():
                path=prefix+name;names.add(path)
                if collect:roots.append((path,d[name]))
    add_dictionary('',_ns,2048,True)
    add_dictionary('',_ide_module.__getattribute__(_ide_builtin,'__dict__'),1024)
    names.update(_ide_keywords)
    names.update('H.'+n for n in ('code','user','stdout','stderr','stdin','events','raw','say','requests','responses','usage'))
    for path,obj in roots:
        if len(names)>=8192:break
        if path=='__builtins__':continue  # builtin root names already included
        d=_ide_dictionary(obj);add_dictionary(path+'.',d,512)
        cls=_ide_type(obj)
        if _ide_type(cls) is _ide_type:
            for base in _ide_type.__getattribute__(cls,'__mro__'):
                add_dictionary(path+'.',_ide_type.__getattribute__(base,'__dict__'),256)
        # Two member levels cover os.path.join and agent.context.read_text.
        # Descend only through actual dictionary values, never descriptors.
        for count,name in enumerate(d):
            if count>=512 or len(names)>=8192:break
            if _ide_type(name) is _ide_str and name.isidentifier():
                child=d[name];sub=_ide_dictionary(child)
                add_dictionary(path+'.'+name+'.',sub,256)
                if path=='agent':
                    cls=_ide_type(child)
                    if _ide_type(cls) is _ide_type:
                        add_dictionary(path+'.'+name+'.',_ide_type.__getattribute__(cls,'__dict__'),256)
    return {'kind':'ide_names','names':sorted(names),'cwd':_ide_getcwd()}
_send({'kind':'ready'})
while True:
    _cmd=_recv()
    if _cmd.get('kind')=='rpc_reply':continue
    if _cmd.get('kind')=='shutdown':break
    if _cmd.get('kind')=='ide_inspect':
        try:_send(_ide_snapshot())
        except BaseException:_send({'kind':'ide_names','names':[]})
        continue
    if _cmd.get('kind')=='ide_validate':
        try:
            _incomplete=_ide_compile(_cmd['source'],'<editor>','exec') is None
        except (SyntaxError,ValueError,OverflowError):_incomplete=False
        _send({'kind':'ide_validation','incomplete':_incomplete})
        continue
    _source=_cmd['source']
    _out=os.open(_cmd['stdout'],os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    _err=os.open(_cmd['stderr'],os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    os.dup2(_out,1);os.dup2(_err,2);os.close(_out);os.close(_err)
    _status='ok'
    _send({'kind':'started'})
    try:
        _tree=ast.parse(_source,filename='<agent-cell>',mode='exec')
        _compiled=compile(_tree,'<agent-cell>','exec')
        if not _control(_tree,_cmd.get('forced',False)):exec(_compiled,_ns,_ns)
    except BaseException:
        _status='error'
        traceback.print_exc()
    finally:
        sys.stdout.flush();sys.stderr.flush()
    _send({'kind':'done','status':_status})
"####;

use serde_json::{Value, json};
use std::{fs::{self,File,OpenOptions}, io::{self,Write,BufRead,BufReader},
    path::{Path,PathBuf}, process::{Command,Child}, collections::{HashMap,HashSet},
    os::unix::{net::UnixStream,io::AsRawFd,fs::OpenOptionsExt,process::CommandExt}};
use base64::{Engine as _,engine::general_purpose::STANDARD as B64};

type Result<T> = std::result::Result<T,Box<dyn std::error::Error>>;
#[derive(Clone)]
struct Item { id:String, role:String, text:String, output:bool, ranges:Vec<(usize,usize)> }

struct Journal { file:File, offsets:Vec<(u64,u64)>, path:PathBuf, seq:usize, failed:bool }
impl Journal {
    fn open(path:PathBuf)->Result<Self>{
        use std::io::Seek;
        let file=OpenOptions::new().create(true).read(true).append(true).mode(0o600).open(&path)?;
        if unsafe{libc::flock(file.as_raw_fd(),libc::LOCK_EX|libc::LOCK_NB)}!=0 {
            return Err("session is already open".into());
        }
        let mut reader=BufReader::new(File::open(&path)?);
        let mut offsets=vec![];let mut position=0u64;let mut line=Vec::new();
        loop{
            line.clear();let size=reader.read_until(b'\n',&mut line)?;
            if size==0{break;}
            if !line.ends_with(b"\n"){
                let backup=path.with_extension(format!("torn-{}.jsonl",std::process::id()));
                fs::copy(&path,backup)?;file.set_len(position)?;file.sync_all()?;break;
            }
            let value:Value=serde_json::from_slice(&line)?;
            if value["seq"].as_u64()!=Some(offsets.len() as u64){return Err("invalid journal sequence".into());}
            offsets.push((position,size as u64));position+=size as u64;
        }
        file.sync_all()?;
        File::open(path.parent().ok_or("journal directory missing")?)?.sync_all()?;
        let seq=offsets.len();
        let _=reader.seek(std::io::SeekFrom::Start(0));
        Ok(Self{file,offsets,path,seq,failed:false})
    }
    fn event(&self,index:usize)->Result<Value>{
        use std::io::{Read,Seek,SeekFrom};
        let &(offset,size)=self.offsets.get(index).ok_or("history event out of range")?;
        let mut file=File::open(&self.path)?;file.seek(SeekFrom::Start(offset))?;
        let mut bytes=vec![0;size as usize];file.read_exact(&mut bytes)?;
        Ok(serde_json::from_slice(&bytes)?)
    }
    fn append(&mut self,kind:&str,payload:Value)->Result<usize>{
        if self.failed{return Err("session journal write previously failed; restart and recover before continuing".into());}
        let n=self.seq;
        let v=json!({"schema_version":1,"seq":n,"kind":kind,"payload":payload,
            "session_id":self.path.file_stem().unwrap().to_string_lossy(),
            "timestamp_ms":now_ms()});
        let bytes=format!("{v}\n").into_bytes();
        let write=(||->io::Result<u64>{
            let offset=self.file.metadata()?.len();
            self.file.write_all(&bytes)?;self.file.flush()?;self.file.sync_all()?;Ok(offset)
        })();
        let offset=match write{Ok(offset)=>offset,Err(e)=>{self.failed=true;return Err(e.into());}};
        self.offsets.push((offset,bytes.len() as u64));self.seq+=1;Ok(n)
    }
}
fn now_ms()->u128 {std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_millis()}

// Captures bounded chunks as execution proceeds; payload lives only in the journal.
// A lazy index permits nested helper commands without preallocating empty streams.
struct Capture {
    file:File, name:String, operation:String, index:Option<usize>,
    bytes:usize, newlines:usize, chars:Option<usize>, last:Option<u8>,
    preview:Vec<u8>, preview_lines:usize, carry:Vec<u8>, chunk:usize,
}
impl Capture {
    fn open(path:&Path,name:&str,operation:&str)->Result<Self>{
        Ok(Self{file:File::open(path)?,name:name.into(),operation:operation.into(),
            index:None,bytes:0,newlines:0,chars:Some(0),last:None,
            preview:vec![],preview_lines:0,carry:vec![],chunk:0})
    }
    fn commit(&mut self,host:&mut Host,bytes:&[u8])->Result<()>{
        let index=*self.index.get_or_insert_with(||host.history[&self.name].len());
        let seq=host.journal.append("stream",json!({"collection":self.name,"index":index,
            "operation":self.operation,"base64":B64.encode(bytes),
            "text":String::from_utf8_lossy(bytes),"chunk":self.chunk}))?;
        let list=host.history.get_mut(&self.name).ok_or("missing stream collection")?;
        if list.len()==index{list.push(json!({"$chunks":[]}));}
        list[index]["$chunks"].as_array_mut().ok_or("invalid stream history")?.push(json!(seq));
        self.chunk+=1;self.bytes+=bytes.len();
        self.newlines+=bytes.iter().filter(|&&b|b==b'\n').count();
        if !bytes.is_empty(){self.last=bytes.last().copied();}
        if let Some(count)=self.chars{
            self.carry.extend_from_slice(bytes);
            match std::str::from_utf8(&self.carry){
                Ok(s)=>{self.chars=Some(count+s.chars().count());self.carry.clear();},
                Err(e)=>{
                    let prefix=std::str::from_utf8(&self.carry[..e.valid_up_to()])?;
                    self.chars=if e.error_len().is_some(){None}else{Some(count+prefix.chars().count())};
                    self.carry=self.carry[e.valid_up_to()..].to_vec();
                }
            }
        }
        for &b in bytes{
            if self.preview_lines>=12||self.preview.len()>=8000{break;}
            self.preview.push(b);if b==b'\n'{self.preview_lines+=1;}
        }
        Ok(())
    }
    fn drain(&mut self,host:&mut Host)->Result<bool>{
        use std::io::Read;
        let mut buffer=[0u8;65536];
        // Bound each poll's work, even when a child continuously writes.
        for _ in 0..4{
            let size=self.file.read(&mut buffer)?;
            if size==0{return Ok(false);}
            self.commit(host,&buffer[..size])?;
        }
        Ok(true)
    }
    fn finish(mut self,host:&mut Host,complete:bool)->Result<Value>{
        while self.drain(host)?{}
        self.finish_metadata(host,complete)
    }
    fn finish_metadata(mut self,host:&mut Host,complete:bool)->Result<Value>{
        if self.index.is_none(){self.commit(host,&[])?;}
        if !self.carry.is_empty(){self.chars=None;}
        let index=self.index.ok_or("missing capture index")?;
        let lines=self.newlines+usize::from(self.last.is_some()&&self.last!=Some(b'\n'));
        let metadata=json!({"ref":format!("H.{}[{index}]",self.name),"index":index,
            "bytes":self.bytes,"chars":self.chars,"lines":lines,
            "preview":String::from_utf8_lossy(&self.preview).lines().take(12).collect::<Vec<_>>().join("\n"),
            "omitted_lines":lines.saturating_sub(12),"complete":complete});
        let mut durable=metadata.clone();durable.as_object_mut().unwrap().remove("preview");
        host.journal.append(if complete{"stream_complete"}else{"stream_closed"},json!({"collection":self.name,"operation":self.operation,
            "metadata":durable}))?;
        Ok(metadata)
    }
}

struct Worker { child:Child, reader:BufReader<UnixStream>, writer:UnixStream, dir:PathBuf }
impl Worker {
    fn spawn(home:&Path,generation:usize)->Result<Self>{
        let dir=home.join(format!("worker-{}-{}-{}",std::process::id(),generation,unique_id()));
        fs::create_dir_all(&dir)?;
        let script=dir.join("worker.py");fs::write(&script,PYTHON)?;
        let (host,child_socket)=UnixStream::pair()?;
        let fd=child_socket.as_raw_fd();
        let mut command=Command::new(std::env::var("PY_PYTHON").unwrap_or_else(|_|"python3".into()));
        command.args(["-u",script.to_str().unwrap(),&fd.to_string()])
            .stdin(std::process::Stdio::null());
        unsafe {command.pre_exec(move || {
            if libc::setsid()<0 {return Err(io::Error::last_os_error());}
            if libc::fcntl(fd,libc::F_SETFD,0)<0 {return Err(io::Error::last_os_error());}
            Ok(())
        });}
        let child=command.spawn()?;drop(child_socket);
        let writer=host.try_clone()?;
        let mut worker=Self{child,reader:BufReader::new(host),writer,dir};
        if worker.recv()?["kind"]!="ready" {return Err("worker did not initialize".into());}
        Ok(worker)
    }
    fn send(&mut self,v:&Value)->Result<()> {writeln!(self.writer,"{}",v)?;Ok(())}
    fn reply(&mut self,id:&Value,mut response:Value)->Result<()>{
        response["kind"]=json!("rpc_reply");response["rpc_id"]=id.clone();self.send(&response)
    }
    fn recv(&mut self)->Result<Value>{
        let mut line=String::new();
        if self.reader.read_line(&mut line)?==0{return Err("Python worker disconnected".into());}
        Ok(serde_json::from_str(&line)?)
    }
}
impl Drop for Worker {
    fn drop(&mut self){let _=self.send(&json!({"kind":"shutdown"}));
        unsafe{libc::kill(-(self.child.id() as i32),libc::SIGKILL);}
        let _=self.child.kill();
        let _=self.child.wait();let _=fs::remove_dir_all(&self.dir);}
}
#[derive(Clone,Copy,Debug,PartialEq,Eq)]
enum UiState{Idle,Thinking,Running,Input,Login}
impl UiState{
    fn label(self)->&'static str{match self{Self::Idle=>"idle",Self::Thinking=>"thinking",Self::Running=>"running",Self::Input=>"input",Self::Login=>"login"}}
}
struct Host {
    state:UiState,thinking_model:Option<String>,cells:usize,active_cell:Option<usize>,cancel_revision:u64,
    journal:Journal, worker:Worker, home:PathBuf, context:Vec<Item>,
    history:HashMap<String,Vec<Value>>, ids:HashSet<String>, json:bool,
    stop:bool, stop_wakeup:Option<(f64,String)>, reset:bool, reset_explicit:bool, generation:usize, revision:usize,
    bg_tasks:HashMap<String,BgTask>, wakeups:HashMap<String,Wakeup>, servicing:bool,
    incoming:Option<std::sync::mpsc::Receiver<Value>>, input_closed:bool, pending:std::collections::VecDeque<Value>,
    attachments:HashMap<String,usize>, queued:HashMap<String,Option<(usize,usize)>>, config:Value, config_defaults:Value, auth:Value,
    model:String, effort:String, no_model:bool, context_limit:usize,
    trigger:usize,retain:usize,current_code:Option<String>, usage:Value,
    skills:Value, catalog:Value, initializing:bool, startup_ready:bool,
}
impl Host {
    fn event(&self,kind:&str,payload:Value) {
        let mut v=payload;v["kind"]=json!(kind);v["schema_version"]=json!(1);
        if self.json {println!("{}",v);} else {
            match kind {
                "state"=>for line in terminal_state(&v,terminal_width()){println!("{line}");},
                "cell_start"=>for line in terminal_cell_start(&v,terminal_width()){println!("{line}");},
                "cell_end"=>for line in terminal_cell_end(&v,terminal_width()){println!("{line}");},
                "say"=>for line in terminal_markdown(v["text"].as_str().unwrap_or(""),terminal_width()){println!("{line}");},
                "input_prompt"=>{print!("{}",terminal_wrap(v["prompt"].as_str().unwrap_or(""),terminal_width()).0.join("\n"));let _=io::stdout().flush();},
                "preview"=>for line in terminal_preview(&v,terminal_width()){println!("{line}");},
                "rejected"|"error"=>for line in terminal_wrap(v["error"].as_str().unwrap_or("operation rejected"),terminal_width()).0{eprintln!("{}",terminal_styled_for(&line,"31",2));},
                "notice"=>for line in terminal_wrap(v["text"].as_str().unwrap_or(""),terminal_width()).0{println!("{line}");},
                _=>{}
            }
        }
    }
    fn hist_push(&mut self,name:&str,value:Value)->usize{
        let value=if ["raw","requests","responses","say","usage","stdin"].contains(&name) && self.journal.seq>0{
            json!({"$event":self.journal.seq-1})
        }else{value};
        let list=self.history.entry(name.into()).or_default();let n=list.len();list.push(value);n
    }
    fn add_context(&mut self,role:&str,text:String,output:bool,ranges:Vec<(usize,usize)>)->Result<String>{
        let id=format!("c{}",self.journal.seq);
        let ranges=if ranges.is_empty(){vec![(self.journal.seq,self.journal.seq+1)]}else{ranges};
        let n=self.journal.append("context_add",json!({"id":id,"role":role,"text":text,"output":output,"ranges":ranges}))?;
        self.context.push(Item{id:id.clone(),role:role.into(),text,output,
            ranges:if ranges.is_empty(){vec![(n,n+1)]}else{ranges}});
        self.revision+=1;Ok(id)
    }
    fn chars(&self)->usize{self.system_prompt().chars().count()+512+self.context.iter().map(|i|i.text.chars().count()+64).sum::<usize>()}
    fn input_budget(&self)->usize{model_input_budget(self.context_limit,&self.reasoning_metadata(&self.model),4096)}
    fn forced(&self)->bool{self.chars().div_ceil(3)>self.input_budget()*9/10}
    fn context_usage(&self)->Value{
        json!({"context_revision":self.revision,"item_count":self.context.len(),
            "rendered_chars":self.chars(),"estimated_input_tokens":self.chars().div_ceil(3),
            "measured_last_input_tokens":self.usage["last_input_tokens"],
            "model_context_limit":self.context_limit,"reserved_output_tokens":4096,
            "input_token_budget":self.input_budget(),
            "remaining_input_tokens":self.input_budget().saturating_sub(self.chars().div_ceil(3)),
            "system_chars":self.system_prompt().chars().count(),"skills_estimated_tokens":self.skills["estimated_added_tokens"],
            "forced":self.forced(),"force_threshold":0.9,"estimator":"unicode-chars/3-v1 estimate; system/control allowance included; image budget separate"})
    }
}

impl Host {
    fn rpc(&mut self,v:&Value)->Result<Value>{
        let op=v["op"].as_str().unwrap_or("");
        if self.initializing && !["history","history_len"].contains(&op){return Err("startup entries may define/import helpers and read H, but cannot use agent bridge controls, context, input or side-effect helpers during initialization".into());}
        match op {
            "history"=>{
                let name=v["collection"].as_str().ok_or("missing collection")?;
                let len=if name=="events"{self.journal.seq}else{self.history.get(name).ok_or("unknown H collection")?.len()};
                if let Some(n)=v["index"].as_i64(){
                    let index=if n<0{len as i64+n}else{n};
                    if index<0||index as usize>=len{return Err("H index out of range".into());}
                    return self.history_value(name,index as usize);
                }
                let a=v["start"].as_u64().unwrap_or(0) as usize;
                let b=v["stop"].as_u64().unwrap_or(len as u64) as usize;
                if a>b{return Err("invalid H slice".into());}
                Ok(json!((a.min(len)..b.min(len)).map(|n|self.history_value(name,n)).collect::<Result<Vec<_>>>()?))
            },
            "history_len"=>Ok(json!(if v["collection"]=="events"{self.journal.seq}
                else{self.history.get(v["collection"].as_str().unwrap_or("")).map_or(0,Vec::len)})),
            "read_text"=>{
                let text=v["text"].as_str().ok_or("text required")?;
                let max=v["max_chars"].as_u64().ok_or("positive maximum required")? as usize;
                if max==0||text.chars().count()>max {return Err("selection exceeds max_chars".into());}
                if (self.chars()+text.chars().count()+64).div_ceil(3)>=self.input_budget() {
                    return Err("selection exceeds next-request context budget".into());
                }
                let id=self.add_context("user",text.into(),true,vec![])?;Ok(json!(id))
            },
            "context_usage"=>Ok(self.context_usage()),
            "context_items"=>Ok(json!(self.context.iter().map(|i|json!({"id":i.id,"role":i.role,
                "chars":i.text.chars().count(),"ranges":i.ranges})).collect::<Vec<_>>())),
            "say"=>{
                let text=v["text"].as_str().unwrap_or("");
                self.journal.append("say",json!({"text":text}))?;
                self.hist_push("say",json!(text));
                self.event("say",json!({"text":text}));Ok(Value::Null)
            },
            "stop"=>{
                let wakeup=match v.get("wakeup").filter(|w|!w.is_null()){
                    None=>None,
                    Some(w)=>{let values=w.as_array().filter(|a|a.len()==2).ok_or("wakeup must be [seconds, reason]")?;
                        let seconds=bg_seconds(&values[0],604800.0)?.ok_or("wakeup duration required")?;
                        let reason=bg_text(&json!({"reason":values[1]}),"reason",512)?.ok_or("wakeup reason required")?.to_owned();
                        Some((seconds,reason))}
                };
                self.stop=true;self.stop_wakeup=wakeup;Ok(Value::Null)
            },
            "bg_run"=>self.bg_run(v),
            "task_list"=>self.bg_list(v.get("state").filter(|x|!x.is_null()).map(|x|x.as_str().ok_or("state must be a string")).transpose()?),
            "task_get"=>self.bg_get(v["task_id"].as_str().ok_or("task_id required")?),
            "task_kill"=>self.bg_kill(v["task_id"].as_str().ok_or("task_id required")?,v.get("force").map(|x|x.as_bool().ok_or("force must be boolean")).transpose()?.unwrap_or(false)),
            "reset"=>{self.reset=true;self.reset_explicit=true;Ok(Value::Null)},
            "collapse"=>self.collapse(v),
            "sh"=>self.shell(v),
            "llm"=>self.inner_llm(v),
            "models"=>{self.reload_auth()?;Ok(self.models())},
            "image"=>self.image(v),
            "read_raw"=>self.read_raw(v),
            // input RPCs are serviced asynchronously in execute, not in this handler.
            _=>Err(format!("unsupported agent operation: {op}").into())
        }
    }
    fn collapse(&mut self,v:&Value)->Result<Value>{
        let a=self.context.iter().position(|i|i.id==v["start"]).ok_or("missing start boundary")?;
        let b=self.context.iter().position(|i|i.id==v["end"]).ok_or("missing end boundary")?;
        if a>=b{return Err("collapse requires ordered distinct boundaries".into());}
        let source=v["source"].as_str().ok_or("missing retained call")?;
        let mut ranges:Vec<(usize,usize)>=self.context[a..b].iter().flat_map(|i|i.ranges.clone()).collect();
        ranges.sort_unstable();
        let mut merged:Vec<(usize,usize)>=vec![];
        for (s,e) in ranges{
            if let Some(last)=merged.last_mut(){if s<=last.1{last.1=last.1.max(e);continue;}}
            merged.push((s,e));
        }
        let end=self.context[b].clone();
        let preserve=end.role=="user"||end.role=="summary";
        let mut text=format!("[Collapsed originals: {:?}]\n{}",merged,source);
        if preserve{text.push_str("\n[Preserved boundary]\n");text.push_str(&end.text);}
        let mut all_ranges=merged.clone();if preserve{all_ranges.extend(end.ranges.clone());}
        let id=self.context[a].id.clone();
        let own=self.current_code.clone();
        let mut next=self.context.clone();
        next.splice(a..=b,[Item{id:id.clone(),role:"summary".into(),text:text.clone(),output:false,ranges:all_ranges.clone()}]);
        if let Some(own)=own{next.retain(|i|i.id!=own||i.id==id);}
        let after=self.system_prompt().chars().count()+512+next.iter().map(|i|i.text.chars().count()+64).sum::<usize>();
        if after>=self.chars(){return Err("collapse must reduce rendered context size".into());}
        self.journal.append("context_replace",json!({"items":next.iter().map(item_json).collect::<Vec<_>>(),
            "summary":v["summary"],"ranges":merged}))?;
        self.context=next;self.revision+=1;Ok(Value::Null)
    }
}
fn item_json(i:&Item)->Value{json!({"id":i.id,"role":i.role,"text":i.text,"output":i.output,"ranges":i.ranges})}

fn stream_info(bytes:&[u8],name:&str,index:usize)->Value{
    let decoded=std::str::from_utf8(bytes).ok();
    let lines=bytes.iter().filter(|&&b|b==b'\n').count()+usize::from(!bytes.is_empty()&&!bytes.ends_with(b"\n"));
    let preview=String::from_utf8_lossy(bytes).lines().take(12).collect::<Vec<_>>().join("\n");
    json!({"ref":format!("H.{name}[{index}]"),"index":index,"bytes":bytes.len(),
        "chars":decoded.map(|s|s.chars().count()),"lines":lines,
        "preview":preview,"omitted_lines":lines.saturating_sub(12),"complete":true})
}
impl Host{
    fn execute(&mut self,id:&str,source:&str,retain_source:bool)->Result<Value>{
        let source_ref=format!("H.code[{}]",self.history["code"].len());
        self.with_cell("python",id,&source_ref,|host|host.execute_inner(id,source,retain_source))
    }
    fn execute_inner(&mut self,id:&str,source:&str,retain_source:bool)->Result<Value>{
        self.stop=false;self.stop_wakeup=None;self.reset=false;self.reset_explicit=false;
        let code_index=self.hist_push("code",json!(source));
        let n=self.journal.append("code",json!({"index":code_index,"source":source,
            "operation":id,"worker_generation":self.generation,"initialization":self.initializing}))?;
        self.current_code=None;
        let forced=!self.initializing&&self.forced();
        if retain_source{
            self.current_code=Some(self.add_context("assistant",source.into(),false,vec![(n,n+1)])?);
        }
        let out=self.worker.dir.join("stdout");let err=self.worker.dir.join("stderr");
        self.journal.append("intent",json!({"operation":id,"type":"python","code_index":code_index}))?;
        // Fresh capture paths ensure a pre-start crash cannot recapture a prior
        // cell's output. A worker that opens/writes them before started still has
        // its partial bytes committed below even if its handshake is lost.
        for path in [&out,&err]{OpenOptions::new().create(true).write(true).truncate(true).mode(0o600).open(path)?;}
        let sent=self.worker.send(&json!({"kind":"execute","source":source,"stdout":out,"stderr":err,"forced":forced})).is_ok();
        let mut cancelled=false;
        let mut started=false;
        let mut complete=true;
        let mut worker_exit=None;
        let mut captures:Option<(Capture,Capture)>=None;
        let mut cancel_time=None;
        let mut prompt:Option<InputPrompt>=None;
        let mut input_rpc=Value::Null;
        let status=if !sent{complete=false;self.reset=true;"worker_crashed".to_string()}else{'execution:loop{
            self.service_background()?;
            if let Some((stdout,stderr))=captures.as_mut(){
                stdout.drain(self)?;stderr.drain(self)?;
            }
            if started && self.poll_target(self.worker.child.id() as i32,
                if prompt.is_some(){0}else{libc::SIGINT})? {
                if !cancelled{cancel_time=Some(std::time::Instant::now());}cancelled=true;
            }
            if cancelled {
                if let Some(p)=prompt.take(){
                    let response=self.close_input(&p,"cancel",None)?;
                    if self.worker.reply(&input_rpc,response).is_err(){complete=false;self.reset=true;break 'execution "worker_crashed".to_string();}
                }
            }
            while let Some(pos)=self.pending.iter().position(|v|v["kind"]=="stdin_reply"){
                let command=self.pending.remove(pos).unwrap();
                if let Some(p)=&prompt{
                    if let Some((response,cancel))=self.reply_input(p,&command)?{
                        prompt=None;
                        if self.worker.reply(&input_rpc,response).is_err(){complete=false;self.reset=true;break 'execution "worker_crashed".to_string();}
                        if cancel{cancelled=true;cancel_time=Some(std::time::Instant::now());}
                    }
                }else{self.event("rejected",json!({"command_id":command["id"],"error":"no matching active input prompt"}));}
            }
            if let Some(p)=prompt.as_mut(){
                let response=if self.input_closed{Some(self.close_input(p,"eof",None)?)}
                    else if self.incoming.is_none(){self.terminal_input(p)?}else{None};
                if let Some(response)=response{
                    prompt=None;
                    if self.worker.reply(&input_rpc,response).is_err(){complete=false;self.reset=true;break 'execution "worker_crashed".to_string();}
                }
            }
            if cancel_time.is_some_and(|t:std::time::Instant|t.elapsed()>std::time::Duration::from_secs(2)){
                unsafe{libc::kill(-(self.worker.child.id() as i32),libc::SIGKILL);}
                complete=false;self.reset=true;break "cancelled".to_string();
            }
            let mut pollfd=libc::pollfd{fd:self.worker.reader.get_ref().as_raw_fd(),events:libc::POLLIN,revents:0};
            if self.worker.reader.buffer().is_empty() && unsafe{libc::poll(&mut pollfd,1,20)}<=0{continue;}
            let v=match self.worker.recv(){
                Ok(v)=>v,
                Err(_)=>{
                    complete=false;self.reset=true;
                    // A disconnected worker cannot be reused. Give an exiting process
                    // time to expose its actual exit status, then terminate its group.
                    for _ in 0..10{
                        if let Some(exit)=self.worker.child.try_wait()?{worker_exit=Some(exit);break;}
                        std::thread::sleep(std::time::Duration::from_millis(10));
                    }
                    unsafe{libc::kill(-(self.worker.child.id() as i32),libc::SIGKILL);}
                    if worker_exit.is_none(){worker_exit=Some(self.worker.child.wait()?);}
                    break if cancelled{"cancelled".to_string()}else{"worker_crashed".to_string()};
                }
            };

            if v["kind"]=="started"{
                if started{complete=false;self.reset=true;break "worker_protocol_error".to_string();}
                captures=Some((Capture::open(&out,"stdout",id)?,Capture::open(&err,"stderr",id)?));
                started=true;continue;
            }
            if v["kind"]=="done"{
                if !started{complete=false;self.reset=true;break "worker_protocol_error".to_string();}
                break if cancelled{"cancelled".to_string()}else{v["status"].as_str().unwrap_or("error").to_string()};
            }
            if v["kind"]=="rpc"{
                if !started||v["rpc_id"].as_u64().is_none(){complete=false;self.reset=true;break "worker_protocol_error".to_string();}
                let response=if cancelled{input_exception("cancel")}else{
                    if v["op"]=="input" && !self.initializing{
                        input_rpc=v["rpc_id"].clone();
                        prompt=Some(self.begin_input(id,v["prompt"].as_str().unwrap_or(""))?);
                        continue;
                    }
                    let checkpoint=self.cancel_revision;
                    let response=match self.rpc(&v){
                        Ok(value)=>json!({"ok":true,"value":value}),
                        Err(e)=>json!({"ok":false,"error":e.to_string()})
                    };
                    if self.cancel_revision!=checkpoint{
                        // A helper can consume the interrupt while Python waits
                        // on its RPC. Preserve sticky cancellation/semantic status
                        // instead of treating that request as an ordinary error.
                        cancelled=true;cancel_time=Some(std::time::Instant::now());
                        input_exception("cancel")
                    }else{response}
                };
                if self.worker.reply(&v["rpc_id"],response).is_err(){complete=false;self.reset=true;break "worker_crashed".to_string();}
                continue;
            }
            complete=false;self.reset=true;break "worker_protocol_error".to_string();
        }};
        if prompt.is_some(){self.set_state(UiState::Running,None);}
        if self.reset&&worker_exit.is_none(){
            worker_exit=self.worker.child.try_wait()?;
            if worker_exit.is_none(){unsafe{libc::kill(-(self.worker.child.id() as i32),libc::SIGKILL);}
                worker_exit=Some(self.worker.child.wait()?);}
        }
        let (stdout,stderr)=match captures{
            Some(captures)=>captures,
            None=>(Capture::open(&out,"stdout",id)?,Capture::open(&err,"stderr",id)?)
        };
        let stdout=stdout.finish(self,complete)?;
        let stderr=stderr.finish(self,complete)?;
        let code=stream_info(source.as_bytes(),"code",code_index);
        let metadata=json!({"command_id":id,"status":status,"exit_code":if let Some(exit)=worker_exit{exit.code()}else if status=="ok"{Some(0)}else{Some(1)},
            "stdout":stdout,"stderr":stderr,"code":code,"context_usage":self.context_usage()});
        let mut preview=metadata.clone();preview["cell"]=json!(self.active_cell);
        self.event("preview",preview);
        let mut metadata=metadata;
        for stream in ["code","stdout","stderr"] {
            metadata[stream].as_object_mut().unwrap().remove("preview");
        }
        self.journal.append("completion",metadata.clone())?;
        if retain_source {
            let mut observation=metadata.clone();
            for s in ["stdout","stderr","code"]{observation[s].as_object_mut().unwrap().remove("preview");}
            self.add_context("user",format!("[Execution metadata] {}",observation),false,vec![])?;
        }
        self.current_code=None;
        if status!="ok"{self.stop=false;self.stop_wakeup=None;}
        if self.reset && !self.initializing {
            if self.reset_explicit{self.reset_worker()?;}else{
                self.with_state(UiState::Running,None,|host|host.replace_worker(false))?;
            }
        }
        Ok(metadata)
    }
    fn reset_worker(&mut self)->Result<()>{
        self.with_state(UiState::Running,None,Self::reset_worker_inner)
    }
    fn reset_worker_inner(&mut self)->Result<()>{self.replace_worker(true)}
    fn replace_worker(&mut self,initialize:bool)->Result<()>{
        self.startup_ready=false;
        self.generation+=1;
        self.worker=Worker::spawn(&self.home,self.generation)?;
        let notice="Session loaded into fresh Python. Previous variables are undefined; H and context are restored.";
        self.journal.append("worker_reset",json!({"generation":self.generation}))?;
        self.add_context("user",notice.into(),false,vec![])?;
        self.event("notice",json!({"text":notice}));
        if !initialize&&self.skills["entries"].as_array().is_some_and(|entries|entries.iter().any(|e|e["core"]==true&&e["kind"]=="python")){
            self.journal.append("startup_blocked",json!({"generation":self.generation,"reason":"worker failure; core initialization requires explicit reset"}))?;
            self.event("notice",json!({"text":"Core Python was not automatically re-executed after worker failure. /reset explicitly authorizes initialization; the outer agent is blocked until then."}));
            Ok(())
        }else{self.initialize_skills()}
    }
    fn shell(&mut self,v:&Value)->Result<Value>{
        v["command"].as_str().ok_or("shell command required")?;
        let id=format!("sh{}",self.journal.seq);
        // cell_start is the only durable event before shell_inner's intent.
        let source_ref=format!("H.events[{}]['payload']['source']",self.journal.seq+1);
        self.with_cell("shell",&id,&source_ref,|host|host.shell_inner(v,&id))
    }
    fn shell_inner(&mut self,v:&Value,id:&str)->Result<Value>{
        let command=v["command"].as_str().ok_or("shell command required")?;
        let source_event=self.journal.append("intent",json!({"operation":id,"type":"shell","source":command}))?;
        let mut c=Command::new("/bin/sh");c.args(["-c",command]);
        if let Some(dir)=v["options"]["cwd"].as_str(){c.current_dir(dir);}
        if let Some(env)=v["options"]["env"].as_object(){
            for (k,v) in env {c.env(k,v.as_str().ok_or("environment value must be string")?);}
        }
        let timeout=match &v["options"]["timeout"]{
            Value::Null=>None,
            value=>{let seconds=value.as_f64().ok_or("timeout must be positive seconds")?;
                if !seconds.is_finite()||seconds<=0.0{return Err("timeout must be positive seconds".into());}
                Some(std::time::Duration::try_from_secs_f64(seconds)?)
            }
        };
        let out_path=self.home.join(format!(".{id}-stdout-{}",self.worker.child.id()));
        let err_path=self.home.join(format!(".{id}-stderr-{}",self.worker.child.id()));
        let create=|path:&Path|->std::io::Result<File>{
            use std::os::unix::fs::OpenOptionsExt;
            OpenOptions::new().write(true).create_new(true).mode(0o600).open(path)
        };
        c.stdout(create(&out_path)?).stderr(create(&err_path)?).stdin(std::process::Stdio::null());
        unsafe{c.pre_exec(||{if libc::setsid()<0{return Err(std::io::Error::last_os_error());}Ok(())});}
        let mut child=c.spawn()?;
        let pid=child.id() as i32;
        let mut stdout=Capture::open(&out_path,"stdout",id)?;
        let mut stderr=Capture::open(&err_path,"stderr",id)?;
        let began=std::time::Instant::now();
        let mut stopped=None;
        let mut state=None;
        let exit=loop{
            stdout.drain(self)?;stderr.drain(self)?;
            if self.poll_target(pid,libc::SIGTERM)?&&stopped.is_none(){
                stopped=Some(std::time::Instant::now());state=Some("cancelled");
            }
            if timeout.is_some_and(|limit|began.elapsed()>=limit)&&stopped.is_none(){
                self.journal.append("shell_timeout",json!({"operation":id}))?;
                unsafe{libc::kill(-pid,libc::SIGTERM);}
                stopped=Some(std::time::Instant::now());state=Some("timeout");
            }
            if stopped.is_some_and(|when|when.elapsed()>=std::time::Duration::from_millis(200)){
                unsafe{libc::kill(-pid,libc::SIGKILL);}
            }
            if let Some(status)=child.try_wait()?{break status;}
            std::thread::sleep(std::time::Duration::from_millis(10));
        };
        // No detached descendants may retain session output descriptors.
        unsafe{libc::kill(-pid,libc::SIGKILL);}
        let stdout=stdout.finish(self,true)?;
        let stderr=stderr.finish(self,true)?;
        fs::remove_file(&out_path)?;fs::remove_file(&err_path)?;
        let mut code=stream_info(command.as_bytes(),"events",source_event);
        code["ref"]=json!(format!("H.events[{source_event}]['payload']['source']"));
        self.event("preview",json!({"cell":self.active_cell,"command_id":id,"stdout":stdout,"stderr":stderr,
            "status":state.unwrap_or(if exit.success(){"ok"}else{"error"}),"code":code}));
        let mut result=json!({"exit_code":exit.code(),"status":state.unwrap_or(if exit.success(){"ok"}else{"error"}),
            "stdout":stdout,"stderr":stderr});
        for stream in ["stdout","stderr"]{
            result[stream].as_object_mut().unwrap().remove("preview");
        }
        let mut durable=result.clone();durable["operation"]=json!(id);
        self.journal.append("shell_completion",durable)?;Ok(result)
    }
    fn evict(&mut self)->Result<()>{
        let eligible:Vec<usize>=self.context.iter().enumerate().filter_map(|(n,i)|i.output.then_some(n)).collect();
        if eligible.len()<self.trigger{return Ok(());}
        for &n in eligible.iter().take(eligible.len().saturating_sub(self.retain)){
            let i=&mut self.context[n];
            i.text=format!("[Output omitted: originals {:?}; {} chars]",i.ranges,i.text.chars().count());
            i.output=false;
        }
        self.journal.append("context_replace",json!({"items":self.context.iter().map(item_json).collect::<Vec<_>>(),"reason":"output_batch"}))?;
        self.revision+=1;Ok(())
    }
}

impl Host {
    fn new(home:PathBuf,resume:Option<PathBuf>,json_mode:bool,no_model:bool,model_override:Option<&str>,effort_override:Option<&str>)->Result<Self>{
        use std::os::unix::fs::PermissionsExt;
        fs::create_dir_all(home.join("sessions"))?;
        fs::set_permissions(&home,fs::Permissions::from_mode(0o700))?;
        fs::set_permissions(home.join("sessions"),fs::Permissions::from_mode(0o700))?;
        let resumed=resume.is_some();
        let path=resume.unwrap_or_else(||home.join("sessions").join(format!("{}-{}-{}.jsonl",now_ms(),std::process::id(),unique_id())));
        let config_path=home.join("config.json");
        let config={
            let lock=OpenOptions::new().create(true).read(true).write(true).truncate(false).mode(0o600).open(home.join("config.lock"))?;
            if unsafe{libc::flock(lock.as_raw_fd(),libc::LOCK_EX)}!=0{return Err(io::Error::last_os_error().into());}
            if config_path.exists(){load_json(&config_path)?}else{
                let defaults=json!({"skills":skills_defaults(),"model_catalog":model_catalog_defaults()});write_private_json(&config_path,&defaults)?;defaults
            }
        };
        validate_config(&config)?;
        let mut journal=Journal::open(path)?;
        let mut snapshot=None;let mut unfinished_startup=false;let mut generation=0;
        for sequence in 0..journal.seq{
            let ev=journal.event(sequence)?;
            match ev["kind"].as_str().unwrap_or(""){
                "skills_snapshot"=>{if snapshot.is_some(){return Err("duplicate skills snapshot".into());}snapshot=Some(ev["payload"].clone());},
                "worker_reset"=>generation=ev["payload"]["generation"].as_u64().ok_or("invalid worker generation")? as usize,
                "startup_begin"|"startup_blocked"=>unfinished_startup=true,
                "startup_end"=>unfinished_startup=ev["payload"]["status"]!="ok",
                _=>{}
            }
        }
        let skills=if let Some(snapshot)=snapshot{
            if snapshot["version"]!=1||!snapshot["system"].is_string()||!snapshot["entries"].is_array(){return Err("invalid skills snapshot".into());}snapshot
        }else{
            let snapshot=if resumed{json!({"version":1,"system":SYSTEM,"entries":[],"options":{"enabled":false},"estimated_added_tokens":0,"legacy":true})}
                else{build_skills_snapshot(&home,&config)?};
            journal.append("skills_snapshot",snapshot.clone())?;snapshot
        };
        let worker=Worker::spawn(&home,if resumed{generation+1}else{0})?;
        let mut host=Self{state:UiState::Idle,thinking_model:None,cells:0,active_cell:None,cancel_revision:0,
            journal,worker,home,context:vec![],history:HashMap::new(),ids:HashSet::new(),
            json:json_mode,stop:false,stop_wakeup:None,reset:false,reset_explicit:false,generation:0,revision:0,
            bg_tasks:HashMap::new(),wakeups:HashMap::new(),servicing:false,
            incoming:None,input_closed:false,pending:std::collections::VecDeque::new(),attachments:HashMap::new(),queued:HashMap::new(),config:json!({}),config_defaults:json!({}),auth:json!({}),model:std::env::var("PY_MODEL").unwrap_or_else(|_|"openai/gpt-4.1".into()),
            effort:"medium".into(),no_model,context_limit:std::env::var("PY_CONTEXT_LIMIT").ok().and_then(|s|s.parse().ok()).unwrap_or(128000),trigger:20,retain:10,current_code:None,usage:json!({}),
            skills,catalog:empty_model_catalog(),initializing:false,startup_ready:false};
        for name in ["code","user","stdout","stderr","stdin","raw","say","requests","responses","usage"]{
            host.history.insert(name.into(),vec![]);
        }
        let mut settings=None;
        for sequence in 0..host.journal.seq {
            let ev=host.journal.event(sequence)?;
            let p=&ev["payload"];
            match ev["kind"].as_str().unwrap_or("") {
                "code"=>{host.hist_push("code",p["source"].clone());},
                "user"=>{host.hist_push("user",p["text"].clone());},
                "stdin"=>{host.history.get_mut("stdin").unwrap().push(json!({"$event":sequence}));},
                "worker_reset"=>{host.generation=p["generation"].as_u64().ok_or("invalid worker generation")? as usize;},
                "settings_initial"|"settings_change"=>{settings=Some(p.clone());},
                "task_state"|"task_settled"|"wakeup_state"=>host.bg_restore(ev["kind"].as_str().unwrap(),p)?,
                "cell_start"=>{host.cells=host.cells.max(p["cell"].as_u64().ok_or("invalid session cell number")? as usize);},
                "stream"=>{
                    let list=host.history.get_mut(p["collection"].as_str().unwrap()).ok_or("invalid stream collection")?;
                    let index=p["index"].as_u64().ok_or("missing stream index")? as usize;
                    while list.len()<=index{list.push(json!({"$chunks":[]}));}
                    list[index]["$chunks"].as_array_mut().unwrap().push(json!(sequence));
                },
                "say"=>{host.history.get_mut("say").unwrap().push(json!({"$event":sequence}));},
                "accepted"=>{if let Some(s)=p["command_id"].as_str(){host.ids.insert(s.into());}},
                "context_add"=>{
                    host.context.push(parse_item(p));host.revision+=1;
                },
                "context_replace"=>{host.context=p["items"].as_array().ok_or("invalid context journal")?
                    .iter().map(parse_item).collect();host.revision+=1;},
                "usage"=>{host.usage=p.clone();host.history.get_mut("usage").unwrap().push(json!({"$event":sequence}));},
                "request"=>{host.history.get_mut("requests").unwrap().push(json!({"$event":sequence}));},
                "response"=>{host.history.get_mut("responses").unwrap().push(json!({"$event":sequence}));},
                "raw"=>{host.history.get_mut("raw").unwrap().push(json!({"$event":sequence}));},
                "attachment"=>{host.attachments.insert(p["context_id"].as_str().unwrap().into(),p["raw_index"].as_u64().unwrap() as usize);},
                _=>{}
            }
        }
        host.config_defaults=config.clone();host.config=config;
        host.auth=load_json(&host.home.join("auth.json"))?;
        match load_model_catalog(&host.home.join("models.json")){
            Ok(cache)=>host.catalog=cache,Err(_)=>host.event("notice",json!({"text":"Invalid model catalog cache ignored; using offline inventory until refresh."}))
        }
        host.configure()?;
        if let Some(settings)=settings{
            host.set_model(settings["model"].as_str().ok_or("invalid session model")?.into())?;
            host.effort=settings["effort"].as_str().ok_or("invalid session effort")?.into();
            host.validate_effort(&host.effort)?;
        }
        if let Some(model)=model_override{host.choose_model(model)?;}
        if !resumed&&model_override.is_none()&&host.config["model"].is_null()&&std::env::var("PY_MODEL").is_err()
            &&host.auth["openai-codex"].is_object(){host.select_model_after_login("openai-codex");}
        if let Some(effort)=effort_override{host.change_effort(effort)?;}
        host.validate_effort(&host.effort)?;
        host.check_system_budget(host.context_limit)?;
        if !resumed{host.journal.append("settings_initial",json!({"model":host.model,"effort":host.effort}))?;}
        if resumed {
            host.bg_recover()?;
            host.generation+=1;
            let notice="Session loaded into fresh Python. Previous variables are undefined; H and context are restored.";
            host.journal.append("worker_reset",json!({"generation":host.generation}))?;
            host.add_context("user",notice.into(),false,vec![])?;
            host.event("notice",json!({"text":notice}));
            let recovery=host.recovery_status()?;
            if recovery["operations"].as_array().is_some_and(|ops|ops.iter().any(|v|v["state"]=="unknown")){
                host.event("notice",json!({"text":"Interrupted session has unknown operation outcomes. No execution was replayed; /recovery shows captured partial streams."}));
            }
        }
        if resumed&&unfinished_startup{
            host.event("notice",json!({"text":"Previous startup failed or has unknown side effects. Startup was not replayed. Inspect /recovery; /reset explicitly authorizes a fresh initialization attempt."}));
        }else{host.initialize_skills()?;}
        Ok(host)
    }
    fn user(&mut self,text:&str,visible:bool)->Result<usize>{
        let index=self.hist_push("user",json!(text));
        let n=self.journal.append("user",json!({"index":index,"text":text,"visible":visible}))?;
        if visible{
            let selected=text.chars().take(8000).collect::<String>();
            let rendered=format!("{}\n[{} chars; {} lines; omitted {} chars; H.user[{}]]",
                selected,text.chars().count(),text.lines().count(),text.chars().count().saturating_sub(8000),index);
            self.add_context("user",rendered,false,vec![(n,n+1)])?;
        }
        Ok(index)
    }
    fn dispatch(&mut self,v:Value)->Result<bool>{
        self.reload_auth()?;
        let id=v["id"].as_str().unwrap_or("").to_string();
        if id.is_empty(){self.event("rejected",json!({"command_id":id,"error":"command ID required"}));return Ok(true);}
        if v["kind"]=="stdin_reply"{
            self.event("rejected",json!({"command_id":id,"error":"no matching active input prompt"}));return Ok(true);
        }
        let queued=self.queued.remove(&id);
        if queued.is_none()&&self.ids.contains(&id){self.event("rejected",json!({"command_id":id,"error":"duplicate command ID"}));return Ok(true);}
        let recorded=redacted_command(v.clone())?;
        if queued.is_some(){
            self.journal.append("queue_dispatched",json!({"command_id":id,"state":"dispatched"}))?;
        }else{
            self.journal.append("accepted",json!({"command_id":id,"command":recorded}))?;
            self.ids.insert(id.clone());self.event("accepted",json!({"command_id":id}));
        }
        match v["kind"].as_str().unwrap_or("") {
            "python"=>{
                let source=v["source"].as_str().ok_or("source required")?;
                let visible=v["visible"].as_bool().unwrap_or(false);
                if queued.is_none(){self.user(source,false)?;}
                let result=self.execute(&id,source,false)?;
                if visible{
                    let message=format!("@@{}\n[stdout]\n{}\n[stderr]\n{}",source,
                        self.history_last("stdout")?,
                        self.history_last("stderr")?);
                    self.user(&message,true)?;
                }
                if self.stop{self.commit_stop()?;}
                self.event("completed",result);
            },
            "bg_run"=>{
                let result=self.bg_run(&json!({"kind":v.get("task_kind").unwrap_or(&json!("shell")),"source":v["source"],"options":v["options"]}))?;
                self.event("completed",json!({"command_id":id,"status":"ok","result":result}));
            },
            "task_list"|"task_get"|"task_logs"|"task_kill"|"wakeup_list"|"wakeup_cancel"|"wakeup_run"=>{
                self.finish_background_control(&v)?;
            },
            "shell"=>{
                let command=v["command"].as_str().ok_or("command required")?;
                if queued.is_none(){self.user(command,false)?;}
                let r=self.shell(&json!({"command":command,"options":v["options"]}))?;
                if v["visible"].as_bool().unwrap_or(false){
                    let message=format!("!!{}\n[stdout]\n{}\n[stderr]\n{}",command,
                        self.history_last("stdout")?,
                        self.history_last("stderr")?);
                    self.user(&message,true)?;
                }
                self.event("completed",json!({"command_id":id,"status":r["status"],"stdout":r["stdout"],"stderr":r["stderr"]}));
            },
            "submit"=>{
                let text=v["text"].as_str().ok_or("text required")?;
                if let Some(Some((index,sequence)))=queued{self.select_user(text,index,sequence)?;}
                else{self.user(text,true)?;}
                if !self.no_model{self.run_agent()?;}
                self.event("completed",json!({"command_id":id,"status":"ok"}));
            },
            "interrupt"=>{
                INTERRUPT.store(false,std::sync::atomic::Ordering::SeqCst);
                self.event("completed",json!({"command_id":id,"status":"ok","active":false}));
            },
            "reset"=>{self.reset_worker()?;self.event("completed",json!({"command_id":id,"status":"ok"}));},
            "login"=>{
                let provider=v["provider"].as_str().ok_or("provider required")?;
                match v["method"].as_str(){
                    Some("browser")=>self.browser_login(provider,false)?,
                    Some("manual")=>self.browser_login(provider,true)?,
                    Some("oauth"|"device")=>self.oauth_login(provider)?,
                    Some("api-key")|None=>self.key_login(provider,v["key"].as_str().ok_or("key required")?)?,
                    _=>return Err("Unsupported login method: browser, manual, device, oauth or api-key".into())
                }
                let provider=self.resolve_provider(provider)?;
                self.select_model_after_login(&provider);
                self.event("completed",json!({"command_id":id,"status":"ok"}));
            },
            "logout"=>{
                self.logout(v["provider"].as_str().ok_or("provider required")?)?;
                self.event("completed",json!({"command_id":id,"status":"ok"}));
            },
            "auth"=>self.event("auth",json!({"command_id":id,"providers":self.auth_status()})),
            "models"=>{
                let refresh=v.get("refresh").map(|r|r.as_bool().ok_or("refresh must be boolean")).transpose()?.unwrap_or(false);
                if refresh{
                    let provider=v["provider"].as_str().unwrap_or_else(||self.model.split_once('/').map_or("openai",|p|p.0)).to_string();
                    self.refresh_model_catalog(&provider,true)?;
                }
                self.event("models",json!({"command_id":id,"models":self.models()}));
            },
            "recovery"=>self.event("recovery",{
                let mut status=self.recovery_status()?;status["command_id"]=json!(id);status
            }),
            "status"=>self.event("status",self.status()),
            "context"=>self.event("context",json!({"command_id":id,"usage":self.context_usage(),
                "items":self.context.iter().map(item_json).collect::<Vec<_>>()})),
            "new"|"resume"=>{
                let cancel=v.get("cancel_tasks").map(|x|x.as_bool().ok_or("cancel_tasks must be boolean")).transpose()?.unwrap_or(false);
                let query=if v["kind"]=="resume"{Some(v["session"].as_str().filter(|s|!s.is_empty()).ok_or("session path/query required")?)}else{None};
                self.switch_session(query,cancel)?;
                self.journal.append("accepted",json!({"command_id":id,"command":v}))?;self.ids.insert(id.clone());
                self.event("completed",json!({"command_id":id,"status":"ok","session":self.journal.path}));
            },
            "quit"=>{self.bg_guard(v.get("cancel_tasks").map(|x|x.as_bool().ok_or("cancel_tasks must be boolean")).transpose()?.unwrap_or(false))?;return Ok(false);},
            _=>self.event("rejected",json!({"command_id":id,"error":"unknown command kind"}))
        }
        self.evict()?;Ok(true)
    }
}
fn parse_item(p:&Value)->Item{
    let ranges=p["ranges"].as_array().map(|a|a.iter().filter_map(|v|Some((v[0].as_u64()? as usize,v[1].as_u64()? as usize))).collect()).unwrap_or_default();
    Item{id:p["id"].as_str().unwrap_or("").into(),role:p["role"].as_str().unwrap_or("user").into(),
        text:p["text"].as_str().unwrap_or("").into(),output:p["output"].as_bool().unwrap_or(false),ranges}
}

const SYSTEM: &str = "You are a Python coding agent. Reply ONLY with complete ordinary Python source. No tools or Markdown fences. Persistent CPython exposes agent and H. H.code/user/stdout/stderr contain full history. Only metadata is automatically observed. Explicitly select payload with agent.context.read_text(H.stderr[i][:4000]) or read_raw. agent.say(text) sends a user-only UI message without stdout/context duplication. agent.sh(command) returns status and H stream refs. agent.llm(prompt,model=...) returns data; agent.llm.list() lists models; agent.llm.image(prompt,model=...) generates image data. agent.context.items()/usage() inspect context metadata. agent.loop.stop(wakeup=None) ends this turn after a successful cell; optional wakeup=(seconds,reason) schedules one continuation. agent.bgtasks.run(source,kind='shell',cwd=None,env=None,timeout=None,name=None,wakeup_reason=None) returns task metadata immediately; kind='python' uses a fresh isolated interpreter without agent or main variables. bgtasks.list(state=None),get(task_id),kill(task_id,force=False) manage jobs. Outputs stay in H; only explicit context reads select them. Completion wakeups are opt-in. Does not kill foreground Python. Collapse must be standalone agent.context.collapse('start','end','summary'); successful output is silent. Retained call contains sole summary plus original ranges. In forced mode only collapse or literal agent.context.read_text(H.stderr[44][:4000]) allowed. No replay after resume. Execution is unrestricted.";
// Reasoning request fields follow the pinned Pi provider transformations, not
// generic OpenAI-compatible guesses. This is a pure body-layout helper: it never
// reads credentials, starts I/O or changes session state. A false/absent reasoning
// flag means no fields. `off` means unset for APIs/models without verified disable
// semantics, NOT a promise that the provider's default stops internal reasoning.
fn model_input_budget(context_limit:usize,meta:&Value,reserved_output:usize)->usize{
    let combined=context_limit.saturating_sub(reserved_output);
    meta["max_input_tokens"].as_u64().map_or(combined,|cap|combined.min(cap as usize))
}
fn reasoning_fields(api:&str,provider:&str,id:&str,meta:&Value,effort:&str,max:usize)->Result<Value>{
    if !["off","minimal","low","medium","high","xhigh","max"].contains(&effort){
        return Err("effort must be off/minimal/low/medium/high/xhigh/max".into());
    }
    if max==0{return Err("max_tokens must be positive".into());}
    let mut fields=json!({});
    if meta["reasoning"]!=true{return Ok(fields);}
    let adaptive=id.contains("opus-4-6")||id.contains("opus-4.6");
    let supported=meta["reasoning_efforts"].as_array();
    if supported.is_some_and(|levels|!levels.iter().any(|level|level==effort)){
        return Err(format!("Effort {effort} unsupported by {id}; supported: {}",meta["reasoning_efforts"]).into());
    }
    if effort=="max" && !(api=="anthropic-messages"&&adaptive) && !supported.is_some_and(|levels|levels.iter().any(|level|level=="max")){
        return Err("max effort requires explicit model capability metadata (or Anthropic Messages Opus 4.6); use high or xhigh".into());
    }
    let xhigh=id.contains("gpt-5.2")||id.contains("gpt-5.3")||supported.is_some_and(|levels|levels.iter().any(|level|level=="xhigh"));
    let clamped=if effort=="xhigh"&&!xhigh{"high"}else{effort};
    match api{
        "openai-responses" if effort!="off"||supported.is_some()=>{
            fields["reasoning"]=json!({"effort":if effort=="off"{"none"}else{clamped},"summary":"auto"});
            fields["include"]=json!(["reasoning.encrypted_content"]);
        },
        "openai-completions"=>{
            let compat=&meta["compat"];
            let format=compat["thinkingFormat"].as_str().or(compat["thinking_format"].as_str())
                .unwrap_or(if provider=="zai"{"zai"}else{"openai"});
            if format=="zai"{fields["thinking"]=json!({"type":if effort=="off"{"disabled"}else{"enabled"}});}
            else if format=="qwen"{fields["enable_thinking"]=json!(effort!="off");}
            else{
                let verified=compat["supportsReasoningEffort"].as_bool()
                    .or(compat["supports_reasoning_effort"].as_bool())
                    .unwrap_or(matches!(provider,"openai"|"github-copilot"|"groq"|"cerebras"|"openrouter"));
                if verified&&effort!="off"{fields["reasoning_effort"]=json!(clamped);}
            }
        },
        "anthropic-messages" if effort!="off"=>{
            if adaptive{
                fields["thinking"]=json!({"type":"adaptive"});
                fields["output_config"]=json!({"effort":match effort{
                    "minimal"|"low"=>"low","medium"=>"medium","xhigh"|"max"=>"max",_=>"high"}});
            }else{
                // Pi's pinned adaptive test is Opus-only; Sonnet 4.6 stays on
                // its verified budget path. Reserve >=1024 output and >=1024
                // thinking tokens, refusing caps that cannot satisfy both.
                let desired=match effort{"minimal"=>1024,"low"=>2048,"medium"=>8192,_=>16384};
                let cap=meta["max_tokens"].as_u64().unwrap_or(64000).min(usize::MAX as u64) as usize;
                if cap<2048{return Err("reasoning output cap must allow 1024 thinking and 1024 output tokens".into());}
                let output=max.max(1024).saturating_add(desired).min(cap);
                let budget=desired.min(output-1024);
                fields["max_tokens"]=json!(output);
                fields["thinking"]=json!({"type":"enabled","budget_tokens":budget});
            }
        },
        "google-generative-ai"=>{
            if effort=="off"{
                // 2.5 Flash explicitly permits zero. Pro/3 families cannot
                // uniformly disable thinking, so leave their default untouched.
                if id.contains("2.5-flash"){
                    fields["thinkingConfig"]=json!({"includeThoughts":false,"thinkingBudget":0});
                }
            }else if id.contains("3-pro")||id.contains("3-flash"){
                let level=if id.contains("3-pro"){
                    if matches!(effort,"minimal"|"low"){"LOW"}else{"HIGH"}
                }else{match effort{"minimal"=>"MINIMAL","low"=>"LOW","medium"=>"MEDIUM",_=>"HIGH"}};
                fields["thinkingConfig"]=json!({"includeThoughts":true,"thinkingLevel":level});
            }else{
                let budget=if id.contains("2.5-pro")||id.contains("2.5-flash"){
                    match effort{"minimal"=>128,"low"=>2048,"medium"=>8192,
                        _=>if id.contains("2.5-pro"){32768}else{24576}}
                }else{-1};
                fields["thinkingConfig"]=json!({"includeThoughts":true,"thinkingBudget":budget});
            }
        },
        _=>{}
    }
    Ok(fields)
}
fn merge_reasoning_fields(body:&mut Value,fields:&Value){
    for (k,v) in fields.as_object().unwrap(){body[k]=v.clone();}
}
impl Host {
    fn provider_config(&self,model:&str)->Result<(String,String,String)>{
        let (provider,id)=model.split_once('/').unwrap_or(("openai",model));
        let provider=provider_alias(provider);
        let known=PROVIDERS.iter().find(|p|p.0==provider);
        let cfg=&self.config["providers"][provider];
        let mut url=cfg["base_url"].as_str()
            .or(if provider=="github-copilot"{self.auth[provider]["base_url"].as_str().or(Some("https://api.githubcopilot.com"))}else{None})
            .or(if provider=="openai-codex"{Some("https://chatgpt.com/backend-api")}else{None})
            .or(known.map(|p|p.1)).ok_or("unknown provider; configure its base_url")?.to_string();
        if provider=="openai"{if let Ok(custom)=std::env::var("OPENAI_BASE_URL"){url=custom;}}
        let env=cfg["key_env"].as_str().or(known.map(|p|p.2)).unwrap_or("");
        let mut key=self.credential(provider,env);
        if key.is_empty()&&provider=="google"{key=std::env::var("GOOGLE_API_KEY").unwrap_or_default();}
        if provider=="openai-codex"&&key.is_empty(){return Err("Codex subscription login required: /login codex".into());}
        if key.is_empty() && !url.starts_with("http://127.0.0.1") {return Err(format!("missing API credential for {provider}").into());}
        Ok((url.trim_end_matches('/').into(),key,id.into()))
    }
    fn models(&self)->Value{
        let mut models:Vec<Value>=serde_json::from_str(CATALOG).unwrap();
        // Official Codex/API model docs checked 2026-10-10. Availability remains
        // account/workspace dependent; this is offline metadata, not an access probe.
        for (id,name,aliases,off) in [
            ("gpt-6.1-sol","GPT-6.1 Sol",vec!["sol61","sol6.1"],false),
            ("gpt-6-astra","GPT-6 Astra",vec!["astra6"],false),
            ("gpt-6-sol","GPT-6 Sol",vec!["sol6"],true),
            ("gpt-6-luna","GPT-6 Luna",vec!["luna6"],true),
            ("gpt-5.6-sol","GPT-5.6 Sol",vec!["sol56","sol5.6"],true),
            ("gpt-5.6-terra","GPT-5.6 Terra",vec!["terra56","terra5.6"],true),
            ("gpt-5.6-luna","GPT-5.6 Luna",vec!["luna56","luna5.6"],true),
        ]{
            let mut efforts=vec!["low","medium","high","xhigh","max"];
            if off{efforts.insert(0,"off");}
            for (provider,api) in [("openai","openai-responses"),("openai-codex","openai-codex-responses")]{
                models.push(json!({"id":format!("{provider}/{id}"),"name":name,"aliases":aliases,"provider":provider,
                    "api":api,"context_limit":if provider=="openai-codex"{272000}else{1050000},
                    "max_input_tokens":if provider=="openai-codex"{258400}else{922000},"max_tokens":128000,
                    "image_input":true,"reasoning":true,"image_output":false,"reasoning_efforts":efforts}));
            }
        }
        for id in ["gpt-5.2","gpt-5.2-codex","gpt-5.3-codex"]{
            models.push(json!({"id":format!("openai-codex/{id}"),"name":id,"provider":"openai-codex",
                "api":"openai-codex-responses","context_limit":400000,"max_tokens":128000,
                "image_input":true,"reasoning":true,"image_output":false,"deprecated":true}));
        }
        for provider in ["openai","openai-codex"]{
            let record=&self.catalog["providers"][provider];
            if self.catalog_identity(provider).is_some_and(|identity|record["identity"]==identity){
                if let Some(discovered)=record["models"].as_array(){
                    for model in models.iter_mut().filter(|m|m["provider"]==provider){
                        model["available"]=json!(discovered.iter().any(|d|d["id"]==model["id"]));
                    }
                    for model in discovered{
                        models.retain(|m|m["id"]!=model["id"]);models.push(model.clone());
                    }
                }
            }
        }
        if let Some(providers)=self.config["providers"].as_object(){
            for (provider,cfg) in providers{
                if let Some(list)=cfg["models"].as_array(){
                    for item in list{
                        let mut item=item.clone();
                        item["id"]=json!(format!("{provider}/{}",item["id"].as_str().unwrap_or("")));
                        item["provider"]=json!(provider);item["configured"]=json!(true);item["user_declared"]=json!(true);
                        models.retain(|v|v["id"]!=item["id"]);models.push(item);
                    }
                }
            }
        }
        for item in &mut models{
            let provider=item["provider"].as_str().unwrap_or("").to_string();
            item["known"]=json!(true);
            item["configured"]=json!(self.config["providers"][&provider].is_object());
            let provider=provider_alias(&provider);
            let cfg=&self.config["providers"][provider];
            let env=cfg["key_env"].as_str().or(PROVIDERS.iter().find(|p|p.0==provider).map(|p|p.2)).unwrap_or("");
            item["credential_backed"]=json!(!self.credential(provider,env).trim().is_empty());
            item["ready"]=json!(self.provider_config(item["id"].as_str().unwrap()).is_ok()
                &&item["metadata_complete"]!=false&&(item["available"]!=false||item["user_declared"]==true));
        }
        json!(models)
    }
    fn model_api(&self,model:&str)->String{
        let (provider,id)=model.split_once('/').unwrap_or(("openai",model));
        let provider=provider_alias(provider);
        if provider=="openai-codex"{return "openai-codex-responses".into();}
        let cfg=&self.config["providers"][provider];
        if let Some(models)=cfg["models"].as_array(){
            if let Some(item)=models.iter().find(|m|m["id"]==id){
                if let Some(api)=item["api"].as_str(){return api.into();}
            }
        }
        if let Some(api)=cfg["api"].as_str(){return api.into();}
        let catalog:Vec<Value>=serde_json::from_str(CATALOG).unwrap();
        if let Some(item)=catalog.iter().find(|m|m["id"]==model){
            if let Some(api)=item["api"].as_str(){return api.into();}
        }
        match provider{"anthropic"=>"anthropic-messages","google"=>"google-generative-ai",_=>"openai-completions"}.into()
    }
    fn reasoning_metadata(&self,model:&str)->Value{
        let (provider,id)=model.split_once('/').unwrap_or(("openai",model));
        let provider=provider_alias(provider);
        let canonical=format!("{provider}/{id}");
        let mut meta=self.models().as_array().unwrap().iter().find(|m|m["id"]==canonical).cloned().unwrap_or(json!({}));
        if let Some(item)=self.config["providers"][provider]["models"].as_array()
            .and_then(|list|list.iter().find(|m|m["id"]==id)){
            if let Some(fields)=item.as_object(){for (k,v) in fields{meta[k]=v.clone();}}
        }
        meta
    }
    fn invoke(&mut self,model:&str,messages:Vec<Value>,max:usize)->Result<String>{
        self.with_state(UiState::Thinking,Some(model),|host|host.invoke_inner(model,messages,max))
    }
    fn invoke_inner(&mut self,model:&str,messages:Vec<Value>,max:usize)->Result<String>{
        let (provider,id)=model.split_once('/').unwrap_or(("openai",model));
        let api=self.model_api(model);
        let meta=self.reasoning_metadata(model);
        if meta["metadata_complete"]==false{return Err("model capabilities are incomplete; declare this model's API/context/capabilities in config.json before selecting or invoking it".into());}
        if meta["available"]==false&&meta["user_declared"]!=true{return Err("model not offered in the last account catalog; /model list refresh or select another model".into());}
        // Validate before an operation intent or network request. Unsupported
        // capability metadata is deliberately not inferred from API compatibility.
        let fields=reasoning_fields(&api,provider_alias(provider),id,&meta,&self.effort,max)?;
        if meta["max_input_tokens"].is_u64(){
            let input=serde_json::to_string(&messages)?.chars().count().div_ceil(3);
            let available=model_input_budget(self.model_limit(model)?,&meta,max);
            if input>available{return Err(format!("estimated request input {input} exceeds model input budget {available}; reduce the prompt/context before retrying").into());}
        }
        self.reload_auth()?;
        if model.starts_with("github-copilot/"){self.refresh_copilot()?;}
        let codex=model.starts_with("openai-codex/")||model.starts_with("codex/");
        if codex{self.refresh_codex()?;}
        let anthropic_oauth=provider_alias(provider)=="anthropic"&&self.auth["anthropic"]["type"]=="oauth";
        if anthropic_oauth{self.refresh_anthropic()?;}
        let (url,key,id)=self.provider_config(model)?;
        let request=json!({"model":model,"messages":messages,"max_tokens":max,"effort":self.effort});
        let operation=format!("req{}",self.journal.seq);
        self.journal.append("intent",json!({"operation":operation,"type":"provider"}))?;
        self.journal.append("request",request.clone())?;
        self.hist_push("requests",request);
        let client=reqwest::Client::builder().timeout(std::time::Duration::from_secs(180))
            .redirect(if codex||anthropic_oauth{reqwest::redirect::Policy::none()}else{reqwest::redirect::Policy::limited(10)}).build()?;
        let request_builder=if codex{
            self.codex_request(&client,&url,&key,&id,&messages)?
        }else if api=="openai-responses"{
            let mut input=messages.clone();
            for message in &mut input{
                if let Some(parts)=message["content"].as_array_mut(){
                    for part in parts{
                        match part["type"].as_str(){
                            Some("text")=>part["type"]=json!("input_text"),
                            Some("image_url")=>{
                                let url=part["image_url"]["url"].clone();
                                *part=json!({"type":"input_image","image_url":url});
                            },
                            _=>{}
                        }
                    }
                }
            }
            let mut body=json!({"model":id,"input":input,"max_output_tokens":max,"store":false});
            merge_reasoning_fields(&mut body,&fields);
            client.post(format!("{url}/responses")).bearer_auth(key).json(&body)
        }else if api=="anthropic-messages"{
            let system=messages.iter().filter(|m|m["role"]=="system").filter_map(|m|m["content"].as_str()).collect::<Vec<_>>().join("\n");
            let mut msgs:Vec<_>=messages.into_iter().filter(|m|m["role"]!="system").collect();
            for message in &mut msgs{
                if let Some(parts)=message["content"].as_array_mut(){
                    for part in parts{
                        if part["type"]=="image_url"{
                            let url=part["image_url"]["url"].as_str().ok_or("image URL missing")?;
                            let(mime,data)=image_data_url(url)?;
                            *part=json!({"type":"image","source":{"type":"base64","media_type":mime,"data":data}});
                        }
                    }
                }
            }
            let mut body=json!({"model":id,"system":system,"messages":msgs,"max_tokens":max});
            merge_reasoning_fields(&mut body,&fields);
            let mut request=client.post(format!("{url}/messages")).header("anthropic-version","2023-06-01");
            if anthropic_oauth{
                // Explicit subscription-protocol compatibility, not a change to
                // stored context or harness instructions. Announced during login.
                body["system"]=json!([
                    {"type":"text","text":ANTHROPIC_OAUTH_SYSTEM},
                    {"type":"text","text":system}
                ]);
                let mut beta="claude-code-20250219,oauth-2025-04-20,fine-grained-tool-streaming-2025-05-14".to_string();
                if body.get("thinking").is_some(){beta.push_str(",interleaved-thinking-2025-05-14");}
                request=request.bearer_auth(key).header("anthropic-beta",beta)
                    .header("x-app","cli").header("User-Agent","claude-cli/2.1.2 (external, cli) py-rust/0.1.0")
                    .header("Accept","application/json");
            }else{
                request=request.header("x-api-key",key);
                if body.get("thinking").is_some(){request=request.header("anthropic-beta","interleaved-thinking-2025-05-14");}
            }
            request.json(&body)
        }else if api=="google-generative-ai"{
            let system=messages.iter().filter(|m|m["role"]=="system").filter_map(|m|m["content"].as_str()).collect::<Vec<_>>().join("\n");
            let mut contents=Vec::new();
            for message in messages.iter().filter(|m|m["role"]!="system"){
                let mut parts=Vec::new();
                if let Some(text)=message["content"].as_str(){parts.push(json!({"text":text}));}
                else if let Some(items)=message["content"].as_array(){
                    for item in items{
                        if item["type"]=="text"{parts.push(json!({"text":item["text"]}));}
                        else if item["type"]=="image_url"{
                            let(mime,data)=image_data_url(item["image_url"]["url"].as_str().ok_or("image URL missing")?)?;
                            parts.push(json!({"inlineData":{"mimeType":mime,"data":data}}));
                        }
                    }
                }
                contents.push(json!({"role":if message["role"]=="assistant"{"model"}else{"user"},"parts":parts}));
            }
            let mut body=json!({"contents":contents,"systemInstruction":{"parts":[{"text":system}]},
                "generationConfig":{"maxOutputTokens":max}});
            if let Some(config)=fields.get("thinkingConfig"){body["generationConfig"]["thinkingConfig"]=config.clone();}
            client.post(format!("{url}/models/{id}:generateContent")).header("x-goog-api-key",key).json(&body)
        }else{
            let mut body=json!({"model":id,"messages":messages,"max_tokens":max});
            merge_reasoning_fields(&mut body,&fields);
            client.post(format!("{url}/chat/completions")).bearer_auth(key).json(&body)
        };
        let body=if codex{self.codex_sse(request_builder,&operation)?}else{self.http_json(request_builder,&operation)?};
        self.journal.append("response",json!({"operation":operation,"status":200,"body":body}))?;
        self.hist_push("responses",body.clone());
        self.journal.append("operation_complete",json!({"operation":operation,"type":"provider"}))?;
        let raw=&body["usage"];
        let input=raw["prompt_tokens"].as_u64().or(raw["input_tokens"].as_u64())
            .or(body["usageMetadata"]["promptTokenCount"].as_u64());
        let output=raw["completion_tokens"].as_u64().or(raw["output_tokens"].as_u64())
            .or(body["usageMetadata"]["candidatesTokenCount"].as_u64());
        let cache=raw["prompt_tokens_details"]["cached_tokens"].as_u64()
            .or(raw["input_tokens_details"]["cached_tokens"].as_u64()).or(raw["cache_read_input_tokens"].as_u64())
            .or(body["usageMetadata"]["cachedContentTokenCount"].as_u64());
        let cumulative=|name:&str,amount:Option<u64>|->Value{
            match amount{Some(n)=>json!(self.usage[name].as_u64().unwrap_or(0)+n),None=>Value::Null}
        };
        self.usage=json!({"input_tokens":cumulative("input_tokens",input),"output_tokens":cumulative("output_tokens",output),
            "cache_hit_tokens":cumulative("cache_hit_tokens",cache),"last_input_tokens":input,"operation":operation});
        self.journal.append("usage",self.usage.clone())?;self.hist_push("usage",self.usage.clone());
        let text=if let Some(text)=body["choices"][0]["message"]["content"].as_str(){
            text.to_string()
        }else if let Some(output)=body["output"].as_array(){
            output.iter().filter(|m|m["type"]=="message").filter_map(|m|m["content"].as_array())
                .flatten().filter(|p|p["type"]=="output_text").filter_map(|p|p["text"].as_str()).collect::<String>()
        }else if let Some(parts)=body["content"].as_array(){
            parts.iter().filter(|p|p["type"]=="text").filter_map(|p|p["text"].as_str()).collect::<String>()
        }else if let Some(parts)=body["candidates"][0]["content"]["parts"].as_array(){
            parts.iter().filter(|p|p["thought"]!=true).filter_map(|p|p["text"].as_str()).collect::<String>()
        }else{return Err("provider response contains no text".into());};
        if text.is_empty(){return Err("provider response contains no text".into());}
        Ok(text)
    }
    fn inner_llm(&mut self,v:&Value)->Result<Value>{
        let model=v["options"]["model"].as_str().unwrap_or(&self.model).to_string();
        let prompt=v["prompt"].as_str().ok_or("prompt required")?;
        let max=v["options"]["max_tokens"].as_u64().unwrap_or(2048) as usize;
        let mut parts=vec![json!({"type":"text","text":prompt})];
        if let Some(images)=v["options"]["images"].as_array(){
            if images.len()>4{return Err("at most four images per request".into());}
            let mut total=0;
            for image in images{
                let data=image["base64"].as_str().ok_or("image base64 required")?;
                let(mime,size)=validate_image(data)?;total+=size;
                if total>512000{return Err("combined image bytes exceed 512000".into());}
                parts.push(json!({"type":"image_url","image_url":{"url":format!("data:{mime};base64,{data}")}}));
            }
        }
        let content=if parts.len()==1{json!(prompt)}else{json!(parts)};
        let system=v["options"]["system"].as_str().unwrap_or("You are a helpful assistant.");
        // A call-local effort override must not alter the session default, even
        // when request validation, networking or cancellation returns an error.
        let override_effort=v["options"].get("effort").filter(|e|!e.is_null())
            .map(|e|e.as_str().ok_or("effort must be a level string")).transpose()?;
        let previous=override_effort.map(|e|std::mem::replace(&mut self.effort,e.to_string()));
        let result=self.invoke(&model,vec![json!({"role":"system","content":system}),
            json!({"role":"user","content":content})],max);
        if let Some(previous)=previous{self.effort=previous;}
        Ok(json!(result?))
    }
    fn run_agent(&mut self)->Result<()>{
        for _ in 0..100{
            if !self.startup_ready{return Err("Python initialization is incomplete; inspect /recovery and explicitly /reset to retry startup".into());}
            self.check_system_budget(self.context_limit)?;
            self.evict()?;
            let mut messages=vec![json!({"role":"system","content":self.system_prompt()})];
            messages.extend(self.context.iter().map(|i|{
                let text=format!("[boundary {}]\n{}",i.id,i.text);
                let content=if let Some(index)=self.attachments.get(&i.id){
                    let raw=self.history_value("raw",*index).unwrap_or(Value::Null);
                    json!([{"type":"text","text":text},{"type":"image_url","image_url":
                        {"url":format!("data:{};base64,{}",raw["mime"].as_str().unwrap_or("image/png"),raw["base64"].as_str().unwrap_or(""))}}])
                }else{json!(text)};
                json!({"role":if i.role=="assistant"{"assistant"}else{"user"},"content":content})
            }));
            messages.push(json!({"role":"user","content":format!("Context usage: {}{}",
                self.context_usage(),if self.forced(){" FORCED COLLAPSE MODE: collapse or literal stderr read only."}else{""})}));
            let model=self.model.clone();let code=self.invoke(&model,messages,4096)?;
            let id=format!("agent{}",self.journal.seq);
            let result=self.execute(&id,&code,true)?;
            let cancelled=result["status"]=="cancelled";
            self.event("completed",result);
            if cancelled{return Ok(());}
            if self.deliver_steering()?{continue;}
            if self.stop{self.commit_stop()?;return Ok(());}
        }
        Err("outer loop reached safety limit; return to user".into())
    }
}

impl Host {
    fn image(&mut self,v:&Value)->Result<Value>{
        let model=v["options"]["model"].as_str().unwrap_or("openai/gpt-image-1");
        self.with_state(UiState::Thinking,Some(model),|host|host.image_inner(v))
    }
    fn image_inner(&mut self,v:&Value)->Result<Value>{
        self.reload_auth()?;
        let model=v["options"]["model"].as_str().unwrap_or("openai/gpt-image-1");
        let (url,key,id)=self.provider_config(model)?;
        if !model.starts_with("openai/"){return Err("image generation currently requires openai-compatible provider".into());}
        let prompt=v["prompt"].as_str().ok_or("prompt required")?;
        let operation=format!("img{}",self.journal.seq);
        self.journal.append("intent",json!({"operation":operation,"type":"image"}))?;
        self.journal.append("request",json!({"model":model,"prompt":prompt}))?;
        let request=reqwest::Client::new().post(format!("{url}/images/generations"))
            .bearer_auth(key).json(&json!({"model":id,"prompt":prompt,"size":"1024x1024","n":1}));
        let body=self.http_json(request,&operation)?;
        self.journal.append("response",json!({"operation":operation,"body":body}))?;
        let data=body["data"][0]["b64_json"].as_str().ok_or("image response missing base64")?;
        let bytes=B64.decode(data)?;
        let index=self.history["raw"].len();
        self.journal.append("raw",json!({"index":index,"base64":data,"mime":"image/png","operation":operation}))?;
        self.hist_push("raw",json!({"base64":data,"mime":"image/png"}));
        self.journal.append("operation_complete",json!({"operation":operation,"type":"image"}))?;
        Ok(json!({"raw_index":index,"ref":format!("H.raw[{index}]"),"bytes":bytes.len()}))
    }
    fn read_raw(&mut self,v:&Value)->Result<Value>{
        let data=v["base64"].as_str().ok_or("image bytes required")?;
        let bytes=B64.decode(data)?;
        if bytes.len()>512000{return Err("image exceeds 512000-byte limit".into());}
        let mime=if bytes.starts_with(b"\x89PNG\r\n\x1a\n"){"image/png"}
            else if bytes.starts_with(b"\xff\xd8\xff"){"image/jpeg"}
            else{return Err("only PNG/JPEG images supported".into());};
        let decoded=image::load_from_memory_with_format(&bytes,
            if mime=="image/png"{image::ImageFormat::Png}else{image::ImageFormat::Jpeg})?;
        if decoded.width()>1536||decoded.height()>1536{return Err("image dimensions exceed 1536 pixels".into());}
        let active=self.context.iter().filter(|i|self.attachments.contains_key(&i.id)).count();
        if active>=4{return Err("at most four active images per context".into());}
        let index=self.history["raw"].len();
        self.journal.append("raw",json!({"index":index,"base64":data,"mime":mime}))?;
        self.hist_push("raw",json!({"base64":data,"mime":mime}));
        let id=self.add_context("user",format!("[image H.raw[{index}]]"),true,vec![])?;
        self.journal.append("attachment",json!({"context_id":id,"raw_index":index}))?;
        self.attachments.insert(id.clone(),index);
        Ok(json!(id))
    }
}
fn redacted_command(mut command:Value)->Result<Value>{
    if command["kind"]=="login"{
        command.as_object_mut().ok_or("command must be an object")?.retain(|name,value|match name.as_str(){
            "id"|"kind"|"provider"=>true,
            "method"=>value.as_str().is_some_and(|s|["browser","manual","device","oauth","api-key"].contains(&s)),
            _=>false
        });
    }
    Ok(command)
}
fn unique_id()->u64{
    static NEXT:std::sync::atomic::AtomicU64=std::sync::atomic::AtomicU64::new(0);
    NEXT.fetch_add(1,std::sync::atomic::Ordering::Relaxed)
}
// Styling is added only after sanitization and column layout; payload escape
// bytes never become executable control sequences. NO_COLOR presence wins.
fn terminal_color_for(fd:i32)->bool{
    std::env::var_os("NO_COLOR").is_none()&&!std::env::var("TERM").is_ok_and(|t|t=="dumb")&&unsafe{libc::isatty(fd)==1}
}
fn terminal_styled_for(text:&str,style:&str,fd:i32)->String{
    if text.is_empty()||!terminal_color_for(fd){text.into()}else{format!("\x1b[{style}m{text}\x1b[0m")}
}
fn terminal_table_styled(lines:Vec<String>)->Vec<String>{
    if !terminal_color_for(1){return lines;}
    let mut header=true;
    lines.into_iter().map(|line|{
        if line.starts_with('├'){header=false;}
        if header&&line.starts_with('│'){return terminal_styled(&line,"1");}
        let mut out=String::new();
        for c in line.chars(){if "│┌┐└┘├┤┬┴┼─".contains(c){out.push_str(&format!("\x1b[2m{c}\x1b[0m"));}else{out.push(c);}}
        out
    }).collect()
}
fn terminal_styled(text:&str,style:&str)->String{terminal_styled_for(text,style,1)}
fn terminal_style_lines(lines:Vec<String>,style:&str)->Vec<String>{lines.into_iter().map(|s|terminal_styled(&s,style)).collect()}
fn terminal_result_style(status:&str)->&'static str{match status{"ok"=>"32","error"=>"31",_=>"33"}}
fn ui_text(text:&str){for line in terminal_wrap(text,terminal_width()).0{println!("{line}");}}
fn terminal_state(v:&Value,width:usize)->Vec<String>{
    let state=v["state"].as_str().unwrap_or("idle");
    let text=if state=="thinking"{format!("· thinking · {} [{}]",v["model"].as_str().unwrap_or(""),v["effort"].as_str().unwrap_or(""))}
        else{format!("· {state}")};
    terminal_style_lines(terminal_wrap(&text,width).0,match state{"thinking"=>"35","idle"=>"2","input"|"login"=>"33",_=>"36"})
}
fn terminal_cell_start(v:&Value,width:usize)->Vec<String>{
    terminal_style_lines(terminal_wrap(&format!("── cell {} · {} · {} · operation {}",v["cell"],v["language"].as_str().unwrap_or(""),
        v["source_ref"].as_str().unwrap_or(""),v["operation"].as_str().unwrap_or("")),width).0,"1;36")
}
fn terminal_cell_end(v:&Value,width:usize)->Vec<String>{
    let mut text=format!("cell {} · {} · elapsed {}ms",v["cell"],v["status"].as_str().unwrap_or("error"),v["elapsed_ms"]);
    for field in ["stdout_ref","stderr_ref"]{if let Some(reference)=v[field].as_str(){text.push_str(" · ");text.push_str(reference);}}
    terminal_style_lines(terminal_wrap(&text,width).0,terminal_result_style(v["status"].as_str().unwrap_or("error")))
}
fn ui_json(title:&str,value:&Value){
    ui_text(title);ui_text(&serde_json::to_string_pretty(value).unwrap_or_default());
}
impl Host{
    fn ui_text(&self,text:&str){self.event("notice",json!({"text":text}));}
    fn ui_json(&self,title:&str,value:&Value){
        if self.json{self.event("info",json!({"title":title,"value":value}));}else{ui_json(title,value);}
    }
    fn announce_ready(&self){
        self.event(if self.startup_ready{"ready"}else{"initialization_blocked"},json!({"session":self.journal.path,"model":self.model,"effort":self.effort,"startup_ready":self.startup_ready,"context_usage":self.context_usage()}));
        self.event("state",self.state_payload());
    }
    fn state_payload(&self)->Value{
        let mut payload=json!({"state":self.state.label(),"cell":self.active_cell,"cells":self.cells});
        if self.state==UiState::Thinking{
            payload["model"]=json!(self.thinking_model.as_deref().unwrap_or(&self.model));
            payload["effort"]=json!(self.effort);
        }
        payload
    }
    fn set_state(&mut self,state:UiState,model:Option<&str>){
        let model=if state==UiState::Thinking{Some(model.unwrap_or(&self.model).to_string())}else{None};
        if self.state==state&&self.thinking_model==model{return;}
        self.state=state;self.thinking_model=model;
        self.event("state",self.state_payload());
    }
    fn with_state<T>(&mut self,state:UiState,model:Option<&str>,action:impl FnOnce(&mut Self)->Result<T>)->Result<T>{
        let previous=self.state;let previous_model=self.thinking_model.clone();
        self.set_state(state,model);
        let result=action(self);
        self.set_state(previous,previous_model.as_deref());
        result
    }
    fn with_cell(&mut self,language:&str,operation:&str,source_ref:&str,action:impl FnOnce(&mut Self)->Result<Value>)->Result<Value>{
        let cell=self.cells.checked_add(1).ok_or("session cell counter exhausted")?;
        let parent=self.active_cell;
        let start=json!({"cell":cell,"parent_cell":parent,"language":language,"operation":operation,"source_ref":source_ref});
        self.journal.append("cell_start",start.clone())?;
        self.cells=cell;self.active_cell=Some(cell);self.event("cell_start",start);
        let began=std::time::Instant::now();
        let result=self.with_state(UiState::Running,None,|host|{
            let result=action(host);
            let value=result.as_ref().ok();
            let status=value.and_then(|v|v["status"].as_str()).unwrap_or("error");
            let end=json!({"cell":cell,"parent_cell":parent,"language":language,"operation":operation,"source_ref":source_ref,
                "status":status,"elapsed_ms":began.elapsed().as_millis().min(u64::MAX as u128) as u64,
                "stdout_ref":value.and_then(|v|v["stdout"]["ref"].as_str()),
                "stderr_ref":value.and_then(|v|v["stderr"]["ref"].as_str())});
            let committed=host.journal.append("cell_end",end.clone());
            if committed.is_ok(){host.event("cell_end",end);}
            host.active_cell=parent;
            committed?;result
        });
        // Also restore attribution if a durable-end write failed.
        self.active_cell=parent;result
    }
    fn login_providers(&self)->Vec<String>{
        let mut names:Vec<String>=PROVIDERS.iter().map(|p|p.0.to_string()).collect();
        names.extend(["openai-codex".into(),"github-copilot".into()]);
        if let Some(config)=self.config["providers"].as_object(){names.extend(config.keys().cloned());}
        names.sort();names.dedup();names
    }
    fn resolve_provider(&self,query:&str)->Result<String>{
        let query=provider_alias(query);
        let names=self.login_providers();
        if let Some(name)=names.iter().find(|n|n.eq_ignore_ascii_case(query)){return Ok(name.clone());}
        let found:Vec<_>=names.into_iter().filter(|n|fuzzy_score(query,n).is_some()).collect();
        if found.len()==1{return Ok(found[0].clone());}
        if found.is_empty(){return Err(format!("Unknown provider {query}; /login lists supported providers").into());}
        Err(format!("Provider query is ambiguous: {}",found.join(", ")).into())
    }
    fn matching_models(&self,query:&str)->Vec<Value>{
        let models=self.models();
        let mut found:Vec<(i64,Value)>=vec![];
        let (provider,needle)=query.split_once('/').map(|(p,n)|(Some(provider_alias(p)),n)).unwrap_or((None,query));
        for model in models.as_array().unwrap(){
            let id=model["id"].as_str().unwrap_or("");
            if model["image_output"]==true{continue;}
            if provider.is_some_and(|p|!model["provider"].as_str().unwrap_or("").eq_ignore_ascii_case(p)){continue;}
            let candidate=if provider.is_some(){id.split_once('/').map_or(id,|(_,id)|id)}else{id};
            let score=fuzzy_score(needle,candidate).or_else(||fuzzy_score(needle,model["name"].as_str().unwrap_or("")))
                .or_else(||model["aliases"].as_array().and_then(|aliases|aliases.iter().filter_map(|a|a.as_str().and_then(|a|fuzzy_score(needle,a))).max()));
            if let Some(score)=score{found.push((score,model.clone()));}
        }
        found.sort_by(|a,b|b.0.cmp(&a.0).then_with(||a.1["id"].as_str().cmp(&b.1["id"].as_str())));
        found.into_iter().map(|p|p.1).collect()
    }
    fn choose_model(&mut self,query:&str)->Result<()>{
        let query=query.trim();
        let canonical=query.split_once('/').map(|(p,id)|format!("{}/{id}",provider_alias(p))).unwrap_or(query.into());
        let models=self.models();
        let exact:Vec<_>=models.as_array().unwrap().iter().filter(|m|m["image_output"]!=true&&
            (m["id"].as_str().is_some_and(|id|id.eq_ignore_ascii_case(&canonical)||
                (!query.contains('/')&&id.split_once('/').is_some_and(|(_,id)|id.eq_ignore_ascii_case(query))))
             ||m["aliases"].as_array().is_some_and(|aliases|aliases.iter().any(|alias|alias.as_str().is_some_and(|alias|
                if let Some((provider,needle))=canonical.split_once('/'){
                    m["provider"].as_str().is_some_and(|p|p.eq_ignore_ascii_case(provider))&&alias.eq_ignore_ascii_case(needle)
                }else{alias.eq_ignore_ascii_case(query)}))))).collect();
        let found=if exact.is_empty(){self.matching_models(query)}else{exact.into_iter().cloned().collect()};
        if found.is_empty(){return Err(format!("No model matches {query}; /model list shows known/configured models").into());}
        if found.len()!=1{
            return Err(format!("Model query is ambiguous: {}. Use a full provider/model ID.",
                found.iter().take(16).filter_map(|m|m["id"].as_str()).collect::<Vec<_>>().join(", ")).into());
        }
        if found[0]["metadata_complete"]==false{return Err("model discovered without capability metadata; declare its API/context/capabilities in config.json first".into());}
        if found[0]["available"]==false&&found[0]["user_declared"]!=true{return Err("model not offered in the last account catalog; /model list refresh to check availability".into());}
        let model=found[0]["id"].as_str().unwrap();
        self.check_model_system_budget(model,self.model_limit(model)?)?;
        self.set_model(model.into())?;
        if self.validate_effort(&self.effort).is_err(){
            self.effort=found[0]["default_effort"].as_str().unwrap_or("medium").into();
            self.event("notice",json!({"text":format!("Previous effort unsupported by this model; reset to {}.",self.effort)}));
        }
        self.save_session_settings()?;
        self.event("notice",json!({"text":format!("Model: {} [{}]",self.model,self.effort)}));
        if found[0]["deprecated"]==true{self.event("notice",json!({"text":"This legacy Codex model is deprecated for ChatGPT sign-in. Try /model codex/sol61 or /model codex/luna6; availability depends on your account/workspace."}));}
        Ok(())
    }
    fn validate_effort(&self,effort:&str)->Result<()>{
        if !["off","minimal","low","medium","high","xhigh","max"].contains(&effort){
            return Err("Unsupported effort: choose off, minimal, low, medium, high, xhigh or max (model-dependent)".into());
        }
        let meta=self.reasoning_metadata(&self.model);
        if let Some(levels)=meta["reasoning_efforts"].as_array(){
            if !levels.iter().any(|level|level==effort){return Err(format!("Effort {effort} unsupported by this model; supported: {}",meta["reasoning_efforts"]).into());}
        }else if effort=="max"&&!(meta["reasoning"]==true&&meta["api"]=="anthropic-messages"&&self.model.contains("claude-opus-4-6")){
            return Err("Effort max unsupported by this model; requires explicit capability metadata or Opus 4.6".into());
        }
        Ok(())
    }
    fn change_effort(&mut self,effort:&str)->Result<()>{
        let effort=effort.to_lowercase();self.validate_effort(&effort)?;
        self.effort=effort;self.save_session_settings()?;
        self.event("notice",json!({"text":format!("Effort: {}",self.effort)}));Ok(())
    }
    fn save_session_settings(&mut self)->Result<()>{
        self.journal.append("settings_change",json!({"model":self.model,"effort":self.effort}))?;Ok(())
    }
    fn list_models(&self,query:&str){
        let found=self.matching_models(query);
        let mut table="## Models (built-in/configured/cached inventory)\n\n| Model | Authentication | Context |\n| --- | --- | ---: |\n".to_string();
        for model in found{
            let ready=if model["metadata_complete"]==false{"needs capability metadata"}
                else if model["available"]==false&&model["user_declared"]!=true{"not in account catalog"}
                else if model["credential_backed"]==true{"credentials present"}
                else if model["ready"]==true{"local/anonymous"}else{"login needed"};
            table.push_str(&format!("| {}{} | {} | {} |\n",model["id"].as_str().unwrap_or(""),
                if model["deprecated"]==true{" (deprecated for subscription)"}else{""},ready,model["context_limit"]));
        }
        self.event("say",json!({"text":table}));
    }
    fn session_paths(&self)->Result<Vec<PathBuf>>{
        let mut paths=fs::read_dir(self.home.join("sessions"))?.filter_map(|p|p.ok().map(|p|p.path()))
            .filter(|p|p.extension().is_some_and(|e|e=="jsonl")).collect::<Vec<_>>();
        paths.sort();paths.reverse();Ok(paths)
    }
    fn switch_session(&mut self,query:Option<&str>,cancel_tasks:bool)->Result<()>{
        if !self.pending.is_empty(){return Err("Pending accepted commands must be handled before switching sessions".into());}
        self.bg_guard(cancel_tasks)?;
        let path=if let Some(query)=query{
            let query=query.trim_matches(|c|c=='\''||c=='"');
            let direct=PathBuf::from(query);
            let paths=self.session_paths()?;
            if direct.is_file(){Some(fs::canonicalize(direct)?)}else{
                let found:Vec<_>=paths.into_iter().filter(|p|fuzzy_score(query,p.file_name().unwrap_or_default().to_string_lossy().as_ref()).is_some()).collect();
                if found.len()!=1{return Err(format!("Session query matches {} sessions; use /sessions and an exact path",found.len()).into());}
                Some(found[0].clone())
            }
        }else{None};
        if path.as_ref().is_some_and(|p|fs::canonicalize(&self.journal.path).ok().as_ref()==Some(p)){
            self.ui_text("Already in this session.");return Ok(());
        }
        let defaults=if path.is_none(){load_json(&self.home.join("config.json"))?}else{Value::Null};
        let model_override=if path.is_none()&&defaults["model"]==self.config["model"]{Some(self.model.as_str())}else{None};
        let effort_override=if path.is_none()&&defaults["effort"]==self.config["effort"]{Some(self.effort.as_str())}else{None};
        let mut next=Host::new(self.home.clone(),path.clone(),self.json,self.no_model,model_override,effort_override)?;
        next.announce_ready();
        self.ui_text(&format!("Session: {}",next.journal.path.display()));
        next.incoming=self.incoming.take();next.input_closed=self.input_closed;
        *self=next;Ok(())
    }
    fn ui_command(&mut self,line:&str)->Result<bool>{
        self.reload_auth()?;
        let (command,args)=line.split_once(char::is_whitespace).map(|(c,a)|(c,a.trim())).unwrap_or((line,""));
        match command{
            "/quit"|"/exit"=>{self.bg_guard(transition_cancel(args)?)?;return Ok(false);},
            "/tasks"|"/task"|"/bg"|"/wakeups"|"/wakeup"=>self.ui_background_command(command,args)?,
            "/help"|"/hotkeys"=>self.ui_text("py — ordinary persistent Python, unrestricted execution.\nOrdinary Python. No sandbox.\n! shell / @ Python: hidden from the model; !! / @@: visible.\nTab: fuzzy picker for commands, arguments, Python names, executables and files.\nType to filter; arrows/Tab move, Enter selects (not submits); Esc cancels.\nShift+Enter inserts newline on supported terminals; Ctrl-J is a fallback.\nContinuation lines align after the two-column > prompt.\nPython brackets/suites and bracketed multiline paste form a single cell.\nCtrl-D: exit idle editor. Ctrl-C: clear editor / cancel active operation.\n/model list [query] /models [query]: cached inventory, refreshed when stale\n/model list refresh [provider] /models refresh [provider]: force refresh\n/model <provider/id or fuzzy query>: select, rejecting ambiguity\n/effort <off|minimal|low|medium|high|xhigh> (/think, /thinking)\n/login [provider] [browser|manual|device|api-key] /logout [provider] /auth\n/status /context /config /recovery /session /sessions\n/config get <key> | set <key> <JSON-value> | unset <key> | reload\nDurable entries: ~/.py/skills/*.md (or PY_HOME); ordinary file editing, no skills API.\nCore entries and inventory freeze on /new; reset/resume preserve that snapshot.\nCore Python runs unrestricted in every fresh main worker; prefer definitions/imports.\n/new /resume <path or fuzzy session> /reset /compact [instructions]\n/tasks [state] /task <id> /task logs <id> [stdout|stderr|both]\n/task kill <id> [--force] /bg shell|python <source>\n/wakeups /wakeup cancel|run <id>\n/quit, /new and /resume require --cancel-tasks while jobs run.\nH stores full history; previews show at most 12 original lines.\nSession resume restores H/context/settings, never Python variables or execution.\n/quit: exit. Browser OAuth: Codex callback, Anthropic hidden code paste.\nUnsupported provider protocols and live subscription parity are not claimed."),
            "/model" if args.is_empty()=>self.ui_text(&self.model),
            "/model" if args=="list"||args.starts_with("list ")=>self.model_list_command(args.strip_prefix("list").unwrap().trim())?,
            "/model"=>{
                let provider=args.split_once('/').map(|p|provider_alias(p.0).to_string()).unwrap_or_else(||self.model.split_once('/').map_or("openai",|p|provider_alias(p.0)).to_string());
                let cancel_revision=self.cancel_revision;self.maybe_refresh_model_catalog(&provider);
                if self.cancel_revision!=cancel_revision{return Err("model selection cancelled; previous selection retained".into());}
                self.choose_model(args)?;
            },
            "/models"=>self.model_list_command(args)?,
            "/effort"|"/think"|"/thinking"=>if args.is_empty(){self.ui_text(&self.effort);}else{self.change_effort(args)?;},
            "/login"=>self.interactive_login(args)?,
            "/logout"=>if args.is_empty(){
                let stored=self.auth.as_object().map(|m|m.keys().cloned().collect::<Vec<_>>()).unwrap_or_default();
                if stored.is_empty(){self.ui_text("No stored credentials. Environment variables are unchanged.");}
                else{self.ui_text(&format!("Use /logout <provider>: {}. Environment credentials are not removed.",stored.join(", ")));}
            }else{let provider=self.resolve_provider(args)?;self.logout(&provider)?;self.ui_text(&format!("Removed stored credentials for {provider}; environment credentials unchanged."));},
            "/auth"=>self.ui_json("Authentication (no secrets)",&self.auth_status()),
            "/status"=>self.ui_json("Status",&self.status()),
            "/context"=>self.ui_json("Context metadata",&self.context_usage()),
            "/config"=>self.config_command(args)?,
            "/recovery"=>self.ui_json("Recovery: no automatic replay",&self.recovery_status()?),
            "/session"=>self.ui_text(&format!("Session: {}",self.journal.path.display())),
            "/sessions"|"/resume" if args.is_empty()=>{self.ui_text("Sessions — /resume <path or fuzzy filename>");for path in self.session_paths()?{self.ui_text(&path.to_string_lossy());}},
            "/resume"=>{let (query,cancel)=transition_query(args)?;self.switch_session(Some(query),cancel)?;},
            "/new"=>self.switch_session(None,transition_cancel(args)?)?,
            "/reset"=>self.reset_worker()?,
            "/interrupt"=>self.ui_text("No operation is running. Ctrl-C cancels an active operation."),
            "/compact"=>{
                if self.no_model{return Err("Compaction requires an authenticated model; local mode never silently discards context".into());}
                let id=format!("compact{}",self.journal.seq);
                self.dispatch(json!({"id":id,"kind":"submit","text":format!("Compact the context using a standalone agent.context.collapse call, preserve task constraints, unfinished work and original-range references, then agent.loop.stop(). Additional instructions: {args}")}))?;
            },
            _=>return Err(format!("Unknown command {command}; /help lists implemented commands").into())
        }
        Ok(true)
    }
}
#[cfg(test)]
mod background_ui_tests {
    use super::*;
    #[test]
    fn transition_cancel_requires_explicit_choice(){
        assert!(!transition_cancel("").unwrap());assert!(transition_cancel("--cancel-tasks").unwrap());
        assert!(transition_cancel("yes").is_err());assert!(transition_cancel("--cancel-tasks extra").is_err());
    }
    #[test]
    fn transition_resume_preserves_path_and_cancellation(){
        assert_eq!(transition_query("/some path/session.jsonl --cancel-tasks").unwrap(),("/some path/session.jsonl",true));
        assert_eq!(transition_query("session.jsonl").unwrap(),("session.jsonl",false));
        assert!(transition_query("--cancel-tasks").is_err());assert!(transition_query("x--cancel-tasks").is_err());
    }
    fn fixture(body:&str){
        let script=format!(r#"
import os,sys,tempfile,subprocess,json,glob,time,queue,threading,pty,fcntl,termios,select
home=tempfile.mkdtemp(prefix='py-background-ui-');env=dict(os.environ,PY_HOME=home,TERM='xterm-256color')
for key in ('PY_MODEL','PY_CONTEXT_LIMIT'):env.pop(key,None)
def records():
 paths=glob.glob(home+'/sessions/*.jsonl')
 if not paths:return []
 result=[]
 for line in open(paths[0]):
  try:result.append(json.loads(line))
  except ValueError:pass
 return result
def events(p):
 q=queue.Queue()
 def read():
  for line in p.stdout:
   try:q.put(json.loads(line))
   except ValueError:pass
 threading.Thread(target=read,daemon=True).start()
 return q
def wait_event(q,predicate):
 end=time.monotonic()+8;seen=[]
 while time.monotonic()<end:
  try:v=q.get(timeout=max(.001,end-time.monotonic()))
  except queue.Empty:break
  seen.append(v)
  if predicate(v):return v
 raise AssertionError(('missing event',seen,records()))
{body}
"#);
        let out=crate::test_command("python3").args(["-c",&script,&std::env::var("PY_HARNESS_BIN").expect("build CLI and set PY_HARNESS_BIN")]).output().unwrap();
        assert!(out.status.success(),"{}\n{}",String::from_utf8_lossy(&out.stdout),String::from_utf8_lossy(&out.stderr));
    }
    #[test]
    fn e2e_nonterminal_editor_does_not_read_ahead_input(){fixture(r#"
p=subprocess.Popen([sys.argv[1],'--json','--no-model'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
out,err=p.communicate("@print(input('question: '))\nanswer-kept-for-input\n/quit\n",timeout=8)
assert p.returncode==0,(out,err)
r=records();codes=[v['payload']['source'] for v in r if v['kind']=='code']
assert codes==["print(input('question: '))"],codes
assert any(v['kind']=='stream' and 'answer-kept-for-input' in v['payload']['text'] for v in r),r
"#);}
    #[test]
    fn e2e_idle_json_services_background_completion_and_wakeup(){fixture(r#"
p=subprocess.Popen([sys.argv[1],'--json','--json-input','--no-model'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env);q=events(p)
try:
 wait_event(q,lambda v:v.get('kind')=='ready')
 p.stdin.write(json.dumps({'id':'launch','kind':'bg_run','task_kind':'shell','source':'sleep .06; printf background-private','options':{'wakeup_reason':'check result'}})+'\n');p.stdin.flush()
 completed=wait_event(q,lambda v:v.get('kind')=='completed' and v.get('command_id')=='launch');task=completed['result']['id']
 wait_event(q,lambda v:v.get('kind')=='task_state' and v.get('id')==task and v.get('status')=='succeeded')
 wait_event(q,lambda v:v.get('kind')=='notice' and 'Wakeup:' in v.get('text',''))
 time.sleep(.08);r=records()
 assert len([v for v in r if v['kind']=='wakeup_state' and v['payload']['state']=='consumed'])==1,r
 assert not any('background-private' in v['payload'].get('text','') for v in r if v['kind']=='context_add'),r
 p.stdin.write(json.dumps({'id':'quit','kind':'quit'})+'\n');p.stdin.flush();p.wait(timeout=5)
 assert p.returncode==0,p.stderr.read()
finally:
 if p.poll() is None:p.kill();p.wait()
"#);}
    #[test]
    fn e2e_idle_tty_wakeup_preserves_multiline_draft_and_cursor(){fixture(r#"
m,s=pty.openpty();fcntl.ioctl(m,termios.TIOCSWINSZ,__import__('struct').pack('HHHH',24,80,0,0))
def session():os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
p=subprocess.Popen([sys.argv[1],'--json','--no-model'],stdin=s,stdout=subprocess.PIPE,stderr=s,text=True,env=env,preexec_fn=session);os.close(s);q=events(p);data=bytearray()
def read_until(predicate):
 end=time.monotonic()+8
 while time.monotonic()<end:
  if predicate():return
  if select.select([m],[],[],.03)[0]:
   try:data.extend(os.read(m,65536))
   except OSError:break
 raise AssertionError(('missing tty paint',bytes(data),records()))
try:
 wait_event(q,lambda v:v.get('kind')=='ready');read_until(lambda:b'\x1b[?2004h' in data)
 os.write(m,b"@agent.loop.stop(wakeup=(.4,'check timer'))\r")
 wait_event(q,lambda v:v.get('kind')=='completed' and v.get('status')=='ok')
 read_until(lambda:data.count(b'\x1b[?2004h')>=2)
 # Paste a multiline draft, then move into the second line before waking.
 os.write(m,b'\x1b[200~@text = (\n    "ac"\n)\nprint(text)\x1b[201~\x1b[A\x1b[A\x01\x1b[C\x1b[C\x1b[C\x1b[C\x1b[C\x1b[C')
 wait_event(q,lambda v:v.get('kind')=='notice' and 'Wakeup:' in v.get('text',''))
 read_until(lambda:data.count(b'\x1b[?2004h')>=3)
 os.write(m,b'b\r')
 wait_event(q,lambda v:v.get('kind')=='completed' and v.get('status')=='ok')
 codes=[v['payload']['source'] for v in records() if v['kind']=='code']
 assert codes[-1]=='text = (\n    "abc"\n)\nprint(text)',codes
 read_until(lambda:data.count(b'\x1b[?2004h')>=4)
 os.write(m,b'/quit\r');p.wait(timeout=5);assert p.returncode==0,p.returncode
finally:
 if p.poll() is None:p.kill();p.wait()
 os.close(m)
"#);}
}
fn transition_cancel(args:&str)->Result<bool>{
    match args.trim(){""=>Ok(false),"--cancel-tasks"=>Ok(true),_=>Err("Only --cancel-tasks is accepted here".into())}
}
fn transition_query(args:&str)->Result<(&str,bool)>{
    let (query,cancel)=if let Some(query)=args.strip_suffix("--cancel-tasks"){
        if !query.ends_with(char::is_whitespace){return Err("Separate --cancel-tasks from the session path".into());}
        (query.trim(),true)
    }else{(args.trim(),false)};
    if query.is_empty(){return Err("Usage: /resume <path or fuzzy session> [--cancel-tasks]".into());}
    Ok((query,cancel))
}
impl Host{
    fn ui_background_command(&mut self,command:&str,args:&str)->Result<()>{
        let id=format!("ui{}",self.journal.seq);let words:Vec<_>=args.split_whitespace().collect();
        let mut v=match command{
            "/tasks" if words.len()<=1=>json!({"kind":"task_list","state":words.first()}),
            "/task"=>match words.as_slice(){
                [task]=>json!({"kind":"task_get","task_id":task}),
                ["logs",task]=>json!({"kind":"task_logs","task_id":task,"stream":"both"}),
                ["logs",task,stream] if ["stdout","stderr","both"].contains(stream)=>json!({"kind":"task_logs","task_id":task,"stream":stream}),
                ["kill",task]=>json!({"kind":"task_kill","task_id":task,"force":false}),
                ["kill",task,"--force"]=>json!({"kind":"task_kill","task_id":task,"force":true}),
                _=>return Err("Usage: /task <id> | /task logs <id> [stdout|stderr|both] | /task kill <id> [--force]".into())
            },
            "/bg"=>{
                let (kind,source)=args.split_once(char::is_whitespace).ok_or("Usage: /bg shell|python <source>")?;
                if !["shell","python"].contains(&kind)||source.trim().is_empty(){return Err("Usage: /bg shell|python <source>".into());}
                json!({"kind":"bg_run","task_kind":kind,"source":source.trim_start()})
            },
            "/wakeups" if words.is_empty()=>json!({"kind":"wakeup_list"}),
            "/wakeup"=>match words.as_slice(){
                ["cancel",wake]=>json!({"kind":"wakeup_cancel","wakeup_id":wake}),
                ["run",wake]=>json!({"kind":"wakeup_run","wakeup_id":wake}),
                _=>return Err("Usage: /wakeup cancel|run <id>".into())
            },
            _=>return Err("Unexpected arguments; /help lists background commands".into())
        };
        let previous:HashSet<_>=self.bg_tasks.keys().cloned().collect();
        v["id"]=json!(id);self.dispatch(v.clone())?;
        let result=match v["kind"].as_str().unwrap(){
            "task_list"=>self.bg_list(v["state"].as_str())?,
            "bg_run"=>self.bg_tasks.iter().find(|(id,_)|!previous.contains(*id)).map(|(_,task)|task.metadata.clone()).ok_or("background launch did not create a task")?,
            "task_get"|"task_kill"=>self.bg_get(v["task_id"].as_str().unwrap())?,
            "task_logs"=>{
                let metadata=self.bg_get(v["task_id"].as_str().unwrap())?;
                for stream in ["stdout","stderr"]{
                    if v["stream"]!="both"&&v["stream"]!=stream{continue;}
                    let index=metadata[stream]["index"].as_u64().ok_or("task has no output reference")? as usize;
                    self.ui_text(&format!("{} — {}\n{}\n[{} bytes; preview only; full output in H]",stream,metadata[stream]["ref"].as_str().unwrap_or(""),self.ui_stream_preview(stream,index)?,metadata[stream]["bytes"]));
                }
                return Ok(());
            },
            _=>self.wakeup_list()?
        };
        self.ui_json("Background management",&result);Ok(())
    }
    fn ui_stream_preview(&self,stream:&str,index:usize)->Result<String>{
        let value=self.history.get(stream).and_then(|values|values.get(index)).ok_or("missing task stream")?;
        let mut bytes=Vec::new();
        if let Some(chunks)=value["$chunks"].as_array(){
            for seq in chunks{
                let event=self.journal.event(seq.as_u64().ok_or("invalid stream chunk")? as usize)?;
                let chunk=B64.decode(event["payload"]["base64"].as_str().ok_or("missing stream bytes")?)?;
                bytes.extend_from_slice(&chunk[..chunk.len().min(16384-bytes.len())]);
                if bytes.len()>=16384||bytes.iter().filter(|b|**b==b'\n').count()>=12{break;}
            }
        }else if let Some(text)=value.as_str(){bytes.extend_from_slice(&text.as_bytes()[..text.len().min(16384)]);}
        else{return Err("invalid task stream".into());}
        Ok(String::from_utf8_lossy(&bytes).lines().take(12).collect::<Vec<_>>().join("\n").chars().take(4000).collect())
    }
}
fn main(){
    if let Err(e)=cli(){
        for line in terminal_wrap(&e.to_string(),terminal_width()).0{eprintln!("{line}");}
        std::process::exit(1);
    }
}
fn cli()->Result<()>{
    // Parse every option before touching storage or starting Python. There are
    // deliberately no positional prompts, key arguments or extension flags.
    let args:Vec<String>=std::env::args().skip(1).collect();
    let (mut help,mut version,mut spec,mut json_mode,mut json_input,mut no_model)=(false,false,false,false,false,false);
    let (mut model,mut effort,mut resume):(Option<String>,Option<String>,Option<PathBuf>)=(None,None,None);
    let mut i=0;
    while i<args.len(){
        let flag=args[i].as_str();
        match flag{
            "--help"|"-h"=>help=true,"--version"|"-V"=>version=true,"--spec"=>spec=true,
            "--json"=>json_mode=true,"--json-input"=>json_input=true,"--no-model"=>no_model=true,
            "--model"|"--effort"|"--session"|"--resume"=>{
                let value=args.get(i+1).filter(|v|!v.is_empty()&&!v.starts_with('-'))
                    .ok_or_else(||format!("{flag} requires a value; run --help for usage"))?;
                match flag{
                    "--model"=>{if model.replace(value.clone()).is_some(){return Err("--model specified more than once".into());}},
                    "--effort"=>{
                        let value=value.to_lowercase();
                        if !["off","minimal","low","medium","high","xhigh","max"].contains(&value.as_str()){
                            return Err("Unsupported effort: off, minimal, low, medium, high, xhigh or max (model-dependent)".into());
                        }
                        if effort.replace(value).is_some(){return Err("--effort specified more than once".into());}
                    },
                    _=>{if resume.replace(PathBuf::from(value)).is_some(){return Err("Use only one --session or --resume path".into());}}
                }
                i+=1;
            },
            _=>return Err(format!("Unexpected argument {flag}; run --help for supported options (no positional prompts)").into())
        }
        i+=1;
    }
    if help{
        ui_text("Usage: py [options]\nPersistent ordinary Python + a Python-writing model. No sandbox.\n\n--model <provider/id or unique fuzzy query>: choose a known/configured model\n--effort <off|minimal|low|medium|high|xhigh|max>: reasoning preference (supported levels depend on the model)\n--session <path>, --resume <path>: restore an existing journal; fresh Python, no replay\n--no-model: local Python/shell mode, no automatic model turns\n--json: write versioned JSONL events to stdout instead of human previews\n--json-input: read JSONL commands from stdin instead of the editor\n--spec: print the embedded specification and coverage ledger\n--help, -h: show this help without starting Python or creating files\n--version, -V: show the version without creating files\n\nJSON input and output are independent; combine --json --json-input for automation.\nPY_HOME chooses global state storage (default ~/.py); PY_MODEL/config.json choose defaults.\nCLI model/effort override restored session settings and are saved in that session, not global defaults.\nInteractive: /help, /login, /model list, /new, /resume. Tab fuzzily completes.\n! / @: hidden shell / Python; !! / @@: visible. Empty prompts are ignored.\nThere are no positional prompts or CLI API-key flags; use hidden /login entry.");
        return Ok(());
    }
    if version{println!("py {} (transition)",env!("CARGO_PKG_VERSION"));return Ok(());}
    if spec{print!("{SPEC}");return Ok(());}
    if let Some(path)=&resume{if !path.is_file(){return Err(format!("Session file does not exist: {}",path.display()).into());}}
    install_signals();
    let home=std::env::var_os("PY_HOME").map(PathBuf::from).unwrap_or_else(||
        PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".py"));
    let mut host=Host::new(home,resume,json_mode,no_model,model.as_deref(),effort.as_deref())?;
    host.announce_ready();
    if json_input{
        let (tx,rx)=std::sync::mpsc::channel();
        std::thread::spawn(move||{
            for line in io::stdin().lock().lines(){
                match line{
                    Ok(s)=>match serde_json::from_str::<Value>(&s){
                        Ok(v)=>if tx.send(v).is_err(){break;},
                        Err(e)=>{let _=tx.send(json!({"id":format!("bad{}",now_ms()),"kind":"invalid","error":e.to_string()}));}
                    },
                    Err(_)=>break
                }
            }
        });
        host.incoming=Some(rx);
        loop{
            INTERRUPT.store(false,std::sync::atomic::Ordering::SeqCst);
            host.service_background()?;
            let command=if let Some(v)=host.pending.pop_front(){v}else{
                match host.incoming.as_ref().unwrap().recv_timeout(std::time::Duration::from_millis(25)){
                    Ok(v)=>v,
                    Err(std::sync::mpsc::RecvTimeoutError::Disconnected)=>break,
                    Err(std::sync::mpsc::RecvTimeoutError::Timeout)=>{
                        if let Err(e)=host.dispatch_wakeups(){host.event("error",json!({"error":e.to_string()}));}
                        continue;
                    }
                }
            };
            INTERRUPT.store(false,std::sync::atomic::Ordering::SeqCst);
            let command_id=command["id"].as_str().unwrap_or("").to_string();
            match host.dispatch(command){
                Ok(false)=>break,Ok(true)=>{},Err(e)=>host.event("error",json!({"command_id":command_id,"error":e.to_string()}))
            }
        }
    }else{
        let mut editor=rustyline::Editor::<Completion,rustyline::history::DefaultHistory>::new()?;
        bind_multiline_keys(&mut editor);
        let history=host.home.join("editor-history");
        let _=editor.load_history(&history);
        loop{
            // Idle interrupts clear the editor, never cancel a future operation.
            INTERRUPT.store(false,std::sync::atomic::Ordering::SeqCst);
            host.reload_auth()?;
            let prompt=if host.json{""}else{"> "};
            let models=host.models();let providers=host.login_providers();let current_model=host.model.clone();
            let mut helper=Completion::from_worker(&mut host.worker,models,host.home.clone(),current_model,providers)?;
            helper.catalog.task_ids=host.bg_tasks.keys().cloned().collect();
            helper.catalog.wakeup_ids=host.wakeups.keys().cloned().collect();
            editor.set_helper(Some(helper));
            editor.bind_sequence(rustyline::KeyEvent::from('\t'),editor.helper().unwrap().picker_handler());
            let line=match if terminal_editor_available(host.json){
                terminal_readline(editor.helper().unwrap(),&editor.history().iter().cloned().collect::<Vec<_>>(),if host.json{2}else{1},&mut host)
            }else{serviceable_readline(editor.helper().unwrap(),prompt,&mut host)}{
                Ok(s)=>s,Err(rustyline::error::ReadlineError::Interrupted)=>{
                    INTERRUPT.store(false,std::sync::atomic::Ordering::SeqCst);continue;
                },
                Err(rustyline::error::ReadlineError::Eof)=>break,Err(e)=>return Err(e.into())
            };
            INTERRUPT.store(false,std::sync::atomic::Ordering::SeqCst);
            if line.trim().is_empty(){continue;}
            // Never persist authentication commands, let alone an accidentally
            // pasted credential. Keys are accepted only by the hidden tty prompt.
            if !line.trim_start().starts_with("/login")&&!line.trim_start().starts_with("/config"){
                editor.add_history_entry(&line)?;
            }
            if line.starts_with('/'){
                match host.ui_command(line.trim()){
                    Ok(false)=>break,Ok(true)=>{},Err(e)=>host.event("error",json!({"error":e.to_string()}))
                }
                continue;
            }
            let id=format!("ui{}",host.journal.seq);
            let v=if let Some(s)=line.strip_prefix("!!"){json!({"id":id,"kind":"shell","command":s,"visible":true})}
                else if let Some(s)=line.strip_prefix("!"){json!({"id":id,"kind":"shell","command":s})}
                else if let Some(s)=line.strip_prefix("@@"){json!({"id":id,"kind":"python","source":s,"visible":true})}
                else if let Some(s)=line.strip_prefix("@"){json!({"id":id,"kind":"python","source":s})}
                else{json!({"id":id,"kind":"submit","text":line})};
            if let Err(e)=host.dispatch(v){host.event("error",json!({"error":e.to_string()}));}
        }
        editor.save_history(&history)?;
        if history.exists(){fs::set_permissions(&history,std::os::unix::fs::PermissionsExt::from_mode(0o600))?;}
    }
    host.bg_shutdown()?;
    Ok(())
}

fn bind_multiline_keys(editor:&mut rustyline::Editor<Completion,rustyline::history::DefaultHistory>){
    // Ctrl-J is a newline, never accept-or-validate. Native backends that report
    // Shift-Enter can use the same action. Rustyline 15's Unix byte parser does
    // not decode CSI-u / modifyOtherKeys Enter: do not enable those protocols or
    // map UnknownEscSeq to newline (it would reinterpret unrelated responses).
    editor.bind_sequence(rustyline::KeyEvent::ctrl('J'),rustyline::Cmd::Newline);
    editor.bind_sequence(rustyline::KeyEvent(rustyline::KeyCode::Enter,rustyline::Modifiers::SHIFT),rustyline::Cmd::Newline);
}

// The tty editor is deliberately a direct reader + buffer + renderer. Rustyline
// still owns non-tty input/history and the shared idle validation/completion API.
fn terminal_editor_available(json:bool)->bool{
    (unsafe{libc::isatty(0)==1&&(json||libc::isatty(1)==1)})&&(json||!std::env::var("TERM").is_ok_and(|t|t=="dumb"))
}
struct EditTerminal{input:File,output:File,previous:libc::termios,cursor_row:usize,rows:usize,keyboard:bool,display:bool}
impl EditTerminal{
    fn open(output_fd:i32)->io::Result<Self>{
        use std::os::fd::FromRawFd;
        let mut previous=unsafe{std::mem::zeroed::<libc::termios>()};
        if unsafe{libc::tcgetattr(0,&mut previous)}<0{return Err(io::Error::last_os_error());}
        let input=unsafe{libc::dup(0)};if input<0{return Err(io::Error::last_os_error());}
        let input=unsafe{File::from_raw_fd(input)};
        let output=unsafe{libc::dup(output_fd)};if output<0{return Err(io::Error::last_os_error());}
        let output=unsafe{File::from_raw_fd(output)};
        let mut raw=previous;unsafe{libc::cfmakeraw(&mut raw);}
        raw.c_cc[libc::VMIN]=1;raw.c_cc[libc::VTIME]=0;
        if unsafe{libc::tcsetattr(0,libc::TCSANOW,&raw)}<0{return Err(io::Error::last_os_error());}
        let display=unsafe{libc::isatty(output_fd)==1}&&!std::env::var("TERM").is_ok_and(|t|t=="dumb");
        Ok(Self{input,output,previous,cursor_row:0,rows:1,keyboard:false,display})
    }
    fn keyboard(&mut self,enabled:bool)->io::Result<()>{
        if self.display&&enabled!=self.keyboard{
            // Push/pop, not a blind mode reset, preserves the invoking terminal.
            self.keyboard=enabled;
            self.output.write_all(if enabled{b"\x1b[>1u"}else{b"\x1b[<u"})?;
            self.output.flush()?;
        }Ok(())
    }
    fn byte(&mut self,timeout:i32)->io::Result<Option<u8>>{
        let mut poll=libc::pollfd{fd:self.input.as_raw_fd(),events:libc::POLLIN,revents:0};
        let ready=unsafe{libc::poll(&mut poll,1,timeout)};
        if ready<0{let e=io::Error::last_os_error();return if e.kind()==io::ErrorKind::Interrupted{Ok(None)}else{Err(e)};}
        if ready==0{return Ok(None);}
        let mut byte=[0];match std::io::Read::read(&mut self.input,&mut byte){
            Ok(0)=>Err(io::Error::new(io::ErrorKind::UnexpectedEof,"editor input closed")),
            Ok(_)=>Ok(Some(byte[0])),Err(e) if e.kind()==io::ErrorKind::Interrupted=>Ok(None),Err(e)=>Err(e)
        }
    }
    fn input_ready(&self)->bool{
        let mut poll=libc::pollfd{fd:self.input.as_raw_fd(),events:libc::POLLIN,revents:0};
        unsafe{libc::poll(&mut poll,1,0)>0&&poll.revents&libc::POLLIN!=0}
    }
    fn width(&self)->usize{terminal_width_for(self.output.as_raw_fd())}
    fn height(&self)->usize{
        let mut size=unsafe{std::mem::zeroed::<libc::winsize>()};
        if unsafe{libc::ioctl(self.output.as_raw_fd(),libc::TIOCGWINSZ,&mut size)}==0&&size.ws_row>0{size.ws_row as usize}else{24}
    }
    fn draw(&mut self,line:&str,cursor:usize)->io::Result<()>{
        if !self.display{return Ok(());}
        let width=self.width();let pad=if width>=4{2}else{width.saturating_sub(2)};
        let layout=editor_rows(line,width.saturating_sub(pad+1).max(1));
        let row=layout.iter().rposition(|r|r.start<=cursor).unwrap_or(0);
        let visible=self.height().saturating_sub(2).clamp(1,24);
        let first=row.saturating_sub(visible/2).min(layout.len().saturating_sub(visible));
        let shown=&layout[first..(first+visible).min(layout.len())];
        self.output.write_all(b"\r")?;if self.cursor_row>0{write!(self.output,"\x1b[{}A",self.cursor_row)?;}
        self.output.write_all(b"\x1b[K\x1b[J")?;
        for (i,item) in shown.iter().enumerate(){
            if i>0{self.output.write_all(b"\r\n")?;}
            let prefix=if i==0{if pad==2{"> "}else if pad==1{">"}else{""}}else if pad==2{"  "}else if pad==1{" "}else{""};
            if i==0{self.output.write_all(terminal_styled_for(prefix,"1;36",self.output.as_raw_fd()).as_bytes())?;}
            else{self.output.write_all(prefix.as_bytes())?;}
            self.output.write_all(item.text.as_bytes())?;
        }
        let local=row-first;let columns=(pad+terminal_columns(&terminal_safe(&line[layout[row].start..cursor.min(layout[row].end)])))
            .min(width.saturating_sub(1));
        self.output.write_all(b"\r")?;
        let up=shown.len()-1-local;if up>0{write!(self.output,"\x1b[{up}A")?;}
        if columns>0{write!(self.output,"\x1b[{columns}C")?;}
        self.output.flush()?;self.cursor_row=local;self.rows=shown.len();Ok(())
    }
}
impl Drop for EditTerminal{
    fn drop(&mut self){
        if self.display{
            let _=self.output.write_all(b"\r");
            let down=self.rows.saturating_sub(self.cursor_row+1);if down>0{let _=write!(self.output,"\x1b[{down}B");}
            let _=self.output.write_all(b"\r\n\x1b[?2004l");let _=self.keyboard(false);let _=self.output.flush();
        }
        unsafe{libc::tcsetattr(0,libc::TCSANOW,&self.previous);}
    }
}
struct EditRow{start:usize,end:usize,text:String}
fn editor_rows(line:&str,width:usize)->Vec<EditRow>{
    let mut rows=vec![];let mut offset=0;
    for logical in line.split('\n'){
        let clusters=terminal_clusters(logical);let mut starts=Vec::with_capacity(clusters.len()+1);let mut n=offset;
        for (text,_) in &clusters{starts.push(n);n+=text.len();}starts.push(n);
        if clusters.is_empty(){rows.push(EditRow{start:offset,end:offset,text:String::new()});}
        let mut a=0;
        while a<clusters.len(){
            let mut b=a;let mut used=0;let mut space=None;
            while b<clusters.len(){
                let safe=terminal_safe(&clusters[b].0);let columns=terminal_columns(&safe).min(width);
                if used+columns>width&&b>a{break;}
                used+=columns;b+=1;if clusters[b-1].0.chars().all(char::is_whitespace){space=Some(b);}
            }
            if b<clusters.len(){if let Some(split)=space.filter(|s|*s>a){b=split;}}
            let start=starts[a];let end=starts[b];let safe=terminal_safe(&line[start..end]);
            let safe=terminal_clusters(&safe).into_iter().map(|(s,w)|if w>width{"?".into()}else{s}).collect::<String>();
            rows.push(EditRow{start,end,text:picker_clip(&safe,width)});a=b;
        }
        offset=n+1;
    }
    rows
}
fn editor_previous(line:&str,cursor:usize)->usize{
    let mut offset=0;let mut previous=0;
    for (cluster,_) in terminal_clusters(line){if offset>=cursor{break;}previous=offset;offset+=cluster.len();}
    previous
}
fn editor_next(line:&str,cursor:usize)->usize{
    let mut offset=0;for (cluster,_) in terminal_clusters(line){offset+=cluster.len();if offset>cursor{return offset;}}line.len()
}
fn editor_line_start(line:&str,cursor:usize)->usize{line[..cursor].rfind('\n').map_or(0,|n|n+1)}
fn editor_line_end(line:&str,cursor:usize)->usize{line[cursor..].find('\n').map_or(line.len(),|n|cursor+n)}
fn editor_word_left(line:&str,cursor:usize)->usize{
    let prefix=line[..cursor].trim_end_matches(char::is_whitespace);
    prefix.char_indices().rev().find(|(_,c)|c.is_whitespace()).map_or(0,|(n,c)|n+c.len_utf8())
}
fn editor_word_right(line:&str,cursor:usize)->usize{
    let mut word=false;for (offset,c) in line[cursor..].char_indices(){
        if c.is_whitespace(){if word{return cursor+offset;}}else{word=true;}
    }line.len()
}
struct EditBuffer{line:String,cursor:usize,undo:std::collections::VecDeque<(String,usize)>,killed:String,
    history:Option<usize>,draft:(String,usize)}
impl EditBuffer{
    fn checkpoint(&mut self){
        if self.line.len()>65536{return;}
        self.undo.push_back((self.line.clone(),self.cursor));
        while self.undo.len()>32||self.undo.iter().map(|s|s.0.len()).sum::<usize>()>1_048_576{self.undo.pop_front();}
    }
    fn replace(&mut self,start:usize,end:usize,text:&str){
        if self.line.len().saturating_sub(end-start)+text.len()>1_048_576{return;}
        self.checkpoint();self.line.replace_range(start..end,text);self.cursor=start+text.len();
    }
    fn history(&mut self,old:&[String],up:bool){
        if old.is_empty(){return;}
        if self.history.is_none(){if !up{return;}self.draft=(self.line.clone(),self.cursor);self.history=Some(old.len());}
        let n=self.history.unwrap();let next=if up{n.saturating_sub(1)}else{(n+1).min(old.len())};
        self.checkpoint();
        if next==old.len(){self.line=self.draft.0.clone();self.cursor=self.draft.1;self.history=None;}
        else{self.line=old[next].clone();self.cursor=self.line.len();self.history=Some(next);}
    }
    fn vertical(&mut self,old:&[String],up:bool){
        let rows=editor_rows(&self.line,terminal_width().saturating_sub(3).max(1));
        let n=rows.iter().rposition(|r|r.start<=self.cursor).unwrap_or(0);
        if (up&&n==0)||(!up&&n+1==rows.len()){self.history(old,up);return;}
        let target=&rows[if up{n-1}else{n+1}];let goal=terminal_columns(&terminal_safe(&self.line[rows[n].start..self.cursor.min(rows[n].end)]));
        let mut used=0;let mut cursor=target.start;
        for (cluster,_) in terminal_clusters(&self.line[target.start..target.end]){
            let columns=terminal_columns(&terminal_safe(&cluster));if used+columns>goal{break;}
            used+=columns;cursor+=cluster.len();
        }self.cursor=cursor;
    }
}
enum EditKey{Byte(u8),Text(String),Sequence(String),Paste(String),Ignore}
fn editor_report(code:u32,modifier:u32)->EditKey{
    if code==13{return match modifier{1=>EditKey::Byte(13),2=>EditKey::Byte(10),_=>EditKey::Ignore};}
    if matches!(modifier,5|6)&&code<128{
        let c=code as u8;if c.is_ascii_alphabetic(){return EditKey::Byte(c.to_ascii_uppercase()-b'@');}
        if code==127{return EditKey::Byte(23);}
    }
    if modifier<=2{match code{
        9|27|127=>return EditKey::Byte(code as u8),
        57350=>return EditKey::Sequence("[D".into()),57351=>return EditKey::Sequence("[C".into()),
        57352=>return EditKey::Sequence("[A".into()),57353=>return EditKey::Sequence("[B".into()),
        _=>if let Some(c)=char::from_u32(code).filter(|c|!c.is_control()&&!(57344..=63743).contains(&(*c as u32))){return EditKey::Text(c.to_string());}
    }}EditKey::Ignore
}
fn editor_key(terminal:&mut EditTerminal,host:&mut Host)->rustyline::Result<Option<EditKey>>{
    let Some(byte)=terminal.byte(50)?else{return Ok(None);};
    if byte==27{
        let Some(first)=terminal.byte(35)?else{return Ok(Some(EditKey::Ignore));};
        if first==b'\r'{return Ok(Some(EditKey::Byte(10)));}
        if first!=b'['&&first!=b'O'{return Ok(Some(EditKey::Sequence(format!("alt:{}",first as char))));}
        let mut bytes=vec![first];for _ in 0..256{
            let Some(b)=terminal.byte(35)?else{return Ok(Some(EditKey::Ignore));};bytes.push(b);
            if (0x40..=0x7e).contains(&b){break;}
        }
        if !bytes.last().is_some_and(|b|(0x40..=0x7e).contains(b)){
            return Err(io::Error::new(io::ErrorKind::InvalidData,"oversized terminal key report").into());
        }
        if bytes.len()>64{return Ok(Some(EditKey::Ignore));}
        let sequence=String::from_utf8_lossy(&bytes).into_owned();
        if sequence=="[13;2~"{return Ok(Some(EditKey::Byte(10)));}
        if sequence=="[200~"{
            let mut paste=vec![];let mut overflow=false;
            loop{
                host.service_background().map_err(|e|io::Error::other(e.to_string()))?;
                if INTERRUPT.load(std::sync::atomic::Ordering::SeqCst){return Err(rustyline::error::ReadlineError::Interrupted);}
                let Some(b)=terminal.byte(50)?else{continue;};paste.push(b);
                if paste.ends_with(b"\x1b[201~"){
                    paste.truncate(paste.len()-6);
                    return Ok(Some(if overflow{EditKey::Ignore}else{match String::from_utf8(paste){Ok(text)=>EditKey::Paste(text),Err(_)=>EditKey::Ignore}}));
                }
                if paste.len()>1_048_576+6{overflow=true;paste.drain(..paste.len()-6);}
            }
        }
        if let Some(parameters)=sequence.strip_prefix('[').and_then(|s|s.strip_suffix('u')){
            let mut values=parameters.split(';');
            let mut codes=values.next().unwrap_or("").split(':');let code=codes.next().unwrap_or("").parse::<u32>();
            let shifted=codes.next().and_then(|c|c.parse::<u32>().ok());
            let modifier=values.next().unwrap_or("1");
            if modifier.split(':').nth(1)==Some("3"){return Ok(Some(EditKey::Ignore));}
            return Ok(Some(match (code,modifier.split(':').next().unwrap_or("").parse::<u32>()){
                (Ok(code),Ok(modifier))=>editor_report(if modifier==2{shifted.unwrap_or(code)}else{code},modifier),_=>EditKey::Ignore
            }));
        }
        if let Some(parameters)=sequence.strip_prefix("[27;").and_then(|s|s.strip_suffix('~')){
            let mut values=parameters.split(';');let modifier=values.next().unwrap_or("").parse::<u32>();let code=values.next().unwrap_or("").parse::<u32>();
            return Ok(Some(match (code,modifier){(Ok(c),Ok(m))=>editor_report(c,m),_=>EditKey::Ignore}));
        }
        return Ok(Some(EditKey::Sequence(sequence)));
    }
    if byte<32||byte==127{return Ok(Some(EditKey::Byte(byte)));}
    let length=if byte<128{1}else if byte&0xe0==0xc0{2}else if byte&0xf0==0xe0{3}else if byte&0xf8==0xf0{4}else{0};
    if length==0{return Ok(Some(EditKey::Ignore));}
    let mut bytes=vec![byte];for _ in 1..length{if let Some(b)=terminal.byte(100)?{bytes.push(b);}else{return Ok(Some(EditKey::Ignore));}}
    Ok(Some(match String::from_utf8(bytes){Ok(text)=>EditKey::Text(text),Err(_)=>EditKey::Ignore}))
}
fn editor_complete(helper:&Completion,terminal:&mut EditTerminal,buffer:&mut EditBuffer,host:&mut Host)->rustyline::Result<()>{
    let (start,matching)=helper.catalog.candidates(&buffer.line,buffer.cursor,false)?;
    let selected=if matching.len()==1{Some(matching[0].replacement.clone())}
        else if matching.is_empty()||!terminal.display{None}else{
            let (_,choices)=helper.catalog.candidates(&buffer.line,buffer.cursor,true)?;
            let prefix=&buffer.line[..buffer.cursor];let python=prefix.starts_with('@');
            let offset=if prefix.starts_with("@@")||prefix.starts_with("!!"){2}else if python||prefix.starts_with('!'){1}else{0};
            let (_,quote)=completion_token(prefix,offset);let query=completion_unescape(&prefix[start..],python,quote);
            // Picker has its own legacy decoder; temporarily restore the prior
            // keyboard protocol rather than feeding it reports it cannot parse.
            // A queued typing burst may not have been painted yet. Anchor below
            // the complete visible input, not below the caret's current line.
            terminal.draw(&buffer.line,buffer.cursor)?;
            terminal.keyboard(false)?;
            let choice=picker_select_service(&choices,&query,terminal.output.as_raw_fd(),terminal.rows,
                terminal.rows.saturating_sub(terminal.cursor_row),||host.service_background());
            terminal.keyboard(true)?;
            choice.map_err(|_|io::Error::other("selection failed"))?
        };
    if let Some(value)=selected{buffer.replace(start,buffer.cursor,&value);}
    terminal.draw(&buffer.line,buffer.cursor)?;
    if terminal.display{terminal.output.write_all(b"\x1b[?25h")?;terminal.output.flush()?;}
    Ok(())
}
fn serviceable_readline(helper:&Completion,prompt:&str,host:&mut Host)->rustyline::Result<String>{
    use rustyline::error::ReadlineError;
    if unsafe{libc::isatty(0)==1}{print!("{prompt}");io::stdout().flush()?;}
    let mut bytes=Vec::new();
    let mut refreshed=None;
    loop{
        host.service_background().map_err(|e|io::Error::other(e.to_string()))?;
        if INTERRUPT.swap(false,std::sync::atomic::Ordering::SeqCst){return Err(ReadlineError::Interrupted);}
        let mut poll=libc::pollfd{fd:0,events:libc::POLLIN,revents:0};
        let ready=unsafe{libc::poll(&mut poll,1,25)};
        if ready<0{let error=io::Error::last_os_error();if error.kind()==io::ErrorKind::Interrupted{continue;}return Err(error.into());}
        if ready==0{
            let pending=host.wakeups.values().any(|w|w.metadata["state"]=="ready");
            if let Err(e)=host.dispatch_wakeups(){host.event("error",json!({"error":e.to_string()}));}
            if pending{refreshed=Some(editor_completion(host).map_err(|e|io::Error::other(e.to_string()))?);}
            continue;
        }
        let helper=refreshed.as_ref().unwrap_or(helper);
        // Never read ahead: foreground Python input() owns subsequent lines.
        let mut byte=0u8;let size=unsafe{libc::read(0,(&mut byte as *mut u8).cast(),1)};
        if size<0{let error=io::Error::last_os_error();if error.kind()==io::ErrorKind::Interrupted{continue;}return Err(error.into());}
        if size==0{return if bytes.is_empty(){Err(ReadlineError::Eof)}else{String::from_utf8(bytes).map_err(|e|io::Error::new(io::ErrorKind::InvalidData,e).into())};}
        if byte==b'\n'{
            if bytes.last()==Some(&b'\r'){bytes.pop();}
            let source=std::str::from_utf8(&bytes).map_err(|e|io::Error::new(io::ErrorKind::InvalidData,e))?;
            if helper.incomplete(source)?{bytes.push(b'\n');}else{return Ok(source.into());}
        }else{bytes.push(byte);}
        if bytes.len()>1_048_576{return Err(io::Error::new(io::ErrorKind::InvalidData,"editor input exceeds one MiB").into());}
    }
}
fn terminal_readline(helper:&Completion,history:&[String],output_fd:i32,host:&mut Host)->rustyline::Result<String>{
    use rustyline::error::ReadlineError;
    let mut terminal=EditTerminal::open(output_fd)?;
    let mut buffer=EditBuffer{line:String::new(),cursor:0,undo:std::collections::VecDeque::new(),killed:String::new(),history:None,draft:(String::new(),0)};
    terminal.draw(&buffer.line,buffer.cursor)?;terminal.keyboard(true)?;
    // This readiness marker is last: callers cannot race a half-configured tty.
    if terminal.display{terminal.output.write_all(b"\x1b[?2004h")?;terminal.output.flush()?;}
    let mut width=terminal.width();let mut height=terminal.height();
    let mut refreshed=None;
    loop{
        host.service_background().map_err(|e|io::Error::other(e.to_string()))?;
        if host.wakeups.values().any(|w|w.metadata["state"]=="ready")&&!terminal.input_ready(){
            // The entire edit state remains in buffer while the model owns the tty.
            drop(terminal);
            if let Err(e)=host.dispatch_wakeups(){host.event("error",json!({"error":e.to_string()}));}
            refreshed=Some(editor_completion(host).map_err(|e|io::Error::other(e.to_string()))?);
            terminal=EditTerminal::open(output_fd)?;
            terminal.draw(&buffer.line,buffer.cursor)?;terminal.keyboard(true)?;
            if terminal.display{terminal.output.write_all(b"\x1b[?2004h")?;terminal.output.flush()?;}
            width=terminal.width();height=terminal.height();
        }
        let helper=refreshed.as_ref().unwrap_or(helper);
        if INTERRUPT.swap(false,std::sync::atomic::Ordering::SeqCst){return Err(ReadlineError::Interrupted);}
        if width!=terminal.width()||height!=terminal.height(){width=terminal.width();height=terminal.height();terminal.draw(&buffer.line,buffer.cursor)?;}
        let key=match editor_key(&mut terminal,host){Err(ReadlineError::Io(e)) if e.kind()==io::ErrorKind::UnexpectedEof=>return Err(ReadlineError::Eof),other=>other?};
        let Some(key)=key else{continue;};
        match key{
            EditKey::Text(text)|EditKey::Paste(text)=>buffer.replace(buffer.cursor,buffer.cursor,&text),
            EditKey::Byte(13)=>{
                let incomplete=helper.incomplete(&buffer.line)?;
                if INTERRUPT.swap(false,std::sync::atomic::Ordering::SeqCst){return Err(ReadlineError::Interrupted);}
                if incomplete{buffer.replace(buffer.cursor,buffer.cursor,"\n");}
                else{terminal.draw(&buffer.line,buffer.cursor)?;return Ok(buffer.line);}
            },
            EditKey::Byte(10)=>buffer.replace(buffer.cursor,buffer.cursor,"\n"),
            EditKey::Byte(3)=>return Err(ReadlineError::Interrupted),
            EditKey::Byte(4) if buffer.line.is_empty()=>return Err(ReadlineError::Eof),
            EditKey::Byte(4)=>{let next=editor_next(&buffer.line,buffer.cursor);buffer.replace(buffer.cursor,next,"");},
            EditKey::Byte(8|127)=>{let previous=editor_previous(&buffer.line,buffer.cursor);buffer.replace(previous,buffer.cursor,"");},
            EditKey::Byte(1)=>buffer.cursor=editor_line_start(&buffer.line,buffer.cursor),
            EditKey::Byte(5)=>buffer.cursor=editor_line_end(&buffer.line,buffer.cursor),
            EditKey::Byte(2)=>buffer.cursor=editor_previous(&buffer.line,buffer.cursor),
            EditKey::Byte(6)=>buffer.cursor=editor_next(&buffer.line,buffer.cursor),
            EditKey::Byte(9)=>editor_complete(helper,&mut terminal,&mut buffer,host)?,
            EditKey::Byte(16)=>buffer.history(history,true),EditKey::Byte(14)=>buffer.history(history,false),
            EditKey::Byte(21|23|11)=>{
                let (start,end)=match key{EditKey::Byte(21)=>(editor_line_start(&buffer.line,buffer.cursor),buffer.cursor),
                    EditKey::Byte(23)=>(editor_word_left(&buffer.line,buffer.cursor),buffer.cursor),
                    _=>(buffer.cursor,if buffer.cursor==editor_line_end(&buffer.line,buffer.cursor){editor_next(&buffer.line,buffer.cursor)}else{editor_line_end(&buffer.line,buffer.cursor)})};
                buffer.killed=buffer.line[start..end].into();buffer.replace(start,end,"");
            },
            EditKey::Byte(25)=>{let text=buffer.killed.clone();buffer.replace(buffer.cursor,buffer.cursor,&text);},
            EditKey::Byte(31)=>if let Some((line,cursor))=buffer.undo.pop_back(){buffer.line=line;buffer.cursor=cursor;},
            EditKey::Sequence(ref sequence)=>match sequence.as_str(){
                "[A"|"OA"=>buffer.vertical(history,true),"[B"|"OB"=>buffer.vertical(history,false),
                "[C"|"OC"=>buffer.cursor=editor_next(&buffer.line,buffer.cursor),"[D"|"OD"=>buffer.cursor=editor_previous(&buffer.line,buffer.cursor),
                "[1;5D"|"alt:b"=>buffer.cursor=editor_word_left(&buffer.line,buffer.cursor),
                "[1;5C"|"alt:f"=>buffer.cursor=editor_word_right(&buffer.line,buffer.cursor),
                "[H"|"OH"|"[1~"|"[7~"=>buffer.cursor=editor_line_start(&buffer.line,buffer.cursor),
                "[F"|"OF"|"[4~"|"[8~"=>buffer.cursor=editor_line_end(&buffer.line,buffer.cursor),
                "[3~"=>{let next=editor_next(&buffer.line,buffer.cursor);buffer.replace(buffer.cursor,next,"");},_=>{}
            },_=>{}
        }
        // A queued burst is one visual edit, not an O(n²) repaint per byte.
        // Submit explicitly draws the final buffer before leaving; completion
        // draws its chosen/restored buffer once selection has finished.
        if !terminal.input_ready(){terminal.draw(&buffer.line,buffer.cursor)?;}
    }
}

impl Host {
    fn poll_commands(&mut self)->Result<bool>{
        self.poll_target(self.worker.child.id() as i32,libc::SIGINT)
    }
    fn poll_target(&mut self,pid:i32,signal:i32)->Result<bool>{
        self.service_background()?;
        let mut cancelled=false;
        if INTERRUPT.swap(false,std::sync::atomic::Ordering::SeqCst){
            self.journal.append("cancel",json!({"generation":self.generation,"source":"SIGINT"}))?;
            self.cancel_revision=self.cancel_revision.wrapping_add(1);
            unsafe{libc::kill(-pid,signal);}
            cancelled=true;
        }
        loop{
            let command=match self.incoming.as_ref().map(|r|r.try_recv()){
                Some(Ok(v))=>v,
                Some(Err(std::sync::mpsc::TryRecvError::Disconnected))=>{self.input_closed=true;break;},
                _=>break
            };
            if command["kind"]=="interrupt"{
                if !self.accept_input_control(&command)?{continue;}
                let id=command["id"].as_str().unwrap();
                self.journal.append("cancel",json!({"command_id":id,"generation":self.generation}))?;
                self.cancel_revision=self.cancel_revision.wrapping_add(1);
                unsafe{libc::kill(-pid,signal);}
                self.event("completed",json!({"command_id":id,"status":"ok"}));
                cancelled=true;
            }else if !self.background_control(&command)?{self.queue_arrival(command)?;}
        }
        Ok(cancelled)
    }
    fn accept_input_control(&mut self,command:&Value)->Result<bool>{
        let id=command["id"].as_str().unwrap_or("");
        if id.is_empty()||self.ids.contains(id){
            self.event("rejected",json!({"command_id":id,"error":"missing or duplicate command ID"}));return Ok(false);
        }
        self.journal.append("accepted",json!({"command_id":id,"command":command}))?;
        self.ids.insert(id.into());self.event("accepted",json!({"command_id":id}));Ok(true)
    }
    fn begin_input(&mut self,operation:&str,text:&str)->Result<InputPrompt>{
        let p=InputPrompt{id:format!("input{}",self.journal.seq),operation:operation.into(),
            generation:self.generation,text:text.into(),buffer:vec![]};
        let payload=p.payload();self.journal.append("input_prompt",payload.clone())?;
        self.set_state(UiState::Input,None);
        self.event("input_prompt",payload);Ok(p)
    }
    fn close_input(&mut self,p:&InputPrompt,action:&str,text:Option<&str>)->Result<Value>{
        let mut payload=p.payload();payload["action"]=json!(action);
        let response=if let Some(text)=text{
            let index=self.history["stdin"].len();
            payload["index"]=json!(index);payload["text"]=json!(text);
            self.journal.append("stdin",payload.clone())?;self.hist_push("stdin",json!(text));
            json!({"ok":true,"value":text})
        }else{input_exception(action)};
        payload.as_object_mut().unwrap().remove("text");
        self.journal.append("input_closed",payload)?;
        self.set_state(UiState::Running,None);Ok(response)
    }
    fn reply_input(&mut self,p:&InputPrompt,command:&Value)->Result<Option<(Value,bool)>>{
        let action=match command.get("action"){None=>"text",Some(v)=>v.as_str().unwrap_or("")};
        let text=command["value"].as_str();
        let id=command["id"].as_str().unwrap_or("");
        let error=if id.is_empty()||self.ids.contains(id){Some("missing or duplicate command ID")}
            else if command["prompt_id"]!=p.id||command["operation"]!=p.operation||command["worker_generation"]!=p.generation{
                Some("reply does not match the active prompt, operation and generation")
            }else if !["text","eof","cancel"].contains(&action)||(action=="text"&&text.is_none()){
                Some("stdin_reply requires text value or eof/cancel action")
            }else{None};
        if let Some(error)=error{self.event("rejected",json!({"command_id":id,"error":error}));return Ok(None);}
        if !self.accept_input_control(command)?{return Ok(None);}
        if action=="cancel"{self.journal.append("cancel",json!({"command_id":id,
            "operation":p.operation,"prompt_id":p.id,"generation":p.generation}))?;}
        let response=self.close_input(p,action,if action=="text"{text}else{None})?;
        self.event("completed",json!({"command_id":id,"status":"ok"}));Ok(Some((response,action=="cancel")))
    }
    fn terminal_input(&mut self,p:&mut InputPrompt)->Result<Option<Value>>{
        let mut fd=libc::pollfd{fd:0,events:libc::POLLIN,revents:0};
        if unsafe{libc::poll(&mut fd,1,0)}<=0{return Ok(None);}
        // Canonical terminals can release a partial line on Ctrl-D. Read bounded
        // nonblocking bytes, retaining the partial line while the event loop runs.
        let flags=unsafe{libc::fcntl(0,libc::F_GETFL)};
        if flags<0||unsafe{libc::fcntl(0,libc::F_SETFL,flags|libc::O_NONBLOCK)}<0{
            return Err(io::Error::last_os_error().into());
        }
        let result=(||->Result<Option<Value>>{
            for _ in 0..512{
                let mut byte=0u8;let count=unsafe{libc::read(0,(&mut byte as *mut u8).cast(),1)};
                if count<0{
                    let error=io::Error::last_os_error();
                    if [io::ErrorKind::WouldBlock,io::ErrorKind::Interrupted].contains(&error.kind()){return Ok(None);}
                    return Err(error.into());
                }
                if count==0&&p.buffer.is_empty(){return Ok(Some(self.close_input(p,"eof",None)?));}
                if count==0||byte==b'\n'{
                    let text=std::str::from_utf8(&p.buffer)?;
                    return Ok(Some(self.close_input(p,"text",Some(text))?));
                }
                p.buffer.push(byte);
            }
            Ok(None)
        })();
        if unsafe{libc::fcntl(0,libc::F_SETFL,flags)}<0{return Err(io::Error::last_os_error().into());}
        result
    }
}

struct InputPrompt {id:String,operation:String,generation:usize,text:String,buffer:Vec<u8>}
impl InputPrompt {
    fn payload(&self)->Value{json!({"prompt_id":self.id,"operation":self.operation,
        "worker_generation":self.generation,"prompt":self.text})}
}
fn input_exception(action:&str)->Value{
    json!({"ok":false,"exception":if action=="cancel"{"KeyboardInterrupt"}else{"EOFError"}})
}

// Idle editor IPC is separate from cell execution. The cached names are never
// queried while a cell is running; only validation uses this cloned socket.
#[derive(Clone)]
struct CompletionCatalog{
    names:Vec<String>,models:Value,home:PathBuf,python_cwd:PathBuf,
    current_model:String,configured_providers:Vec<String>,task_ids:Vec<String>,wakeup_ids:Vec<String>,
}
struct Completion {
    catalog:CompletionCatalog,
    selection:std::sync::Arc<std::sync::Mutex<Option<(usize,String)>>>,
    ipc:std::cell::RefCell<(BufReader<UnixStream>,UnixStream)>,
}
impl Completion {
    fn from_worker(worker:&mut Worker,models:Value,home:PathBuf,current_model:String,configured_providers:Vec<String>)->Result<Self>{
        worker.send(&json!({"kind":"ide_inspect"}))?;
        worker.reader.get_ref().set_read_timeout(Some(std::time::Duration::from_secs(2)))?;
        let response=worker.recv();
        worker.reader.get_ref().set_read_timeout(None)?;
        let response=response?;
        if response["kind"]!="ide_names"{return Err("invalid idle worker inspection response".into());}
        let names=response["names"].as_array().map(|a|a.iter().filter_map(|v|v.as_str().map(str::to_string)).collect()).unwrap_or_default();
        let python_cwd=response["cwd"].as_str().map(PathBuf::from).unwrap_or(std::env::current_dir()?);
        Ok(Self{catalog:CompletionCatalog{names,models,home,python_cwd,current_model,configured_providers,task_ids:vec![],wakeup_ids:vec![]},
            selection:std::sync::Arc::new(std::sync::Mutex::new(None)),ipc:std::cell::RefCell::new((
            BufReader::new(worker.reader.get_ref().try_clone()?),worker.writer.try_clone()?))})
    }
    fn picker_handler(&self)->rustyline::EventHandler{
        rustyline::EventHandler::Conditional(Box::new(PickerTab{catalog:self.catalog.clone(),selection:self.selection.clone()}))
    }
}
fn editor_completion(host:&mut Host)->Result<Completion>{
    let models=host.models();let providers=host.login_providers();
    let mut helper=Completion::from_worker(&mut host.worker,models,host.home.clone(),host.model.clone(),providers)?;
    helper.catalog.task_ids=host.bg_tasks.keys().cloned().collect();
    helper.catalog.wakeup_ids=host.wakeups.keys().cloned().collect();
    Ok(helper)
}
impl CompletionCatalog{
    fn providers(&self)->Vec<String>{
        let mut providers:Vec<_>=PROVIDERS.iter().map(|p|p.0.to_string()).collect();
        providers.extend(["github-copilot".into(),"openai-codex".into(),"codex".into()]);
        providers.extend(self.configured_providers.iter().cloned());
        if let Some(models)=self.models.as_array(){for model in models{
            if let Some(provider)=model["provider"].as_str(){providers.push(provider.into());}
        }}
        providers.sort();providers.dedup();providers
    }
}
impl rustyline::Helper for Completion{}
impl rustyline::hint::Hinter for Completion{type Hint=String;}
impl rustyline::highlight::Highlighter for Completion{}
impl rustyline::validate::Validator for Completion{
    fn validate(&self,ctx:&mut rustyline::validate::ValidationContext<'_>)
        ->rustyline::Result<rustyline::validate::ValidationResult>{
        use rustyline::validate::ValidationResult;
        Ok(if self.incomplete(ctx.input())?{ValidationResult::Incomplete}else{ValidationResult::Valid(None)})
    }
}
impl Completion{
    fn incomplete(&self,input:&str)->rustyline::Result<bool>{
        use rustyline::error::ReadlineError;
        let Some(source)=input.strip_prefix("@@").or_else(||input.strip_prefix('@')) else{
            return Ok(input.ends_with('\\'));
        };
        // A huge paste is accepted as a cell without blocking editor validation.
        if source.len()>65536{return Ok(false);}
        let mut ipc=self.ipc.borrow_mut();
        ipc.0.get_ref().set_read_timeout(Some(std::time::Duration::from_secs(2)))?;
        let result=(||->rustyline::Result<bool>{
            writeln!(ipc.1,"{}",json!({"kind":"ide_validate","source":source}))?;
            let mut line=String::new();
            if ipc.0.read_line(&mut line)?==0{return Err(ReadlineError::Eof);}
            let v:Value=serde_json::from_str(&line).map_err(|_|ReadlineError::Io(io::Error::new(io::ErrorKind::InvalidData,"invalid idle validation response")))?;
            if v["kind"]!="ide_validation"{return Err(ReadlineError::Io(io::Error::new(io::ErrorKind::InvalidData,"unexpected idle validation response")));}
            Ok(v["incomplete"]==true)
        })();
        ipc.0.get_ref().set_read_timeout(None)?;
        result
    }
}
// Larger scores win. Categories deliberately dominate all within-category
// bonuses: exact > prefix > contiguous substring > ordered subsequence.
fn fuzzy_score(query:&str,candidate:&str)->Option<i64>{
    let query=query.to_lowercase();let candidate=candidate.to_lowercase();
    if query==candidate{return Some(1_000_000);}
    if candidate.starts_with(&query){return Some(800_000-candidate.chars().count().min(10_000) as i64);}
    if let Some(at)=candidate.find(&query){return Some(600_000-at.min(10_000) as i64*2-candidate.chars().count().min(10_000) as i64);}
    let q:Vec<_>=query.chars().collect();let c:Vec<_>=candidate.chars().collect();
    if q.len()>c.len(){return None;}
    let mut next=0;let mut score=400_000;let mut last=None;
    for (i,ch) in c.iter().enumerate(){
        if next<q.len()&&*ch==q[next]{
            if i==0||matches!(c[i-1],'.'|'/'|'_'|'-'|' '){score+=20;}
            if last==Some(i.saturating_sub(1)){score+=10;}
            score-=i.min(1000) as i64;last=Some(i);next+=1;
        }
    }
    if next==q.len(){Some(score-c.len().min(10_000) as i64)}else{None}
}
fn completion_rank(mut items:Vec<(i64,String,String)>)->Vec<rustyline::completion::Pair>{
    items.sort_by(|a,b|b.0.cmp(&a.0).then(a.1.cmp(&b.1)).then(a.2.cmp(&b.2)));
    let mut seen=HashSet::new();items.into_iter().filter(|(_,display,_)|seen.insert(display.clone())).take(4096)
        .map(|(_,display,replacement)|rustyline::completion::Pair{display:terminal_safe(&display),replacement}).collect()
}
// Return the token offset and open quote without splitting a quoted/spaced path.
fn completion_token(prefix:&str,offset:usize)->(usize,Option<char>){
    let mut start=offset;let mut quoted_start=offset;let mut quote=None;let mut escaped=false;
    for (i,ch) in prefix[offset..].char_indices(){let at=offset+i;
        if escaped{escaped=false;continue;}
        if ch=='\\'&&quote!=Some('\''){escaped=true;continue;}
        if let Some(q)=quote{if ch==q{quote=None;}continue;}
        if ch=='\''||ch=='"'{quote=Some(ch);quoted_start=at+1;}
        else if ch.is_whitespace()||matches!(ch,';'|'|'|'&'){start=at+ch.len_utf8();}
    }
    (if quote.is_some(){quoted_start}else{start},quote)
}
fn completion_unescape(s:&str,python:bool,initial_quote:Option<char>)->String{
    let mut out=String::new();let mut chars=s.chars().peekable();let mut quote=initial_quote;
    while let Some(c)=chars.next(){
        if !python&&matches!(c,'\''|'"'){
            if quote==Some(c){quote=None;continue;}
            if quote.is_none(){quote=Some(c);continue;}
        }
        if c=='\\'{if let Some(&next)=chars.peek(){
            if (python&&matches!(next,'\\'|'\''|'"'))||(!python&&quote!=Some('\'')&&
                (quote.is_none()||matches!(next,'\\'|'"'|'$'|'`'))){
                out.push(chars.next().unwrap());continue;
            }
        }}out.push(c);
    }out
}
fn completion_path_word(path:&str,quote:Option<char>,python:bool)->String{
    if let Some(q)=quote{
        if !python&&q=='\''{return path.replace('\'',"'\\''");}
        let mut value=String::new();for c in path.chars(){
            if c=='\\'||c==q||(!python&&q=='"'&&matches!(c,'$'|'`')){value.push('\\');}
            value.push(c);
        }return value;
    }
    // Keep the unquoted tilde separate from the quoted filename so the shell
    // still expands HOME even when the completed basename contains spaces.
    if !python{if let Some(rest)=path.strip_prefix("~/"){
        return format!("~/{}",completion_path_word(rest,None,true));
    }}
    if path.chars().all(|c|c.is_alphanumeric()||matches!(c,'/'|'.'|'_'|'-'|'~')){return path.into();}
    format!("'{}'",path.replace('\'',"'\\''"))
}
fn completion_files(word:&str,quote:Option<char>,python:bool,cwd:&Path,command:bool)->Vec<(i64,String,String)>{
    use std::os::unix::fs::PermissionsExt;
    let word=completion_unescape(word,python,quote);
    let (directory,query)=word.rsplit_once('/').map_or(("",word.as_str()),|(d,q)|(&word[..d.len()+1],q));
    let expanded=if directory=="~/"||directory.starts_with("~/"){
        PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(&directory[2..])
    }else{PathBuf::from(directory)};
    let search=if expanded.is_absolute(){expanded}else{cwd.join(expanded)};
    let mut items=Vec::new();
    if let Ok(entries)=fs::read_dir(search){for entry in entries.take(4096).flatten(){
        let Some(name)=entry.file_name().to_str().map(str::to_string)else{continue;};
        if name.starts_with('.')&&!query.starts_with('.'){continue;}
        if name.chars().any(char::is_control){continue;}
        let Ok(meta)=entry.metadata()else{continue;};
        if command&&!meta.is_dir()&&(!meta.is_file()||meta.permissions().mode()&0o111==0){continue;}
        let Some(score)=fuzzy_score(query,&name)else{continue;};
        let suffix=if meta.is_dir(){"/"}else{""};
        let value=if command&&directory.is_empty()&&!meta.is_dir(){format!("./{name}{suffix}")}else{format!("{directory}{name}{suffix}")};
        items.push((score,format!("{directory}{name}{suffix}"),completion_path_word(&value,quote,python)));
    }}items
}
fn completion_executables(query:&str,quote:Option<char>)->Vec<(i64,String,String)>{
    use std::os::unix::fs::PermissionsExt;
    let mut items=Vec::new();let mut visited=HashSet::new();let mut count=0;
    // POSIX shell builtins are useful even when they have no PATH executable.
    for name in ["cd","echo","printf","pwd","command","export","unset","alias","unalias","umask",
        "read","test","true","false","exec","exit","wait","kill","jobs","fg","bg","set","shift","trap"]{
        if let Some(score)=fuzzy_score(query,name){items.push((score+1,name.into(),completion_path_word(name,quote,false)));}
    }
    if let Some(path)=std::env::var_os("PATH"){for dir in std::env::split_paths(&path).take(64){
        if !visited.insert(dir.clone()){continue;}
        if let Ok(entries)=fs::read_dir(dir){for entry in entries.take(4096).flatten(){
            count+=1;if count>16384{return items;}
            let Some(name)=entry.file_name().to_str().map(str::to_string)else{continue;};
            if name.chars().any(char::is_control){continue;}
            let Some(score)=fuzzy_score(query,&name)else{continue;};
            if entry.metadata().is_ok_and(|m|m.is_file()&&m.permissions().mode()&0o111!=0){
                items.push((score+1,name.clone(),completion_path_word(&name,quote,false)));
            }
        }}
    }}items
}
impl rustyline::completion::Completer for Completion{
    type Candidate=rustyline::completion::Pair;
    fn complete(&self,line:&str,pos:usize,_ctx:&rustyline::Context<'_>)
        ->rustyline::Result<(usize,Vec<Self::Candidate>)>{
        if let Some((start,value))=self.selection.lock().unwrap().take(){
            return Ok((start,vec![rustyline::completion::Pair{display:terminal_safe(&value),replacement:value}]));
        }
        self.catalog.candidates(line,pos,false)
    }
}
impl CompletionCatalog{
    fn candidates(&self,line:&str,pos:usize,all:bool)
        ->rustyline::Result<(usize,Vec<rustyline::completion::Pair>)>{
        const COMMANDS:&[&str]=&["/help","/hotkeys","/model","/models","/effort","/think","/thinking","/status",
            "/context","/config","/login","/logout","/auth","/session","/sessions","/resume","/recovery",
            "/reset","/new","/compact","/interrupt","/quit","/exit","/tasks","/task","/bg","/wakeups","/wakeup"];
        let Some(prefix)=line.get(..pos)else{return Ok((pos,vec![]));};
        let mut items=Vec::new();
        if prefix.starts_with('/'){
            let Some(space)=prefix.find(char::is_whitespace)else{
                for command in COMMANDS{if let Some(score)=fuzzy_score(if all{""}else{prefix},command){items.push((score,command.to_string(),command.to_string()));}}
                return Ok((0,completion_rank(items)));
            };
            let command=&prefix[..space];let args=prefix[space..].trim_start();let arg_start=prefix.len()-args.len();
            let (start,_)=completion_token(prefix,arg_start);let raw_word=&prefix[start..];let word=if all{""}else{raw_word};
            if command=="/model"||command=="/models"{
                if command=="/model"&&start==arg_start{if let Some(score)=fuzzy_score(word,"list"){items.push((score,"list".into(),"list".into()));}}
                if line[..start].trim_end()=="/model list"||line[..start].trim_end()=="/models"{
                    if let Some(score)=fuzzy_score(word,"refresh"){items.push((score,"refresh".into(),"refresh".into()));}
                }
                if let Some(models)=self.models.as_array(){for model in models{
                    if model["image_output"]==true{continue;}
                    let Some(id)=model["id"].as_str()else{continue;};
                    let name=model["name"].as_str().unwrap_or("");
                    // Provider aliases are alternate spellings, not additional
                    // picker options. Preserve the user's explicit alias.
                    let mut value=id.to_string();
                    if let Some((provider,model_id))=id.split_once('/'){
                        for (alias,canonical) in [("codex","openai-codex"),("copilot","github-copilot"),("google","google-gemini-cli")]{
                            if provider==canonical&&raw_word.split_once('/').is_some_and(|(typed,_)|typed.eq_ignore_ascii_case(alias)){
                                value=format!("{alias}/{model_id}");break;
                            }
                        }
                    }
                    let score=fuzzy_score(word,&value).or_else(||fuzzy_score(word,name).map(|s|s-100))
                        .or_else(||model["aliases"].as_array().and_then(|aliases|aliases.iter().filter_map(|a|a.as_str().and_then(|a|{
                            let alias=if raw_word.contains('/'){format!("{}/{a}",value.split_once('/').unwrap().0)}else{a.into()};fuzzy_score(word,&alias)
                        })).max()));
                    if let Some(score)=score{
                        let aliases=model["aliases"].as_array().map(|a|a.iter().filter_map(Value::as_str).collect::<Vec<_>>().join(", ")).unwrap_or_default();
                        let display=if aliases.is_empty(){format!("{value}  {name}")}else{format!("{value}  {name}  [{aliases}]")};
                        items.push((score,display,value));
                    }
                }}
            }else if matches!(command,"/effort"|"/think"|"/thinking"){
                let model=self.models.as_array().and_then(|models|models.iter().find(|m|m["id"]==self.current_model));
                let supported=model.and_then(|m|m["reasoning_efforts"].as_array());
                let max=model.is_some_and(|m|m["reasoning"]==true&&m["api"]=="anthropic-messages"&&self.current_model.contains("claude-opus-4-6"));
                for effort in ["off","minimal","low","medium","high","xhigh","max"]{
                    if supported.map_or(effort=="max"&&!max,|levels|!levels.iter().any(|level|level==effort)){continue;}
                    if let Some(score)=fuzzy_score(word,effort){items.push((score,effort.into(),effort.into()));}
                }
            }else if matches!(command,"/login"|"/logout"|"/auth"){
                let provider=prefix[arg_start..].split_whitespace().next().unwrap_or("");
                let providers=if start==arg_start{self.providers()}else if command=="/login"{
                    // Mirror execution's exact/unique-fuzzy provider resolution
                    // using only the immutable idle snapshot, never host IPC.
                    let known=self.providers();let query=provider_alias(provider);
                    let exact=known.iter().find(|p|p.eq_ignore_ascii_case(query)).cloned();
                    let resolved=exact.or_else(||{
                        let matches:Vec<_>=known.into_iter().filter(|p|fuzzy_score(query,p).is_some()).collect();
                        if matches.len()==1{matches.into_iter().next()}else{None}
                    });
                    match resolved.as_deref(){
                        Some("anthropic")=>vec!["browser","manual","api-key","oauth"],
                        Some("openai-codex")=>vec!["browser","manual","device","oauth"],
                        Some("github-copilot")=>vec!["device","oauth"],
                        Some(_)=>vec!["api-key"],
                        None=>vec![]
                    }.into_iter().map(str::to_string).collect()
                }else{vec![]};
                for provider in providers{if let Some(score)=fuzzy_score(word,&provider){items.push((score,provider.clone(),provider));}}
            }else if matches!(command,"/tasks"|"/task"|"/bg"|"/wakeup"|"/quit"|"/exit"|"/new"){
                let tokens:Vec<_>=args.split_whitespace().collect();
                let at_new=args.ends_with(char::is_whitespace);
                let slot=tokens.len().saturating_sub(usize::from(!at_new));
                let task_slot=command=="/task"&&(slot==0||slot==1&&tokens.first().is_some_and(|s|["logs","kill"].contains(s)));
                let wakeup_slot=command=="/wakeup"&&slot==1;
                if task_slot||wakeup_slot{
                    for id in if task_slot{&self.task_ids}else{&self.wakeup_ids}{
                        if let Some(score)=fuzzy_score(word,id){items.push((score,id.clone(),id.clone()));}
                    }
                }
                let suggestions:&[&str]=match command{
                    "/tasks"=>&["all","running","finished","succeeded","failed","timed_out","cancelled","killed","outcome_unknown"],
                    "/task" if args.starts_with("logs ")&&slot>=2=>&["stdout","stderr","both"],
                    "/task" if args.starts_with("kill ")&&slot>=2=>&["--force"],
                    "/task" if slot==0=>&["logs","kill"],"/task"=>&[],
                    "/bg" if slot==0=>&["shell","python"],"/bg"=>&[],
                    "/wakeup" if slot==0=>&["cancel","run"],"/wakeup"=>&[],
                    _=>&["--cancel-tasks"]
                };
                for suggestion in suggestions{if let Some(score)=fuzzy_score(word,suggestion){items.push((score,suggestion.to_string(),suggestion.to_string()));}}
            }else if command=="/resume"{
                let directory=self.home.join("sessions");
                if let Ok(entries)=fs::read_dir(directory){for entry in entries.take(4096).flatten(){
                    let name=entry.file_name().to_string_lossy().into_owned();
                    if let Some(score)=fuzzy_score(word,&name){items.push((score,name,entry.path().to_string_lossy().into_owned()));}
                }}
            }else{items=completion_files(completion_file_query(raw_word,all),None,false,&std::env::current_dir()?,false);}
            return Ok((start,completion_rank(items)));
        }
        if prefix.starts_with('@'){
            let offset=if prefix.starts_with("@@"){2}else{1};
            let (quoted_start,quote)=completion_token(prefix,offset);
            if quote.is_some(){return Ok((quoted_start,completion_rank(completion_files(completion_file_query(&prefix[quoted_start..],all),quote,true,&self.python_cwd,false))));}
            let start=prefix[offset..].char_indices().rev().find(|(_,c)|!c.is_alphanumeric()&&!matches!(c,'_'|'.'))
                .map_or(offset,|(i,c)|offset+i+c.len_utf8());
            let word=if all{""}else{&prefix[start..]};let qualifier=word.rsplit_once('.').map(|p|p.0);
            for name in &self.names{
                let score=if let Some(qualifier)=qualifier{
                    name.rsplit_once('.').filter(|(q,_)|*q==qualifier)
                        .and_then(|(_,n)|fuzzy_score(word.rsplit_once('.').unwrap().1,n))
                        .or_else(||fuzzy_score(word,name).map(|s|s-50_000))
                }else{fuzzy_score(word,name).map(|s|if name.contains('.'){s-100_000}else{s})};
                if let Some(score)=score{items.push((score,name.clone(),name.clone()));}
            }
            return Ok((start,completion_rank(items)));
        }
        let offset=if prefix.starts_with("!!"){2}else if prefix.starts_with('!'){1}else{0};
        let (start,quote)=completion_token(prefix,offset);let word=&prefix[start..];
        let before=prefix[offset..start].trim().trim_end_matches(['\'','"']).trim_end();
        let command=offset>0&&(before.is_empty()||before.ends_with([';','|','&']));
        items=completion_files(completion_file_query(word,all),quote,false,&std::env::current_dir()?,command);
        if command&&!word.contains('/'){
            let query=if all{String::new()}else{completion_unescape(word,false,quote)};
            items.extend(completion_executables(&query,quote));
        }
        Ok((start,completion_rank(items)))
    }
}

fn completion_file_query(word:&str,all:bool)->&str{
    if all{word.rfind('/').map_or("",|i|&word[..i+1])}else{word}
}
// Only this immutable, between-cell snapshot enters the key handler. Python IPC
// and the worker are deliberately absent from selection and query filtering.
struct PickerTab{catalog:CompletionCatalog,selection:std::sync::Arc<std::sync::Mutex<Option<(usize,String)>> >}
impl rustyline::ConditionalEventHandler for PickerTab{
    fn handle(&self,_event:&rustyline::Event,_count:rustyline::RepeatCount,_positive:bool,ctx:&rustyline::EventContext<'_>)->Option<rustyline::Cmd>{
        let (start,matching)=self.catalog.candidates(ctx.line(),ctx.pos(),false).ok()?;
        let selected=if matching.len()==1{Some(matching[0].replacement.clone())}
        else if matching.is_empty(){None}else{
            let (_,choices)=self.catalog.candidates(ctx.line(),ctx.pos(),true).ok()?;
            let prefix=&ctx.line()[..ctx.pos()];
            let python=prefix.starts_with('@');
            let offset=if prefix.starts_with("@@")||prefix.starts_with("!!"){2}else if python||prefix.starts_with('!'){1}else{0};
            let (_,quote)=completion_token(prefix,offset);
            let query=completion_unescape(&prefix[start..],python,quote);
            picker_select(&choices,&query,1,1,1).ok().flatten()
        };
        Some(if let Some(value)=selected{
            // Cmd::Replace is unsuitable here: Emacs redo overrides its count,
            // and insert_str leaves the cursor before inserted text. Complete
            // delegates atomic range replacement + cursor placement to rustyline.
            *self.selection.lock().unwrap()=Some((start,value));rustyline::Cmd::Complete
        }else{rustyline::Cmd::Repaint})
    }
}
struct PickerTerminal{
    input:File,tty:File,previous:libc::termios,active:bool,
    slots:usize,cursor_row:usize,origin_up:usize,prompt_rows:usize,
}
impl PickerTerminal{
    fn open(output_fd:i32,prompt_rows:usize,origin_up:usize)->Result<Option<Self>>{
        use std::os::fd::FromRawFd;
        let input=unsafe{libc::dup(0)};if input<0{return Err(io::Error::last_os_error().into());}
        let input=unsafe{File::from_raw_fd(input)};
        let tty=unsafe{libc::dup(output_fd)};if tty<0{return Err(io::Error::last_os_error().into());}
        let tty=unsafe{File::from_raw_fd(tty)};
        let mut size=unsafe{std::mem::zeroed::<libc::winsize>()};
        let height=if unsafe{libc::ioctl(tty.as_raw_fd(),libc::TIOCGWINSZ,&mut size)}==0&&size.ws_row>0{size.ws_row as usize}else{24};
        // At most half the screen: reserve room for the source and recent output.
        // Multiline input can consume that room; a tiny screen declines the menu.
        let slots=height.saturating_sub(prompt_rows).min((height/2).max(2)).min(8);
        if slots<2{return Ok(None);}
        let mut previous=unsafe{std::mem::zeroed::<libc::termios>()};
        if unsafe{libc::tcgetattr(input.as_raw_fd(),&mut previous)}<0{return Err(io::Error::last_os_error().into());}
        let mut raw=previous;raw.c_lflag&=!(libc::ICANON|libc::ECHO|libc::ISIG);
        raw.c_cc[libc::VMIN]=1;raw.c_cc[libc::VTIME]=0;
        if unsafe{libc::tcsetattr(input.as_raw_fd(),libc::TCSANOW,&raw)}<0{return Err(io::Error::last_os_error().into());}
        let mut guard=Self{input,tty,previous,active:false,slots:0,cursor_row:0,origin_up:origin_up.max(1),prompt_rows};
        guard.tty.write_all(b"\r")?;
        if guard.origin_up>1{write!(guard.tty,"\x1b[{}B",guard.origin_up-1)?;}
        // Newlines reserve owned rows and naturally scroll at the bottom. The
        // source scrolls with them; no saved absolute cursor becomes stale.
        for _ in 0..slots{
            guard.tty.write_all(b"\r\n")?;
            if guard.active{guard.cursor_row+=1;}guard.active=true;guard.slots+=1;
        }
        guard.top()?;guard.tty.flush()?;Ok(Some(guard))
    }
    fn top(&mut self)->io::Result<()>{
        self.tty.write_all(b"\r")?;
        if self.cursor_row>0{write!(self.tty,"\x1b[{}A",self.cursor_row)?;}
        self.cursor_row=0;Ok(())
    }
    fn byte(&mut self,timeout:i32)->Result<Option<u8>>{
        use std::os::fd::AsRawFd;
        let mut poll=libc::pollfd{fd:self.input.as_raw_fd(),events:libc::POLLIN,revents:0};
        let ready=unsafe{libc::poll(&mut poll,1,timeout)};
        if ready<0{
            let error=io::Error::last_os_error();
            if error.kind()==io::ErrorKind::Interrupted{return Ok(None);}
            return Err(error.into());
        }
        if ready==0{return Ok(None);}
        let mut byte=[0];if std::io::Read::read(&mut self.input,&mut byte)?==0{return Err("selection input closed".into());}
        Ok(Some(byte[0]))
    }
    fn size(&self)->(usize,usize){
        use std::os::fd::AsRawFd;
        let mut size=unsafe{std::mem::zeroed::<libc::winsize>()};
        let rows=if unsafe{libc::ioctl(self.tty.as_raw_fd(),libc::TIOCGWINSZ,&mut size)}==0&&size.ws_row>0{size.ws_row as usize}else{24};
        (terminal_width_for(self.tty.as_raw_fd()).max(1),rows.max(1))
    }
}
impl Drop for PickerTerminal{
    fn drop(&mut self){
        use std::os::fd::AsRawFd;
        if self.active{
            let _=self.top();
            // Clear only below the prompt, return to its original caret row.
            let _=self.tty.write_all(b"\x1b[0J");
            let _=write!(self.tty,"\x1b[{}A\r\x1b[?25h",self.origin_up);
            let _=self.tty.flush();
        }
        unsafe{libc::tcsetattr(self.input.as_raw_fd(),libc::TCSANOW,&self.previous);}
    }
}
fn picker_clip(text:&str,width:usize)->String{
    let mut result=String::new();let mut used=0;
    for (cluster,columns) in terminal_clusters(&terminal_safe(text)){
        if used+columns>width{break;}result.push_str(&cluster);used+=columns;
    }result
}
fn picker_filter(choices:&[rustyline::completion::Pair],query:&str)->Vec<usize>{
    let mut ranked:Vec<_>=choices.iter().enumerate().filter_map(|(index,pair)|{
        if pair.replacement.chars().any(char::is_control){return None;}
        fuzzy_score(query,&pair.replacement).or_else(||fuzzy_score(query,&pair.display).map(|score|score-100))
            .map(|score|(score+if pair.replacement.starts_with(query){50}else{0},index))
    }).collect();
    ranked.sort_by(|a,b|b.0.cmp(&a.0).then(a.1.cmp(&b.1)));
    ranked.into_iter().map(|p|p.1).collect()
}
fn picker_draw(terminal:&mut PickerTerminal,choices:&[rustyline::completion::Pair],query:&str,filtered:&[usize],selected:usize)->Result<()>{
    let (width,_)=terminal.size();let slots=terminal.slots;
    let mut lines=if slots==2{vec![format!("Fuzzy select · {query}")]}
        else{vec!["Fuzzy select — Enter picks".into(),format!("Find: {query}")]};
    if slots>=6{lines.push("↑↓/Tab move · Esc cancels".into());}
    let footer=usize::from(slots>=5);let available=slots.saturating_sub(lines.len()+footer).max(1);
    let start=selected.saturating_sub(available/2).min(filtered.len().saturating_sub(available));
    if filtered.is_empty(){lines.push("No matches — edit query".into());}
    else{for (position,index) in filtered.iter().enumerate().skip(start).take(available){
        lines.push(format!("{}{}",if position==selected{"→ "}else{"  "},choices[*index].display));
    }}
    if footer>0{lines.push(format!("{}/{}",if filtered.is_empty(){0}else{selected+1},filtered.len()));}
    terminal.top()?;terminal.tty.write_all(b"\x1b[?25l")?;
    for index in 0..slots{
        if index>0{terminal.tty.write_all(b"\r\n")?;terminal.cursor_row+=1;}
        terminal.tty.write_all(b"\x1b[2K")?;
        if let Some(line)=lines.get(index){
            let clipped=picker_clip(line,width.saturating_sub(1).max(1));
            let style=if index==0{"1;36"}else if index==1&&slots>2{"36"}else if line.starts_with("→ "){"1;7"}else if line.starts_with("  "){"0"}else{"2"};
            terminal.tty.write_all(terminal_styled_for(&clipped,style,terminal.tty.as_raw_fd()).as_bytes())?;
        }
    }
    let query_row=usize::from(slots>2);
    let query_line=if slots==2{format!("Fuzzy select · {}",terminal_safe(query))}else{format!("Find: {}",terminal_safe(query))};
    let cursor=terminal_columns(&query_line).min(width.saturating_sub(1));
    terminal.tty.write_all(b"\r")?;
    let up=terminal.cursor_row-query_row;if up>0{write!(terminal.tty,"\x1b[{up}A")?;}
    terminal.cursor_row=query_row;
    if cursor>0{write!(terminal.tty,"\x1b[{cursor}C")?;}
    terminal.tty.write_all(b"\x1b[?25h")?;terminal.tty.flush()?;Ok(())
}
// Nested selection keeps bracketed paste enabled. Consume it atomically so
// pasted Enter/Escape/control bytes can never select, navigate or submit cells.
fn picker_paste_service(terminal:&mut PickerTerminal,service:&mut impl FnMut()->Result<()>)->Result<Option<String>>{
    let mut paste=Vec::new();let mut overflow=false;
    loop{
        service()?;
        if INTERRUPT.load(std::sync::atomic::Ordering::SeqCst){
            unsafe{libc::tcflush(terminal.input.as_raw_fd(),libc::TCIFLUSH);}
            return Ok(None);
        }
        let Some(byte)=terminal.byte(50)?else{continue;};paste.push(byte);
        if paste.ends_with(b"\x1b[201~"){
            paste.truncate(paste.len()-6);
            if overflow{return Ok(None);}
            let Ok(text)=String::from_utf8(paste)else{return Ok(None);};
            // Pasted multiline text is one query edit. Collapse whitespace and
            // drop other controls; they are data, never picker key commands.
            let text:String=text.chars().map(|c|if c.is_whitespace(){' '}else{c})
                .filter(|c|!c.is_control()).collect();
            return Ok(Some(text.split_whitespace().collect::<Vec<_>>().join(" ")));
        }
        if paste.len()>1_048_576+6{overflow=true;paste.drain(..paste.len()-6);}
    }
}
fn picker_select(choices:&[rustyline::completion::Pair],initial_query:&str,output_fd:i32,prompt_rows:usize,origin_up:usize)->Result<Option<String>>{
    picker_select_service(choices,initial_query,output_fd,prompt_rows,origin_up,||Ok(()))
}
fn picker_select_service(choices:&[rustyline::completion::Pair],initial_query:&str,output_fd:i32,prompt_rows:usize,origin_up:usize,mut service:impl FnMut()->Result<()>)->Result<Option<String>>{
    if unsafe{libc::isatty(0)}!=1||unsafe{libc::isatty(output_fd)}!=1||std::env::var("TERM").is_ok_and(|t|t=="dumb"){return Ok(None);}
    let Some(mut terminal)=PickerTerminal::open(output_fd,prompt_rows,origin_up)?else{return Ok(None);};
    let mut query:String=initial_query.chars().take(512).collect();let mut filtered=picker_filter(choices,&query);
    let mut selected=0usize;let mut dirty=true;let mut size=terminal.size();
    loop{
        service()?;
        if INTERRUPT.swap(false,std::sync::atomic::Ordering::SeqCst){return Ok(None);}
        if size!=terminal.size(){
            size=terminal.size();
            if size.1.saturating_sub(terminal.prompt_rows)<terminal.slots{return Ok(None);}
            dirty=true;
        }
        if dirty{picker_draw(&mut terminal,choices,&query,&filtered,selected)?;dirty=false;}
        let Some(byte)=terminal.byte(50)?else{continue;};
        let mut edit=false;
        match byte{
            3|4=>{INTERRUPT.store(false,std::sync::atomic::Ordering::SeqCst);return Ok(None);},
            b'\r'|b'\n'=>if let Some(index)=filtered.get(selected){return Ok(Some(choices[*index].replacement.clone()));},
            b'\t'|14=>{if !filtered.is_empty(){selected=(selected+1)%filtered.len();dirty=true;}},
            16=>{if !filtered.is_empty(){selected=if selected==0{filtered.len()-1}else{selected-1};dirty=true;}},
            8|127=>{query.pop();edit=true;},
            21=>{query.clear();edit=true;},
            23=>{while query.ends_with(char::is_whitespace){query.pop();}while !query.is_empty()&&!query.ends_with(char::is_whitespace){query.pop();}edit=true;},
            27=>{
                let Some(next)=terminal.byte(35)?else{return Ok(None);};
                if next!=b'['&&next!=b'O'{return Ok(None);}
                let mut sequence=vec![next];
                for _ in 0..256{
                    let Some(key)=terminal.byte(35)?else{return Ok(None);};sequence.push(key);
                    if (0x40..=0x7e).contains(&key){break;}
                }
                if !sequence.last().is_some_and(|b|(0x40..=0x7e).contains(b)){
                    return Err("oversized picker terminal key report".into());
                }
                match sequence.as_slice(){
                    b"[A"|b"[Z"|b"OA" if !filtered.is_empty()=>{
                        selected=if selected==0{filtered.len()-1}else{selected-1};dirty=true;
                    },
                    b"[B"|b"OB" if !filtered.is_empty()=>{selected=(selected+1)%filtered.len();dirty=true;},
                    b"[200~"=>if let Some(text)=picker_paste_service(&mut terminal,&mut service)?{
                        if query.len()+text.len()<=2048{query.push_str(&text);edit=true;}
                    },
                    _=>{}
                }
            },
            c if c>=32=>{
                let length=if c<128{1}else if c&0xe0==0xc0{2}else if c&0xf0==0xe0{3}else if c&0xf8==0xf0{4}else{0};
                if length>0{
                    let mut bytes=vec![c];for _ in 1..length{if let Some(c)=terminal.byte(100)?{bytes.push(c);}else{break;}}
                    if let Ok(text)=std::str::from_utf8(&bytes){
                        if query.len()+text.len()<=2048&&!text.chars().any(char::is_control){query.push_str(text);edit=true;}
                    }
                }
            },
            _=>{}
        }
        if edit{filtered=picker_filter(choices,&query);selected=0;dirty=true;}
    }
}

impl Host {
    fn deliver_steering(&mut self)->Result<bool>{
        self.poll_commands()?;
        let Some(pos)=self.pending.iter().position(|v|
            v["kind"]=="submit" && v["mode"].as_str().unwrap_or("steering")=="steering")
            else{return Ok(false)};
        let command=self.pending.remove(pos).unwrap();
        let id=command["id"].as_str().ok_or("queued command ID required")?;
        let record=self.queued.remove(id).ok_or("missing accepted queue arrival")?;
        let (index,sequence)=record.ok_or("queued steering has no user record")?;
        self.select_user(command["text"].as_str().ok_or("queued text required")?,index,sequence)?;
        self.journal.append("queue_delivered",json!({"command_id":id,"mode":"steering"}))?;
        self.event("completed",json!({"command_id":id,"status":"delivered"}));
        self.stop=false;self.stop_wakeup=None;
        Ok(true)
    }
}

static INTERRUPT:std::sync::atomic::AtomicBool=std::sync::atomic::AtomicBool::new(false);
extern "C" fn handle_sigint(_:libc::c_int){
    INTERRUPT.store(true,std::sync::atomic::Ordering::SeqCst);
}
fn install_signals(){
    unsafe{libc::signal(libc::SIGINT,handle_sigint as *const () as libc::sighandler_t);}
}

impl Host {
    fn http_json(&mut self,request:reqwest::RequestBuilder,operation:&str)->Result<Value>{
        let rt=tokio::runtime::Builder::new_current_thread().enable_all().build()?;
        rt.block_on(async{
            let future=async{
                let response=request.send().await?;
                let status=response.status();
                let body:Value=response.json().await?;
                if !status.is_success(){
                    let detail=if operation=="auth"{String::new()}else{format!(": {body}")};
                    return Err::<Value,Box<dyn std::error::Error>>(format!("provider HTTP {status}{detail}").into());
                }
                Ok(body)
            };
            tokio::pin!(future);
            loop{
                tokio::select!{
                    response=&mut future=>return response,
                    _=tokio::time::sleep(std::time::Duration::from_millis(15))=>{
                        self.service_background()?;
                        let mut cancel=INTERRUPT.swap(false,std::sync::atomic::Ordering::SeqCst);
                        while let Some(command)=self.incoming.as_ref().and_then(|r|r.try_recv().ok()){
                            if command["kind"]=="interrupt"{
                                if !self.accept_input_control(&command)?{continue;}
                                self.event("completed",json!({"command_id":command["id"],"status":"ok"}));cancel=true;
                            }else if !self.background_control(&command)?{self.queue_arrival(command)?;}
                        }
                        if cancel{
                            self.journal.append("provider_cancelled",json!({"operation":operation,"billing":"unknown"}))?;
                            self.cancel_revision=self.cancel_revision.wrapping_add(1);
                            return Err("provider request cancelled; outcome/billing may be unknown".into());
                        }
                    }
                }
            }
        })
    }
}

impl Host {
    fn history_value(&self,name:&str,index:usize)->Result<Value>{
        if name=="events"{return self.journal.event(index);}
        let value=self.history.get(name).and_then(|v|v.get(index)).ok_or("history index missing")?;
        if let Some(seq)=value["$event"].as_u64(){
            let ev=self.journal.event(seq as usize)?;
            let p=&ev["payload"];
            return Ok(match name{
                "code"=>p["source"].clone(),
                "user"|"stdin"|"say"=>p["text"].clone(),
                _=>p.clone()
            });
        }
        if let Some(chunks)=value["$chunks"].as_array(){
            let mut bytes=Vec::new();
            for chunk in chunks{
                let ev=self.journal.event(chunk.as_u64().ok_or("invalid chunk")? as usize)?;
                bytes.extend(B64.decode(ev["payload"]["base64"].as_str().ok_or("missing stream bytes")?)?);
            }
            return Ok(json!(String::from_utf8_lossy(&bytes)));
        }
        Ok(value.clone())
    }
    fn history_last(&self,name:&str)->Result<String>{
        let n=self.history.get(name).ok_or("unknown stream")?.len().checked_sub(1).ok_or("empty stream")?;
        Ok(self.history_value(name,n)?.as_str().unwrap_or("").into())
    }

}

const PROVIDERS:&[(&str,&str,&str)]=&[
 ("openai","https://api.openai.com/v1","OPENAI_API_KEY"),
 ("anthropic","https://api.anthropic.com/v1","ANTHROPIC_API_KEY"),
 ("google","https://generativelanguage.googleapis.com/v1beta","GEMINI_API_KEY"),
 ("groq","https://api.groq.com/openai/v1","GROQ_API_KEY"),
 ("cerebras","https://api.cerebras.ai/v1","CEREBRAS_API_KEY"),
 ("mistral","https://api.mistral.ai/v1","MISTRAL_API_KEY"),
 ("xai","https://api.x.ai/v1","XAI_API_KEY"),
 ("openrouter","https://openrouter.ai/api/v1","OPENROUTER_API_KEY"),
 ("zai","https://api.z.ai/api/paas/v4","ZAI_API_KEY"),
 ("opencode","https://opencode.ai/zen/v1","OPENCODE_API_KEY"),
 ("huggingface","https://router.huggingface.co/v1","HF_TOKEN"),
 ("minimax","https://api.minimax.io/v1","MINIMAX_API_KEY"),
 ("minimax-cn","https://api.minimaxi.com/v1","MINIMAX_API_KEY")
];
fn load_json(path:&Path)->Result<Value>{
    if !path.exists(){return Ok(json!({}));}
    Ok(serde_json::from_reader(File::open(path)?)?)
}
fn write_private_json(path:&Path,value:&Value)->Result<()>{
    let tmp=path.with_extension(format!("tmp-{}",std::process::id()));
    let mut file=OpenOptions::new().create_new(true).write(true).mode(0o600).open(&tmp)?;
    file.write_all(serde_json::to_string_pretty(value)?.as_bytes())?;file.sync_all()?;
    fs::rename(&tmp,path)?;
    File::open(path.parent().ok_or("missing directory")?)?.sync_all()?;Ok(())
}
impl Host{
    fn credential(&self,provider:&str,env:&str)->String{
        let stored=&self.auth[provider];
        stored["key"].as_str().filter(|s|!s.trim().is_empty())
            .or(stored["access"].as_str().filter(|s|!s.trim().is_empty())).map(str::to_string)
            .unwrap_or_else(||std::env::var(env).unwrap_or_default())
    }
    fn configure(&mut self)->Result<()>{
        if let Some(model)=self.config["model"].as_str(){self.model=model.into();}
        if let Ok(model)=std::env::var("PY_MODEL"){self.model=model;}
        if let Some(effort)=self.config["effort"].as_str(){self.effort=effort.into();}
        self.set_model(self.model.clone())?;
        Ok(())
    }
    fn model_limit(&self,model:&str)->Result<usize>{
        let canonical=model.split_once('/').map(|(p,id)|format!("{}/{id}",provider_alias(p))).unwrap_or(model.into());
        let models=self.models();
        let mut limit=models.as_array().unwrap().iter().find(|v|v["id"]==canonical)
            .and_then(|descriptor|descriptor["context_limit"].as_u64()).map_or(self.context_limit,|n|n as usize);
        if let Ok(value)=std::env::var("PY_CONTEXT_LIMIT"){limit=value.parse()?;}
        Ok(limit)
    }
    fn set_model(&mut self,model:String)->Result<()>{
        let limit=self.model_limit(&model)?;
        if self.startup_ready{self.check_model_system_budget(&model,limit)?;}
        self.context_limit=limit;self.model=model;Ok(())
    }
    fn status(&self)->Value{json!({"state":self.state.label(),"cells":self.cells,"active_cell":self.active_cell,
        "model":self.model,"effort":self.effort,
        "context_usage":self.context_usage(),"usage":self.usage,"queue_depth":self.pending.len(),
        "worker_generation":self.generation,"startup_ready":self.startup_ready,"session":self.journal.path,
        "background_tasks":self.bg_tasks.len(),"active_background_tasks":self.bg_tasks.values().filter(|t|t.child.is_some()).count(),
        "pending_wakeups":self.wakeups.values().filter(|w|matches!(w.metadata["state"].as_str(),Some("scheduled"|"ready"|"pending_confirmation"))).count()})}
}
const CATALOG:&str=r####"[{"id":"anthropic/claude-haiku-4-5","name":"Claude Haiku 4.5 (latest)","provider":"anthropic","base_url":"https://api.anthropic.com","api":"anthropic-messages","context_limit":200000,"max_tokens":64000,"image_input":true,"reasoning":true,"image_output":false},{"id":"anthropic/claude-opus-4-6","name":"Claude Opus 4.6","provider":"anthropic","base_url":"https://api.anthropic.com","api":"anthropic-messages","context_limit":200000,"max_tokens":128000,"image_input":true,"reasoning":true,"image_output":false},{"id":"anthropic/claude-sonnet-4-6","name":"Claude Sonnet 4.6","provider":"anthropic","base_url":"https://api.anthropic.com","api":"anthropic-messages","context_limit":200000,"max_tokens":64000,"image_input":true,"reasoning":true,"image_output":false},{"id":"cerebras/gpt-oss-120b","name":"GPT OSS 120B","provider":"cerebras","base_url":"https://api.cerebras.ai/v1","api":"openai-completions","context_limit":131072,"max_tokens":32768,"image_input":false,"reasoning":true,"image_output":false},{"id":"google/gemini-2.5-flash","name":"Gemini 2.5 Flash","provider":"google","base_url":"https://generativelanguage.googleapis.com/v1beta","api":"google-generative-ai","context_limit":1048576,"max_tokens":65536,"image_input":true,"reasoning":true,"image_output":false},{"id":"google/gemini-2.5-pro","name":"Gemini 2.5 Pro","provider":"google","base_url":"https://generativelanguage.googleapis.com/v1beta","api":"google-generative-ai","context_limit":1048576,"max_tokens":65536,"image_input":true,"reasoning":true,"image_output":false},{"id":"google/gemini-3-pro-preview","name":"Gemini 3 Pro Preview","provider":"google","base_url":"https://generativelanguage.googleapis.com/v1beta","api":"google-generative-ai","context_limit":1000000,"max_tokens":64000,"image_input":true,"reasoning":true,"image_output":false},{"id":"groq/llama-3.3-70b-versatile","name":"Llama 3.3 70B Versatile","provider":"groq","base_url":"https://api.groq.com/openai/v1","api":"openai-completions","context_limit":131072,"max_tokens":32768,"image_input":false,"reasoning":false,"image_output":false},{"id":"mistral/mistral-large-latest","name":"Mistral Large","provider":"mistral","base_url":"https://api.mistral.ai/v1","api":"openai-completions","context_limit":262144,"max_tokens":262144,"image_input":true,"reasoning":false,"image_output":false},{"id":"openai/gpt-4.1","name":"GPT-4.1","provider":"openai","base_url":"https://api.openai.com/v1","api":"openai-responses","context_limit":1047576,"max_tokens":32768,"image_input":true,"reasoning":false,"image_output":false},{"id":"openai/gpt-4o","name":"GPT-4o","provider":"openai","base_url":"https://api.openai.com/v1","api":"openai-responses","context_limit":128000,"max_tokens":16384,"image_input":true,"reasoning":false,"image_output":false},{"id":"openai/gpt-5","name":"GPT-5","provider":"openai","base_url":"https://api.openai.com/v1","api":"openai-responses","context_limit":400000,"max_tokens":128000,"image_input":true,"reasoning":true,"image_output":false},{"id":"openai/gpt-5.2","name":"GPT-5.2","provider":"openai","base_url":"https://api.openai.com/v1","api":"openai-responses","context_limit":400000,"max_tokens":128000,"image_input":true,"reasoning":true,"image_output":false},{"id":"openai/o3","name":"o3","provider":"openai","base_url":"https://api.openai.com/v1","api":"openai-responses","context_limit":200000,"max_tokens":100000,"image_input":true,"reasoning":true,"image_output":false},{"id":"openrouter/anthropic/claude-sonnet-4.6","name":"Anthropic: Claude Sonnet 4.6","provider":"openrouter","base_url":"https://openrouter.ai/api/v1","api":"openai-completions","context_limit":1000000,"max_tokens":128000,"image_input":true,"reasoning":true,"image_output":false},{"id":"xai/grok-4","name":"Grok 4","provider":"xai","base_url":"https://api.x.ai/v1","api":"openai-completions","context_limit":256000,"max_tokens":64000,"image_input":false,"reasoning":true,"image_output":false},{"id":"zai/glm-4.7","name":"GLM-4.7","provider":"zai","base_url":"https://api.z.ai/api/coding/paas/v4","api":"openai-completions","context_limit":204800,"max_tokens":131072,"image_input":false,"reasoning":true,"image_output":false},{"id":"openai/gpt-image-1","provider":"openai","name":"GPT Image 1","api":"image-generation","image_input":true,"image_output":true,"context_limit":128000}]"####;

/* Curated model metadata, provider effort mappings and browser OAuth compatibility
adapted from Pi (pinned in SPEC).
MIT License

Copyright (c) 2025 Mario Zechner

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
*/

impl Host {
    fn auth_status(&self)->Value{
        json!(self.login_providers().into_iter().map(|name|{
            let env=self.config["providers"][&name]["key_env"].as_str()
                .or(PROVIDERS.iter().find(|p|p.0==name).map(|p|p.2)).unwrap_or("");
            json!({"provider":name,"stored":self.auth[&name].is_object(),
                "environment":std::env::var(env).is_ok_and(|s|!s.trim().is_empty()),
                "method":self.auth[&name]["type"],"secret":Value::Null})
        }).collect::<Vec<_>>())
    }
    fn key_login(&mut self,provider:&str,key:&str)->Result<()>{
        self.with_state(UiState::Login,None,|host|host.key_login_inner(provider,key))
    }
    fn key_login_inner(&mut self,provider:&str,key:&str)->Result<()>{
        if key.trim().is_empty(){return Err("empty API key".into());}
        let provider=self.resolve_provider(provider)?;
        if provider=="openai-codex"{return Err("Codex requires subscription browser/device login, not an API key".into());}
        self.store_credential(&provider,Some(json!({"type":"api_key","key":key})))
    }
    fn logout(&mut self,provider:&str)->Result<()>{self.store_credential(provider,None)}
    fn select_model_after_login(&mut self,provider:&str){
        // Login must never disguise selection failures as authentication failures,
        // override a usable model, or write a global model default.
        let cancel_revision=self.cancel_revision;self.maybe_refresh_model_catalog(provider);
        if self.cancel_revision!=cancel_revision{return;}
        if self.provider_config(&self.model).is_ok(){return;}
        let models=self.models();
        let candidates:Vec<_>=models.as_array().unwrap().iter().filter(|m|m["provider"]==provider
            &&m["ready"]==true&&m["image_output"]!=true&&m["deprecated"]!=true).collect();
        let selected=candidates.iter().find(|m|m["id"]=="openai-codex/gpt-6.1-sol").or_else(||candidates.first());
        if let Some(model)=selected.and_then(|m|m["id"].as_str()){
            if let Err(error)=self.choose_model(model){self.event("notice",json!({"text":format!("Credentials stored, but model selection failed: {error}. Use /model to select a usable model.")}));}
        }else{self.event("notice",json!({"text":"Credentials stored; no usable known/configured model for this provider. Use /model to select a model."}));}
    }
    fn interactive_login(&mut self,arguments:&str)->Result<()>{
        if arguments.is_empty(){return self.interactive_login_inner(arguments);}
        self.with_state(UiState::Login,None,|host|host.interactive_login_inner(arguments))
    }
    fn interactive_login_inner(&mut self,arguments:&str)->Result<()>{
        if arguments.is_empty(){
            self.ui_text("Login: /login <provider> [browser|manual|device|api-key]\nNo Pi/Pig credentials are imported. Browser login opens the provider's sign-in page; manual uses hidden callback/code paste. Live subscription compatibility is not verified by login alone.");
            for provider in self.login_providers(){
                let methods=match provider.as_str(){
                    "openai-codex"=>"OAuth browser (default), manual redirect paste, device login",
                    "anthropic"=>"OAuth browser (default, hidden authorization-code paste), API key",
                    "github-copilot"=>"OAuth device login (headless/browser verification)",
                    _=>"API key (hidden terminal entry)"
                };
                self.ui_text(&format!("{provider}: {methods}"));
            }
            return Ok(());
        }
        let words:Vec<_>=arguments.split_whitespace().collect();
        if words.len()>2{return Err("Usage: /login <provider> [browser|manual|device|api-key]; never paste credentials into the command".into());}
        let provider=self.resolve_provider(words[0])?;
        let browser=matches!(provider.as_str(),"openai-codex"|"anthropic");
        let device=matches!(provider.as_str(),"openai-codex"|"github-copilot");
        let method=words.get(1).copied().unwrap_or(if browser{"browser"}else if device{"device"}else{"api-key"});
        match method{
            "browser"|"oauth" if browser=>self.browser_login(&provider,false)?,
            "manual"|"--manual" if browser=>self.browser_login(&provider,true)?,
            "device"|"oauth" if device=>self.oauth_login(&provider)?,
            "api-key"|"--api-key" if provider!="openai-codex"&&provider!="github-copilot"=>{
                let key=terminal_secret_service("API key (hidden): ",None,||{if self.codex_cancel("auth")?{Err("login cancelled".into())}else{Ok(())}})?;self.key_login(&provider,&key)?;
            },
            _=>return Err(format!("Unsupported login method for {provider}; /login lists available methods (never put a credential in the command)").into())
        }
        self.ui_text(&format!("Stored credentials for {provider}. Live provider compatibility is not verified by login alone."));
        self.select_model_after_login(&provider);Ok(())
    }
}

impl Host{
    fn auth_http(&mut self,request:reqwest::RequestBuilder)->Result<Value>{
        // Auth bodies/tokens are deliberately never journaled.
        self.http_json(request,"auth")
    }
    fn oauth_login(&mut self,provider:&str)->Result<()>{
        self.with_state(UiState::Login,None,|host|host.oauth_login_inner(provider))
    }
    fn oauth_login_inner(&mut self,provider:&str)->Result<()>{
        let provider=provider_alias(provider);
        if provider=="openai-codex"{return self.codex_login();}
        if provider=="anthropic"{return self.browser_login(provider,false);}
        if provider!="github-copilot"{return Err(format!("OAuth flow for {provider} not yet implemented; API-key login is available").into());}
        let base=std::env::var("PY_GITHUB_AUTH_URL").unwrap_or_else(|_|"https://github.com".into());
        let client=reqwest::Client::new();
        let device=self.auth_http(client.post(format!("{base}/login/device/code")).header("Accept","application/json")
            .json(&json!({"client_id":"Iv1.b507a08c87ecfe98","scope":"read:user"})))?;
        let code=device["device_code"].as_str().ok_or("device flow missing device code")?;
        self.event("login_prompt",json!({"provider":provider,
            "url":device["verification_uri"],"user_code":device["user_code"]}));
        if !self.json{ui_text(&format!("Open {} and enter {}",device["verification_uri"].as_str().unwrap_or(""),device["user_code"].as_str().unwrap_or("")));}
        let deadline=std::time::Instant::now()+std::time::Duration::from_secs(device["expires_in"].as_u64().unwrap_or(600));
        let mut interval=device["interval"].as_u64().unwrap_or(5);
        loop{
            if std::time::Instant::now()>=deadline{return Err("device login expired".into());}
            for _ in 0..interval*10{
                if self.codex_cancel("auth")?{return Err("login cancelled".into());}
                std::thread::sleep(std::time::Duration::from_millis(100));
            }
            let token=self.auth_http(client.post(format!("{base}/login/oauth/access_token")).header("Accept","application/json")
                .json(&json!({"client_id":"Iv1.b507a08c87ecfe98","device_code":code,
                    "grant_type":"urn:ietf:params:oauth:grant-type:device_code"})))?;
            if let Some(access)=token["access_token"].as_str(){
                self.store_credential(provider,Some(json!({"type":"oauth","refresh":access,"access":access,"expires":0})))?;
                return Ok(());
            }
            match token["error"].as_str(){
                Some("authorization_pending")=>{},
                Some("slow_down")=>interval+=5,
                _=>return Err("device login failed or authorization was denied".into())
            }
        }
    }
    fn refresh_copilot(&mut self)->Result<()>{
        let _lock=self.auth_lock()?;
        self.auth=load_json(&self.home.join("auth.json"))?;
        let refresh=self.auth["github-copilot"]["refresh"].as_str().ok_or("Copilot login required")?.to_string();
        if self.auth["github-copilot"]["expires"].as_u64().unwrap_or(0)>now_ms() as u64+60000{return Ok(());}
        let url=std::env::var("PY_COPILOT_TOKEN_URL").unwrap_or_else(|_|"https://api.github.com/copilot_internal/v2/token".into());
        let body=self.auth_http(reqwest::Client::new().get(url).header("Authorization",format!("token {refresh}"))
            .header("User-Agent","GitHubCopilotChat/0.35.0").header("Editor-Version","vscode/1.104.3"))?;
        let access=body["token"].as_str().ok_or("Copilot token exchange missing token")?;
        let expires=body["expires_at"].as_u64().ok_or("Copilot exchange missing expiry")?*1000;
        self.auth["github-copilot"]=json!({"type":"oauth","refresh":refresh,"access":access,
            "expires":expires,"base_url":body["endpoints"]["api"]});
        write_private_json(&self.home.join("auth.json"),&self.auth)
    }
}

fn image_data_url(url:&str)->Result<(&str,&str)>{
    let(mime,data)=url.strip_prefix("data:").ok_or("only inline image URLs allowed")?
        .split_once(";base64,").ok_or("invalid image data URL")?;
    if mime!="image/png"&&mime!="image/jpeg"{return Err("unsupported image MIME".into());}
    Ok((mime,data))
}
fn validate_image(data:&str)->Result<(&'static str,usize)>{
    let bytes=B64.decode(data)?;
    if bytes.len()>512000{return Err("image exceeds 512000-byte limit".into());}
    let mime=if bytes.starts_with(b"\x89PNG\r\n\x1a\n"){"image/png"}
        else if bytes.starts_with(b"\xff\xd8\xff"){"image/jpeg"}
        else{return Err("only PNG/JPEG images supported".into());};
    let format=if mime=="image/png"{image::ImageFormat::Png}else{image::ImageFormat::Jpeg};
    let reader=image::ImageReader::with_format(std::io::Cursor::new(&bytes),format);
    let(width,height)=reader.into_dimensions()?;
    if width>1536||height>1536{return Err("image dimensions exceed 1536 pixels".into());}
    image::load_from_memory_with_format(&bytes,format)?;
    Ok((mime,bytes.len()))
}

impl Host{
    fn recovery_status(&self)->Result<Value>{
        let mut operations=std::collections::BTreeMap::<String,Value>::new();
        let mut queues=std::collections::BTreeMap::<String,Value>::new();
        let mut streams=std::collections::BTreeMap::<(String,String,usize),Value>::new();
        for sequence in 0..self.journal.seq{
            let event=self.journal.event(sequence)?;
            let p=&event["payload"];
            match event["kind"].as_str().unwrap_or(""){
                "intent"=>{
                    if let Some(id)=p["operation"].as_str(){
                        operations.insert(id.into(),json!({"operation":id,"type":p["type"],
                            "state":"unknown","replay_allowed":false,"intent_event":sequence}));
                    }
                },
                "completion"|"shell_completion"|"operation_complete"|"task_settled"|"provider_cancelled"=>{
                    if let Some(id)=p["operation"].as_str().or(p["command_id"].as_str()){
                        if let Some(op)=operations.get_mut(id){
                            op["state"]=json!(if p["stdout"]["complete"]==false||p["status"]=="worker_crashed"{
                                "unknown"
                            }else if event["kind"]=="provider_cancelled"{"cancelled"}else{"completed"});
                            op["completion_event"]=json!(sequence);
                        }
                    }
                },
                "stream"=>{
                    let id=p["operation"].as_str().ok_or("stream operation missing")?;
                    let name=p["collection"].as_str().ok_or("stream collection missing")?;
                    let index=p["index"].as_u64().ok_or("stream index missing")? as usize;
                    let stream=streams.entry((id.into(),name.into(),index)).or_insert_with(||
                        json!({"operation":id,"collection":name,"index":index,"complete":false,"chunks":0}));
                    stream["chunks"]=json!(stream["chunks"].as_u64().unwrap_or(0)+1);
                },
                "queue_arrival"=>{
                    if let Some(id)=p["command_id"].as_str(){
                        queues.insert(id.into(),json!({"command_id":id,"command":p["command"],
                            "user_index":p["user_index"],"state":"pending","auto_dispatch":false}));
                    }
                },
                "queue_delivered"|"queue_dispatched"|"queue_restored"|"queue_cancelled"=>{
                    if let Some(queue)=p["command_id"].as_str().and_then(|id|queues.get_mut(id)){
                        queue["state"]=json!(event["kind"].as_str().unwrap().strip_prefix("queue_").unwrap());
                    }
                },
                "stream_complete"|"stream_closed"=>{
                    let key=(p["operation"].as_str().ok_or("stream operation missing")?.into(),
                        p["collection"].as_str().ok_or("stream collection missing")?.into(),
                        p["metadata"]["index"].as_u64().ok_or("stream index missing")? as usize);
                    if let Some(stream)=streams.get_mut(&key){
                        stream["complete"]=p["metadata"]["complete"].clone();stream["counts"]=p["metadata"].clone();
                    }
                },
                _=>{}
            }
        }
        Ok(json!({"operations":operations.into_values().collect::<Vec<_>>(),
            "streams":streams.into_values().collect::<Vec<_>>(),
            "queues":queues.into_values().collect::<Vec<_>>(),
            "policy":"Unknown outcome is never permission to replay. Fresh Python; captured chunks only."}))
    }
}

impl Host{
    fn queue_arrival(&mut self,command:Value)->Result<()>{
        if command["kind"]=="stdin_reply"{self.pending.push_back(command);return Ok(());}
        let id=command["id"].as_str().unwrap_or("").to_string();
        if id.is_empty()||self.ids.contains(&id){
            self.event("rejected",json!({"command_id":id,"error":"missing or duplicate command ID"}));
            return Ok(());
        }
        let recorded=redacted_command(command.clone())?;
        self.journal.append("accepted",json!({"command_id":id,"command":recorded}))?;
        self.ids.insert(id.clone());
        let text=match command["kind"].as_str(){
            Some("submit")=>command["text"].as_str(),
            Some("python")=>command["source"].as_str(),
            Some("shell")=>command["command"].as_str(),
            _=>None
        };
        let record=if let Some(text)=text{
            let sequence=self.journal.seq;
            Some((self.user(text,false)?,sequence))
        }else{None};
        self.journal.append("queue_arrival",json!({"command_id":id,"command":recorded,
            "user_index":record.map(|p|p.0),"state":"pending"}))?;
        self.queued.insert(id.clone(),record);
        self.pending.push_back(command);
        self.event("queued",json!({"command_id":id,"queue_depth":self.pending.len()}));
        Ok(())
    }
    fn select_user(&mut self,text:&str,index:usize,sequence:usize)->Result<()>{
        let selected=text.chars().take(8000).collect::<String>();
        let rendered=format!("{}\n[{} chars; {} lines; omitted {} chars; H.user[{}]]",
            selected,text.chars().count(),text.lines().count(),text.chars().count().saturating_sub(8000),index);
        self.add_context("user",rendered,false,vec![(sequence,sequence+1)])?;
        Ok(())
    }
}


// Terminal rendering is deliberately independent of history and JSON events.
// POSIX wcwidth gives locale-aware columns without a second UI dependency.
fn terminal_char_width(c:char)->usize{
    static LOCALE:std::sync::Once=std::sync::Once::new();
    LOCALE.call_once(||unsafe{
        libc::setlocale(libc::LC_CTYPE,c"".as_ptr());
        // Rust starts in the C locale; prefer a UTF-8 locale if the environment
        // selected C. This changes only character classification, not numbers.
        let name=libc::setlocale(libc::LC_CTYPE,std::ptr::null());
        if !name.is_null()&&[b"C".as_slice(),b"POSIX".as_slice()].contains(&std::ffi::CStr::from_ptr(name).to_bytes()){
            libc::setlocale(libc::LC_CTYPE,c"C.UTF-8".as_ptr());
        }
    });
    unsafe extern "C"{fn wcwidth(c:libc::wchar_t)->libc::c_int;}
    let width=unsafe{wcwidth(c as libc::wchar_t)};
    if width>=0{width as usize}else{1}
}
fn terminal_clusters(text:&str)->Vec<(String,usize)>{
    let mut clusters:Vec<(String,usize)>=vec![];
    let mut joined=false;let mut regional=false;let mut emoji=false;
    for c in text.chars(){
        let width=terminal_char_width(c);
        let is_regional=('\u{1f1e6}'..='\u{1f1ff}').contains(&c);
        let modifier=('\u{1f3fb}'..='\u{1f3ff}').contains(&c);
        // This is a conservative emoji-style join, not arbitrary grapheme
        // shaping: ASCII/CJK around a ZWJ still consume their own columns.
        let is_emoji=!modifier&&(('\u{1f300}'..='\u{1faff}').contains(&c)||
            ('\u{2600}'..='\u{27bf}').contains(&c));
        if let Some((text,total))=clusters.last_mut(){
            let emoji_join=joined&&emoji&&is_emoji;
            if width==0||emoji_join||(is_regional&&regional)||(modifier&&emoji){
                text.push(c);
                if emoji_join||is_regional||(modifier&&emoji){*total=(*total).max(width).max(2);}
                if (c=='\u{fe0f}'&&emoji)||(c=='\u{20e3}'&&text.starts_with(|c:char|c.is_ascii_digit()||c=='#'||c=='*')){
                    *total=(*total).max(2);emoji=true;
                }
                joined=c=='\u{200d}'&&emoji;regional=false;continue;
            }
        }
        clusters.push((c.to_string(),width));joined=false;regional=is_regional;emoji=is_emoji;
    }
    clusters
}
fn terminal_columns(text:&str)->usize{terminal_clusters(text).iter().map(|(_,w)|w).sum()}
fn terminal_width()->usize{terminal_width_for(1)}
fn terminal_width_for(fd:i32)->usize{
    let mut size:libc::winsize=unsafe{std::mem::zeroed()};
    if unsafe{libc::ioctl(fd,libc::TIOCGWINSZ,&mut size)}==0&&size.ws_col>0{return (size.ws_col as usize).min(512);}
    std::env::var("COLUMNS").ok().and_then(|v|v.parse::<usize>().ok()).filter(|v|*v>0).unwrap_or(80).min(512)
}
fn terminal_safe(text:&str)->String{
    let mut safe=String::new();
    for c in text.chars(){match c{
        '\n'=>safe.push(c),'\t'=>safe.push_str("    "),
        c if c.is_control()||('\u{202a}'..='\u{202e}').contains(&c)||('\u{2066}'..='\u{2069}').contains(&c)=>{
            if (c as u32)<256{safe.push_str(&format!("\\x{:02x}",c as u32));}
            else{safe.push_str(&format!("\\u{{{:x}}}",c as u32));}
        },
        _=>safe.push(c)
    }}safe
}
// Return physical lines and the number of forced long-word breaks. The latter
// is also a cost in table layout: a broken word is not a free height reduction.
fn terminal_wrap(text:&str,width:usize)->(Vec<String>,usize){
    let width=width.max(1);let safe=terminal_safe(text);let mut lines=vec![];let mut breaks=0;
    for logical in safe.split('\n'){
        let mut line=String::new();let mut columns=0;let mut whitespace=String::new();
        for piece in logical.split_inclusive(char::is_whitespace){
            let word=piece.trim_end_matches(char::is_whitespace);
            let spaces=&piece[word.len()..];
            if word.is_empty(){whitespace.push_str(spaces);continue;}
            let word_width=terminal_columns(word);let ws=terminal_columns(&whitespace);
            if columns>0&&columns+ws+word_width>width{
                lines.push(line.trim_end().to_string());line.clear();columns=0;whitespace.clear();
            }
            // Preserve indentation and intra-word spaces where they fit. Excess
            // indentation cannot consume the entire visible content width.
            if !whitespace.is_empty(){
                let available=width.saturating_sub(columns+word_width.min(width));
                let count=ws.min(available);line.push_str(&" ".repeat(count));columns+=count;whitespace.clear();
            }
            let clusters=terminal_clusters(word);
            for (i,(cluster,size)) in clusters.iter().enumerate(){
                if columns+size>width&&!line.is_empty(){
                    lines.push(std::mem::take(&mut line));columns=0;if i>0{breaks+=1;}
                }
                if *size>width{line.push('?');columns+=1;}else{line.push_str(cluster);columns+=size;}
            }
            whitespace.push_str(spaces);
        }
        lines.push(line.trim_end().to_string());
    }
    (lines,breaks)
}
fn terminal_prefixed(text:&str,prefix:&str,width:usize)->Vec<String>{
    let prefix=terminal_safe(prefix);let indent=terminal_columns(&prefix);
    if indent>=width{return terminal_wrap(&format!("{prefix}{text}"),width).0;}
    terminal_wrap(text,width-indent).0.into_iter().enumerate().map(|(i,line)|
        format!("{}{line}",if i==0{prefix.clone()}else{" ".repeat(indent)})).collect()
}
fn terminal_inline(text:&str)->String{
    let mut result=String::new();let mut rest=text;
    while !rest.is_empty(){
        if let Some(after)=rest.strip_prefix('\\'){
            if let Some(c)=after.chars().next(){if "\\`*_{}[]()#+-.!|>~".contains(c){result.push(c);rest=&after[c.len_utf8()..];continue;}}
        }
        let mut consumed=false;
        for marker in ["**","__","~~","`","*","_"]{
            if let Some(after)=rest.strip_prefix(marker){
                if let Some(end)=after.find(marker){result.push_str(&after[..end]);rest=&after[end+marker.len()..];consumed=true;break;}
            }
        }
        if consumed{continue;}
        if let Some(after)=rest.strip_prefix('['){
            if let Some(close)=after.find("]("){
                if let Some(end)=after[close+2..].find(')'){
                    result.push_str(&after[..close]);result.push_str(" (");
                    result.push_str(&after[close+2..close+2+end]);result.push(')');
                    rest=&after[close+3+end..];continue;
                }
            }
        }
        let c=rest.chars().next().unwrap();result.push(c);rest=&rest[c.len_utf8()..];
    }result
}
fn terminal_table_cells(line:&str)->Vec<String>{
    let mut cells=vec![];let mut cell=String::new();let mut escaped=false;let mut code=false;
    for c in line.trim().chars(){
        if escaped{if c!='|'&&c!='\\'{cell.push('\\');}cell.push(c);escaped=false;continue;}
        match c{'\\'=>escaped=true,'`'=>{code= !code;cell.push(c);},'|' if !code=>{cells.push(std::mem::take(&mut cell));},_=>cell.push(c)}
    }
    if escaped{cell.push('\\');}cells.push(cell);
    if line.trim_start().starts_with('|'){cells.remove(0);}
    if line.trim_end().ends_with('|')&&cells.last().is_some_and(String::is_empty){cells.pop();}
    cells.into_iter().map(|c|terminal_inline(c.trim())).collect()
}
fn terminal_table_separator(line:&str,columns:usize)->Option<Vec<i8>>{
    let cells=terminal_table_cells(line);if cells.len()!=columns{return None;}
    cells.iter().map(|cell|{
        let trimmed=cell.trim_matches(':');
        if trimmed.len()<3||!trimmed.chars().all(|c|c=='-'){return None;}
        Some(if cell.starts_with(':')&&cell.ends_with(':'){0}else if cell.ends_with(':'){1}else{-1})
    }).collect()
}
// Word costs use prefix sums, not rendered strings for every possible width.
// A uniform-width word (the common ASCII/CJK case) is constant-time; mixed
// widths find each physical line by binary search. Zero-width marks are kept.
struct TerminalWordCost{spaces:usize,prefix:Vec<usize>,uniform:Option<usize>}
fn terminal_cell_costs(cell:&str,available:usize)->Vec<(usize,usize)>{
    let safe=terminal_safe(cell);
    let logical:Vec<Vec<TerminalWordCost>>=safe.split('\n').map(|line|{
        let mut words=vec![];let mut spaces=0;
        for piece in line.split_inclusive(char::is_whitespace){
            let word=piece.trim_end_matches(char::is_whitespace);
            if word.is_empty(){spaces+=terminal_columns(piece);continue;}
            let widths:Vec<_>=terminal_clusters(word).into_iter().map(|(_,w)|w).collect();
            let mut prefix=vec![0];for width in &widths{prefix.push(prefix.last().unwrap()+width);}
            let uniform=widths.first().copied().filter(|w|*w>0&&widths.iter().all(|x|x==w));
            words.push(TerminalWordCost{spaces,prefix,uniform});
            spaces=terminal_columns(&piece[word.len()..]);
        }
        words
    }).collect();
    let mut costs=vec![(usize::MAX,0)];
    for width in 1..=available{
        let mut height=logical.len();let mut breaks=0;
        for words in &logical{
            let mut columns=0;let mut nonempty=false;
            for word in words{
                let total=*word.prefix.last().unwrap();let count=word.prefix.len()-1;
                let mut spaces=word.spaces;
                if columns>0&&columns+spaces+total>width{height+=1;columns=0;nonempty=false;spaces=0;}
                columns+=spaces.min(width.saturating_sub(columns+total.min(width)));
                nonempty|=columns>0;
                if columns+total<=width{columns+=total;nonempty|=count>0;continue;}
                if let Some(unit)=word.uniform.filter(|unit|*unit<=width){
                    let fit=((width-columns)/unit).min(count);columns+=fit*unit;
                    let remaining=count-fit;
                    if remaining>0{
                        if nonempty||fit>0{height+=1;if fit>0{breaks+=1;}}
                        let per_line=width/unit;let extra=(remaining-1)/per_line;
                        height+=extra;breaks+=extra;columns=((remaining-1)%per_line+1)*unit;
                    }
                }else{
                    let mut at=0;
                    while at<count{
                        let end=word.prefix.partition_point(|sum|*sum<=word.prefix[at]+width-columns)
                            .saturating_sub(1).min(count);
                        if end>at{columns+=word.prefix[end]-word.prefix[at];at=end;nonempty=true;}
                        else if !nonempty{
                            // Matches the wrapper's unavoidable replacement at
                            // width one; normal table minima prevent this path.
                            columns+=1;at+=1;nonempty=true;
                        }
                        if at<count{height+=1;if at>0{breaks+=1;}columns=0;nonempty=false;}
                    }
                }
                nonempty=true;
            }
        }
        costs.push((height,breaks));
    }
    costs
}
fn terminal_table_widths(rows:&[Vec<String>],available:usize)->Vec<usize>{
    let columns=rows[0].len();
    let natural:Vec<_>=(0..columns).map(|c|rows.iter().map(|r|terminal_columns(&r[c])).max().unwrap_or(1).max(1)).collect();
    // A CJK/emoji cluster cannot occupy a one-column cell. Do not let a cheap
    // replacement glyph distort the optimizer when the real glyph can fit.
    let minimum:Vec<_>=(0..columns).map(|c|rows.iter().flat_map(|r|terminal_clusters(&r[c]))
        .map(|(_,w)|w).max().unwrap_or(1).max(1)).collect();
    if natural.iter().sum::<usize>()<=available{return natural;}
    // Sample at most 128 rows/512 cells, 512 chars/cell and 16,384 total
    // characters, evenly across the table. Cache equal cells and pre-tokenize
    // widths once, so preprocessing is not repeated wrapping
    // and allocation of 131 million characters at a wide terminal. Rendering
    // below still uses every character of every row, not the optimization sample.
    let mut unique=HashMap::new();let mut costs=vec![];let mut sample=vec![];
    let chars_per_cell=512.min((16_384/columns).max(1));
    let row_limit=rows.len().min(128).min((512/columns).max(1))
        .min((16_384/(columns*chars_per_cell)).max(1));
    for at in 0..row_limit{
        let index=if row_limit==1{0}else{at*(rows.len()-1)/(row_limit-1)};
        let mut indices=vec![];
        for cell in &rows[index]{
            let cell:String=cell.chars().take(chars_per_cell).collect();
            let index=*unique.entry(cell.clone()).or_insert_with(||{
                let index=costs.len();costs.push(terminal_cell_costs(&cell,available));index
            });
            indices.push(index);
        }
        sample.push(indices);
    }
    // Bound objective search separately: no more than two million sampled-cell
    // probes, in addition to the character-bounded prefix-sum cost construction.
    let evaluations=std::cell::Cell::new(0usize);
    let max_evaluations=(2_000_000/(sample.len()*columns).max(1)).max(1);
    let score=|widths:&[usize]|->usize{
        if evaluations.get()>=max_evaluations{return usize::MAX;}
        evaluations.set(evaluations.get()+1);
        sample.iter().map(|row|{
            let mut height=0;let mut breaks=0;
            for (&cell,&width) in row.iter().zip(widths){let cost=costs[cell][width];height=height.max(cost.0);breaks+=cost.1;}
            height+breaks
        }).sum()
    };
    let mut best=minimum.clone();
    for _ in minimum.iter().sum::<usize>()..available{
        let candidate=if evaluations.get()+columns<max_evaluations{
            (0..columns).filter(|&c|best[c]<natural[c]).min_by_key(|&c|{
                let mut widths=best.clone();widths[c]+=1;(score(&widths),std::cmp::Reverse(natural[c]-best[c]),c)
            })
        }else{
            // Even with an exhausted objective budget, fill available space
            // deterministically rather than rendering an unnecessarily tall table.
            (0..columns).filter(|&c|best[c]<natural[c]).max_by_key(|&c|(natural[c]-best[c],std::cmp::Reverse(c)))
        };
        if let Some(c)=candidate{best[c]+=1;}else{break;}
    }
    let mut best_score=score(&best);
    // Pair moves can cross one-column plateaus which ordinary width greed cannot.
    for _ in 0..available.min(128){
        let mut improvement=None;
        for from in 0..columns{for to in 0..columns{
            if evaluations.get()>=max_evaluations||from==to||best[from]<=minimum[from]||best[to]>=natural[to]{continue;}
            let mut widths=best.clone();widths[from]-=1;widths[to]+=1;let value=score(&widths);
            if value<best_score&&improvement.as_ref().is_none_or(|(v,_)|value<*v){improvement=Some((value,widths));}
        }}
        if let Some((value,widths))=improvement{best_score=value;best=widths;}else{break;}
    }
    // Enumerate bounded compositions for <=4 columns. Ties prefer less skewed
    // widths, then lexical order, making every redraw deterministic.
    if columns<=4{
        // Keep bounded recursion state explicit and local, not a layout framework.
        #[allow(clippy::too_many_arguments)]
        fn search<F:Fn(&[usize])->usize>(at:usize,budget:usize,natural:&[usize],minimum:&[usize],current:&mut Vec<usize>,
            best:&mut Vec<usize>,best_score:&mut usize,states:&mut usize,score:&F,
            evaluations:&std::cell::Cell<usize>,max_evaluations:usize){
            if *states>=50_000||evaluations.get()>=max_evaluations{return;}
            if at+1==natural.len(){
                current.push(budget.min(natural[at]).max(minimum[at]));*states+=1;let value=score(current);
                let spread=|w:&[usize]|w.iter().max().unwrap()-w.iter().min().unwrap();
                if value<*best_score||(value==*best_score&&(spread(current),&*current)<(spread(best),&*best)){
                    *best_score=value;*best=current.clone();
                }current.pop();return;
            }
            let remaining:usize=minimum[at+1..].iter().sum();
            for width in minimum[at]..=natural[at].min(budget.saturating_sub(remaining)){
                current.push(width);search(at+1,budget-width,natural,minimum,current,best,best_score,states,score,evaluations,max_evaluations);current.pop();
                if *states>=50_000||evaluations.get()>=max_evaluations{break;}
            }
        }
        search(0,available,&natural,&minimum,&mut vec![],&mut best,&mut best_score,&mut 0,&score,&evaluations,max_evaluations);
    }best
}
fn terminal_table(rows:&[Vec<String>],align:&[i8],width:usize)->Vec<String>{
    let columns=rows[0].len();let overhead=3*columns+1;
    if width<overhead+4*columns{
        let mut lines=vec![];
        for row in rows.iter().skip(1){
            if !lines.is_empty(){lines.push(String::new());}
            for (header,cell) in rows[0].iter().zip(row){lines.extend(terminal_prefixed(cell,&format!("{header}: "),width));}
        }
        if rows.len()==1{for cell in &rows[0]{lines.extend(terminal_wrap(cell,width).0);}}return lines;
    }
    let widths=terminal_table_widths(rows,width-overhead);
    let border=|left:&str,middle:&str,right:&str|format!("{left}{}{right}",
        widths.iter().map(|w|"─".repeat(w+2)).collect::<Vec<_>>().join(middle));
    let mut lines=vec![border("┌","┬","┐")];
    for (index,row) in rows.iter().enumerate(){
        let wrapped:Vec<_>=row.iter().zip(&widths).map(|(c,&w)|terminal_wrap(c,w).0).collect();
        for physical in 0..wrapped.iter().map(Vec::len).max().unwrap_or(0){
            let mut cells=vec![];
            for (c,&w) in widths.iter().enumerate(){
                let text=wrapped[c].get(physical).map(String::as_str).unwrap_or("");let padding=w.saturating_sub(terminal_columns(text));
                let left=if align[c]>0{padding}else if align[c]==0{padding/2}else{0};
                cells.push(format!(" {}{text}{} "," ".repeat(left)," ".repeat(padding-left)));
            }
            lines.push(format!("│{}│",cells.join("│")));
        }
        if index+1<rows.len(){lines.push(border("├","┼","┤"));}
    }
    lines.push(border("└","┴","┘"));lines
}
fn terminal_markdown(text:&str,width:usize)->Vec<String>{
    let safe=terminal_safe(text);let logical:Vec<_>=safe.lines().collect();let mut lines=vec![];let mut at=0;let mut fence=false;
    while at<logical.len(){
        let line=logical[at];let trim=line.trim();
        if trim.starts_with("```")||trim.starts_with("~~~"){fence= !fence;at+=1;continue;}
        if fence{
            lines.extend(terminal_style_lines(terminal_prefixed(line,"  ",width),"36"));at+=1;continue;
        }
        if trim.is_empty(){lines.push(String::new());at+=1;continue;}
        if at+1<logical.len()&&line.contains('|'){
            let header=terminal_table_cells(line);
            if !header.is_empty(){if let Some(align)=terminal_table_separator(logical[at+1],header.len()){
                let columns=header.len();let mut rows=vec![header];at+=2;
                while at<logical.len()&&logical[at].contains('|')&&!logical[at].trim().is_empty(){
                    let mut row=terminal_table_cells(logical[at]);row.resize(columns,String::new());row.truncate(columns);rows.push(row);at+=1;
                }
                lines.extend(terminal_table_styled(terminal_table(&rows,&align,width)));continue;
            }}
        }
        let heading=trim.chars().take_while(|c|*c=='#').count();
        if (1..=6).contains(&heading)&&trim[heading..].starts_with(' '){
            let title=terminal_inline(trim[heading..].trim().trim_end_matches('#').trim());
            lines.extend(terminal_style_lines(terminal_wrap(&title,width).0,"1;36"));
            lines.push(terminal_styled(&if heading==1{"═"}else{"─"}.repeat(terminal_columns(&title).min(width)),"2"));at+=1;continue;
        }
        if trim.len()>=3&&(trim.chars().all(|c|c=='-')||trim.chars().all(|c|c=='*')||trim.chars().all(|c|c=='_')){
            lines.push(terminal_styled(&"─".repeat(width),"2"));at+=1;continue;
        }
        if let Some(quote)=trim.strip_prefix("> "){
            let quote=terminal_inline(quote);let available=width.saturating_sub(2).max(1);
            if width<=2{lines.extend(terminal_wrap(&quote,width).0);}else{
                lines.extend(terminal_wrap(&quote,available).0.into_iter().map(|l|terminal_styled(&format!("│ {l}"),"2;3")));
            }at+=1;continue;
        }
        let bullet=trim.strip_prefix("- ").or_else(||trim.strip_prefix("* ")).or_else(||trim.strip_prefix("+ "));
        if let Some(item)=bullet{lines.extend(terminal_prefixed(&terminal_inline(item),"• ",width));at+=1;continue;}
        let digits=trim.chars().take_while(char::is_ascii_digit).count();
        if digits>0&&trim[digits..].starts_with(". "){
            lines.extend(terminal_prefixed(&terminal_inline(&trim[digits+2..]),&trim[..digits+2],width));at+=1;continue;
        }
        // Soft paragraph newlines are Markdown spaces; explicit blank lines are
        // retained. Block starts are kept separate instead of swallowed.
        let mut paragraph=trim.to_string();at+=1;
        while at<logical.len(){let next=logical[at].trim();
            if next.is_empty()||next.starts_with(['#','>','-','*','+','`','~'])||next.contains('|'){break;}
            paragraph.push(' ');paragraph.push_str(next);at+=1;
        }
        lines.extend(terminal_wrap(&terminal_inline(&paragraph),width).0);
    }lines
}
fn terminal_preview(v:&Value,width:usize)->Vec<String>{
    let mut lines=vec![String::new()];
    let id=v["command_id"].as_str().unwrap_or("?");
    if v["cell"].as_u64().is_none(){
        lines.extend(terminal_style_lines(terminal_wrap(&format!("── cell {id} {}",if v["code"]["ref"].as_str().unwrap_or("").starts_with("H.code"){"python"}else{"shell"}),width).0,"1;36"));
    }
    for stream in ["code","stdout","stderr"]{
        if !v[stream].is_object(){continue;}
        lines.extend(terminal_style_lines(terminal_wrap(&format!("── {stream}"),width).0,"36"));
        if let Some(preview)=v[stream]["preview"].as_str(){if !preview.is_empty(){lines.extend(terminal_wrap(preview.trim_end_matches('\n'),width).0);}}
        let info=format!("{stream}: {} lines, {} chars; {}",v[stream]["lines"],v[stream]["chars"],v[stream]["ref"].as_str().unwrap_or("?"));
        lines.extend(terminal_style_lines(terminal_wrap(&info,width).0,"2"));
    }
    if let Some(status)=v["status"].as_str(){lines.extend(terminal_style_lines(terminal_wrap(&format!("status: {status}"),width).0,terminal_result_style(status)));}
    lines.push(terminal_styled(&"─".repeat(width),"2"));lines.push(String::new());lines
}

#[cfg(test)]
mod rendering_e2e {
    use std::{io::Write,process::Stdio};
    use serde_json::{json,Value};
    fn run(source:&str,width:usize,json_output:bool)->(String,Vec<Value>){
        let home=std::env::temp_dir().join(format!("py-render-{}-{}",std::process::id(),
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()));
        let mut cmd=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap());
        cmd.args(["--json-input","--no-model"]).env("PY_HOME",&home).env("COLUMNS",width.to_string())
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
        if json_output{cmd.arg("--json");}
        let mut child=cmd.spawn().unwrap();
        writeln!(child.stdin.take().unwrap(),"{}",json!({"id":"render-cell","kind":"python","source":source})).unwrap();
        let out=child.wait_with_output().unwrap();
        assert!(out.status.success(),"{}",String::from_utf8_lossy(&out.stderr));
        let session=std::fs::read_dir(home.join("sessions")).unwrap().next().unwrap().unwrap().path();
        let events=std::fs::read_to_string(session).unwrap().lines().map(|l|serde_json::from_str(l).unwrap()).collect();
        (String::from_utf8(out.stdout).unwrap(),events)
    }
    #[test]
    fn e2e_say_markdown_wraps_by_word_and_keeps_original(){
        let text="# Heading\n\nalpha beta gamma delta epsilon zeta eta theta\n\n- **bold** text with `code`\n\n> quoted words inside a block quote\n\n```python\nprint('literal')\n```";
        let (output,journal)=run(&format!("agent.say({})",serde_json::to_string(text).unwrap()),24,false);
        let rendered=output.split("── cell").next().unwrap();
        assert!(!rendered.contains("**bold**"),"Markdown markers must be rendered in say (not rewritten source): {output}");
        assert!(output.contains("• bold text with code"));assert!(output.contains("│ quoted words inside"));
        assert!(output.lines().any(|l|l=="alpha beta gamma delta"),"word wrapping: {output}");
        let say=journal.iter().find(|v|v["kind"]=="say").unwrap();assert_eq!(say["payload"]["text"],text);
        // JSON events are a separate machine surface, never terminal-rendered text.
        let (json_output,_)=run(&format!("agent.say({})",serde_json::to_string(text).unwrap()),24,true);
        assert_eq!(json_output.lines().map(|l|serde_json::from_str::<Value>(l).unwrap())
            .find(|v|v["kind"]=="say").unwrap()["text"],text);
    }
    #[test]
    fn e2e_say_table_optimizes_height_and_wraps_columns(){
        let text="| A | B |\n| --- | --- |\n| a | alpha beta gamma delta epsilon zeta |";
        let (output,_)=run(&format!("agent.say({})",serde_json::to_string(text).unwrap()),32,false);
        let rows:Vec<_>=output.lines().filter(|l|l.starts_with('│')).collect();
        assert!(!rows.is_empty(),"must render a bordered table: {output}");
        assert_eq!(rows.len(),3,"header plus minimal two body lines, not equal-width tall table: {output}");
        assert!(rows.iter().any(|l|l.contains("alpha beta gamma delta")),"wide content column: {output}");
        assert!(!output.split("── cell").next().unwrap().contains("| --- |"));
        for l in rows{assert!(l.chars().count()<=32,"too wide: {l}");}
    }
    #[test]
    fn e2e_say_table_long_word_breaks_are_not_free(){
        let text="| A | B |\n| --- | --- |\n| aaabbbcccddd | alpha beta gamma |\n| alpha beta | x |";
        let (output,_)=run(&format!("agent.say({})",serde_json::to_string(text).unwrap()),25,false);
        let top=output.lines().find(|l|l.starts_with('┌')).unwrap();
        let widths:Vec<_>=top.trim_start_matches('┌').trim_end_matches('┐').split('┬').map(|s|s.chars().count()-2).collect();
        // Exact small-table optimum is [12,6]: five content lines, no broken
        // word. [6,12] also has five physical lines, but breaks the long word.
        assert_eq!(widths,vec![12,6],"penalize forced word breaks: {output}");
        assert!(output.lines().any(|l|l.starts_with('│')&&l.contains("aaabbbcccddd")));
    }
    #[test]
    fn e2e_say_narrow_table_unicode_and_controls(){
        let text="| Name | Words |\n| --- | --- |\n| 界 é | alpha beta gamma |\n\n\u{1b}]52;c;DANGER\u{7}\nlongunbrokenwordabcdefghijk";
        let (output,journal)=run(&format!("agent.say({})",serde_json::to_string(text).unwrap()),12,false);
        assert!(!output.contains('\u{1b}')&&!output.contains('\u{7}'),"terminal control injection: {output:?}");
        assert!(output.contains("界"));assert!(output.contains("é"));
        assert!(output.lines().any(|l|l.contains("Name:")),"stack cells when borders cannot fit: {output}");
        assert_eq!(journal.iter().find(|v|v["kind"]=="say").unwrap()["payload"]["text"],text);
        assert!(output.contains("\\x1b"),"escaped control remains discoverable: {output}");
    }
    #[test]
    fn e2e_markdown_real_pty_width_and_unicode(){
        let home=std::env::temp_dir().join(format!("py-render-pty-{}-{}",std::process::id(),
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()));
        std::fs::create_dir_all(&home).unwrap();
        let script=r#"
import os,pty,fcntl,termios,struct,subprocess,select,json,time,sys,unicodedata
master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,32,0,0))
text='# Unicode\n\n| 名 | Description |\n| --- | --- |\n| 界 é | alpha beta gamma delta epsilon zeta |\n\ncontrol \x1b]52;c;NO\x07'
p=subprocess.Popen([sys.argv[1],'--json-input','--no-model'],stdin=subprocess.PIPE,stdout=slave,stderr=subprocess.PIPE,
 env=dict(os.environ,PY_HOME=sys.argv[2],COLUMNS='99',NO_COLOR='1'))
os.close(slave);transcript=bytearray()
try:
 p.stdin.write((json.dumps(dict(id='pty-render',kind='python',source='agent.say('+repr(text)+')'))+'\n').encode());p.stdin.close()
 deadline=time.monotonic()+6
 while True:
  assert time.monotonic()<deadline,bytes(transcript)
  if select.select([master],[],[],.05)[0]:
   try:
    data=os.read(master,65536)
    if not data:break
    transcript.extend(data)
   except OSError:break
  elif p.poll() is not None:break
 p.wait(timeout=2);assert p.returncode==0,p.stderr.read()
 output=transcript.decode().replace('\r\n','\n')
 assert '\x1b' not in output and '\x07' not in output,repr(output)
 assert '界' in output and 'é' in output,output
 assert '┌' in output and '└' in output,output
 for line in output.splitlines():
  columns=sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W','F') else 1 for c in line)
  assert columns<=32,(columns,line,output)
 print(output)
finally:
 if p.poll() is None:p.kill();p.wait()
 os.close(master)
"#;
        let out=crate::test_command("python3").args(["-c",script,&std::env::var("PY_HARNESS_BIN").unwrap(),home.to_str().unwrap()]).output().unwrap();
        assert!(out.status.success(),"PTY width/render failure:\n{}\n{}",String::from_utf8_lossy(&out.stderr),String::from_utf8_lossy(&out.stdout));
    }
    #[test]
    fn e2e_preview_controls_are_escaped_but_history_is_exact(){
        let text="\u{1b}[2JDANGER\u{7}";
        let (output,journal)=run(&format!("print({})",serde_json::to_string(text).unwrap()),30,false);
        assert!(!output.contains('\u{1b}')&&!output.contains('\u{7}'));
        assert!(output.contains("\\x1b[2JDANGER\\x07"));
        assert_eq!(journal.iter().filter(|v|v["kind"]=="stream"&&v["payload"]["collection"]=="stdout")
            .map(|v|v["payload"]["text"].as_str().unwrap()).collect::<String>(),format!("{text}\n"));
    }
    #[test]
    fn e2e_cell_preview_boundaries_wrap_without_changing_history(){
        let source="print('alpha beta gamma delta epsilon zeta')\nimport sys\nprint('stderr alpha beta gamma delta',file=sys.stderr)";
        let (output,journal)=run(source,30,false);
        assert!(output.contains("── cell 1 · python")&&output.contains("operation render-cell"),"numbered operation boundary: {output}");
        assert!(!output.contains("── cell render-cell"),"no duplicate legacy operation header: {output}");
        assert!(output.contains("── stdout")&&output.contains("── stderr"));
        assert!(output.contains("alpha beta gamma delta"));
        assert!(output.contains("status: ok"));
        assert!(output.contains("H.stdout[0]")&&output.contains("H.stderr[0]"));
        assert_eq!(journal.iter().filter(|v|v["kind"]=="stream"&&v["payload"]["collection"]=="stdout")
            .map(|v|v["payload"]["text"].as_str().unwrap()).collect::<String>(),"alpha beta gamma delta epsilon zeta\n");
    }
}

/* Codex device/protocol reference: Pig, revision pinned in SPEC.
MIT License

Copyright Hewlett Packard Enterprise Development LP

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
*/
// Codex device/protocol constants follow the Pig pin in SPEC; own client identity.
// Browser OAuth uses the Pi pin; WebSocket transport and implicit retries are not provided.
const CODEX_CLIENT_ID:&str="app_EMoamEEZ73f0CkXaXp7hrann";
fn provider_alias(provider:&str)->&str{if provider=="codex"{"openai-codex"}else{provider}}
fn codex_token_value(body:&Value)->Result<Value>{
    let access=oauth_token(body,"access_token")?;
    let refresh=oauth_token(body,"refresh_token")?;
    let seconds=body["expires_in"].as_f64().filter(|s|s.is_finite()&&*s>0.0&&*s<=31536000.0).ok_or("Codex token response has invalid expiry")?;
    let pieces:Vec<_>=access.split('.').collect();
    if pieces.len()!=3{return Err("Codex access token has invalid account metadata".into());}
    let bytes=base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(pieces[1].trim_end_matches('='))
        .map_err(|_|"Codex access token has invalid account metadata")?;
    let claims:Value=serde_json::from_slice(&bytes).map_err(|_|"Codex access token has invalid account metadata")?;
    // Decode for account routing only, not JWT signature verification.
    let account=claims["https://api.openai.com/auth"]["chatgpt_account_id"].as_str()
        .filter(|s|!s.is_empty()&&s.len()<=512&&s.bytes().all(|c|(33..=126).contains(&c)))
        .ok_or("Codex access token lacks valid account ID")?;
    Ok(json!({"type":"oauth","access":access,"refresh":refresh,
        "expires":now_ms() as u64+(seconds*1000.0) as u64,"accountId":account}))
}
impl Host{
    fn auth_lock(&mut self)->Result<File>{self.auth_lock_until(None)}
    fn auth_checkpoint(&mut self,deadline:Option<std::time::Instant>)->Result<()>{
        if self.codex_cancel("auth")?{return Err("authentication cancelled; credentials unchanged".into());}
        if deadline.is_some_and(|until|std::time::Instant::now()>=until){
            return Err("browser login timed out; credentials unchanged".into());
        }
        Ok(())
    }
    fn auth_lock_until(&mut self,deadline:Option<std::time::Instant>)->Result<File>{
        let file=OpenOptions::new().create(true).read(true).write(true).mode(0o600)
            .custom_flags(libc::O_NOFOLLOW|libc::O_CLOEXEC).open(self.home.join("auth.lock"))?;
        loop{
            if deadline.is_some(){self.auth_checkpoint(deadline)?;}
            if unsafe{libc::flock(file.as_raw_fd(),libc::LOCK_EX|libc::LOCK_NB)}==0{
                if deadline.is_some(){self.auth_checkpoint(deadline)?;}
                return Ok(file);
            }
            let error=io::Error::last_os_error();
            if error.kind()!=io::ErrorKind::WouldBlock&&error.kind()!=io::ErrorKind::Interrupted{return Err(error.into());}
            self.auth_checkpoint(deadline)?;
            std::thread::sleep(std::time::Duration::from_millis(15));
        }
    }
    fn reload_auth(&mut self)->Result<()>{
        let _lock=self.auth_lock()?;
        self.auth=load_json(&self.home.join("auth.json"))?;Ok(())
    }
    fn store_credential(&mut self,provider:&str,credential:Option<Value>)->Result<()>{
        self.store_credential_until(provider,credential,None)
    }
    fn store_credential_until(&mut self,provider:&str,credential:Option<Value>,deadline:Option<std::time::Instant>)->Result<()>{
        let _lock=self.auth_lock_until(deadline)?;
        let path=self.home.join("auth.json");let mut auth=load_json(&path)?;
        let map=auth.as_object_mut().ok_or("invalid auth config")?;
        let provider=provider_alias(provider);
        if let Some(value)=credential{map.insert(provider.into(),value);}else{map.remove(provider);}
        if deadline.is_some(){self.auth_checkpoint(deadline)?;}
        // Successful atomic write is the commit boundary. Cancellation arriving
        // after this checkpoint cannot promise rollback of an already-saved login.
        write_private_json(&path,&auth)?;self.auth=auth;Ok(())
    }
    fn codex_cancel(&mut self,operation:&str)->Result<bool>{
        self.service_background()?;
        let mut cancel=INTERRUPT.swap(false,std::sync::atomic::Ordering::SeqCst);
        loop{
            let command=match self.incoming.as_ref().map(|r|r.try_recv()){
                Some(Ok(v))=>v,
                Some(Err(std::sync::mpsc::TryRecvError::Disconnected))=>{self.input_closed=true;break;},
                _=>break
            };
            if command["kind"]=="interrupt"{
                if self.accept_input_control(&command)?{
                    self.event("completed",json!({"command_id":command["id"],"status":"ok"}));cancel=true;
                }
            }else if !self.background_control(&command)?{self.queue_arrival(command)?;}
        }
        if cancel{
            self.journal.append("provider_cancelled",json!({"operation":operation,"billing":"unknown"}))?;
            self.cancel_revision=self.cancel_revision.wrapping_add(1);
        }
        Ok(cancel)
    }
    fn codex_wait<T>(&mut self,future:impl std::future::Future<Output=Result<T>>,operation:&str)->Result<T>{
        let rt=tokio::runtime::Builder::new_current_thread().enable_all().build()?;
        rt.block_on(async{
            tokio::pin!(future);
            loop{
                if self.codex_cancel(operation)?{return Err("Codex operation cancelled; request outcome may be unknown".into());}
                tokio::select!{
                    response=&mut future=>return response,
                    _=tokio::time::sleep(std::time::Duration::from_millis(15))=>{}
                }
            }
        })
    }
    fn codex_http(&mut self,request:reqwest::RequestBuilder)->Result<(u16,Value)>{
        self.codex_wait(async{
            let mut response=request.send().await.map_err(|_|"Codex authentication network error")?;
            let status=response.status().as_u16();let mut bytes=Vec::new();
            while let Some(chunk)=response.chunk().await.map_err(|_|"Codex authentication network error")?{
                if bytes.len()+chunk.len()>1048576{return Err("Codex authentication response exceeds limit".into());}
                bytes.extend_from_slice(&chunk);
            }
            let body=match serde_json::from_slice(&bytes){
                Ok(body)=>body,
                // Pig treats device polling 403/404 as pending even with an
                // empty/non-JSON body. Other callers still reject these statuses.
                Err(_)if status==403||status==404=>Value::Null,
                Err(_)=>return Err("Codex authentication returned invalid JSON".into())
            };
            Ok((status,body))
        },"auth")
    }
    fn codex_auth_base()->String{std::env::var("PY_CODEX_AUTH_BASE_URL").unwrap_or_else(|_|"https://auth.openai.com".into()).trim_end_matches('/').into()}
    fn codex_login(&mut self)->Result<()>{
        let base=Self::codex_auth_base();
        let client=reqwest::Client::builder().timeout(std::time::Duration::from_secs(30)).redirect(reqwest::redirect::Policy::none()).build()?;
        let (status,device)=self.codex_http(client.post(format!("{base}/api/accounts/deviceauth/usercode")).json(&json!({"client_id":CODEX_CLIENT_ID})))?;
        if status!=200{return Err(format!("Codex device login unavailable (HTTP {status}); enable device-code login in ChatGPT settings if required").into());}
        let device_id=device["device_auth_id"].as_str().filter(|s|!s.is_empty()).ok_or("Codex device response missing device ID")?;
        let code=device["user_code"].as_str().filter(|s|!s.is_empty()&&s.len()<=128&&!s.chars().any(char::is_control)).ok_or("Codex device response missing user code")?;
        let mut interval=device["interval"].as_f64().or_else(||device["interval"].as_str()?.trim().parse().ok())
            .filter(|n|n.is_finite()&&*n>=0.0&&*n<=60.0).ok_or("Codex device response has invalid interval")?.max(0.01);
        self.event("login_prompt",json!({"provider":"openai-codex","method":"device_code","url":"https://auth.openai.com/codex/device","user_code":code}));
        if !self.json{ui_text(&format!("Open https://auth.openai.com/codex/device and enter {code}. Ctrl-C cancels."));}
        let seconds=std::env::var("PY_CODEX_DEVICE_TIMEOUT_SECONDS").ok().map(|s|s.parse::<f64>()).transpose()?
            .unwrap_or(900.0);
        if !seconds.is_finite()||seconds<=0.0||seconds>900.0{return Err("invalid Codex device timeout (0–900 seconds)".into());}
        let deadline=std::time::Instant::now()+std::time::Duration::from_secs_f64(seconds);
        loop{
            let left=deadline.saturating_duration_since(std::time::Instant::now());
            if left.is_zero(){return Err("Codex device login expired; try /login codex again".into());}
            let pause=std::time::Duration::from_secs_f64(interval).min(left);
            self.codex_wait(async{tokio::time::sleep(pause).await;Ok(())},"auth")?;
            if std::time::Instant::now()>=deadline{return Err("Codex device login expired; try /login codex again".into());}
            let (status,token)=self.codex_http(client.post(format!("{base}/api/accounts/deviceauth/token"))
                .timeout(deadline.saturating_duration_since(std::time::Instant::now()).min(std::time::Duration::from_secs(30)))
                .json(&json!({"device_auth_id":device_id,"user_code":code})))?;
            if status==200{
                let authorization=token["authorization_code"].as_str().filter(|s|!s.is_empty()).ok_or("Codex device token response missing authorization code")?;
                let verifier=token["code_verifier"].as_str().filter(|s|!s.is_empty()).ok_or("Codex device token response missing verifier")?;
                let (status,body)=self.codex_http(client.post(format!("{base}/oauth/token")).header("Accept","application/json")
                    .form(&[("grant_type","authorization_code"),("client_id",CODEX_CLIENT_ID),("code",authorization),
                        ("code_verifier",verifier),("redirect_uri","https://auth.openai.com/deviceauth/callback")]))?;
                if status!=200{return Err(format!("Codex token exchange failed (HTTP {status}); try /login codex again").into());}
                return self.store_credential("openai-codex",Some(codex_token_value(&body)?));
            }
            let error=token["error"].as_str().or(token["error"]["code"].as_str()).unwrap_or("");
            if status==403||status==404||error=="deviceauth_authorization_pending"{continue;}
            if error=="slow_down"{interval=(interval+5.0).min(60.0);continue;}
            return Err(format!("Codex device authorization denied or failed (HTTP {status})").into());
        }
    }
    fn refresh_codex(&mut self)->Result<()>{
        // Lock spans reload/check/exchange/write: rotating refresh tokens are used once.
        let _lock=self.auth_lock()?;
        self.auth=load_json(&self.home.join("auth.json"))?;
        let credential=&self.auth["openai-codex"];
        let refresh=oauth_token(credential,"refresh").map_err(|_|"Codex stored credential invalid; /login codex again")?.to_string();
        if credential["expires"].as_u64().unwrap_or(0)>now_ms() as u64{
            oauth_token(credential,"access").map_err(|_|"Codex stored credential invalid; /login codex again")?;
            credential["accountId"].as_str().filter(|s|!s.is_empty()&&s.len()<=512&&s.bytes().all(|c|(33..=126).contains(&c)))
                .ok_or("Codex stored credential has invalid account ID; /login codex again")?;
            return Ok(());
        }
        let client=reqwest::Client::builder().timeout(std::time::Duration::from_secs(30)).redirect(reqwest::redirect::Policy::none()).build()?;
        let (status,body)=self.codex_http(client.post(format!("{}/oauth/token",Self::codex_auth_base())).header("Accept","application/json")
            .form(&[("grant_type","refresh_token"),("client_id",CODEX_CLIENT_ID),("refresh_token",refresh.as_str())]))?;
        if status!=200{return Err(format!("Codex token refresh failed (HTTP {status}); /login codex again").into());}
        let next=codex_token_value(&body)?;
        let mut auth=self.auth.clone();auth["openai-codex"]=next;
        write_private_json(&self.home.join("auth.json"),&auth)?;self.auth=auth;Ok(())
    }
    fn codex_request(&self,client:&reqwest::Client,url:&str,key:&str,id:&str,messages:&[Value])->Result<reqwest::RequestBuilder>{
        let mut instructions=Vec::new();let mut input=Vec::new();
        for message in messages{
            let role=message["role"].as_str().unwrap_or("user");
            if role=="system"||role=="developer"{
                if let Some(text)=message["content"].as_str(){instructions.push(text.to_string());}
                else if let Some(parts)=message["content"].as_array(){for p in parts{if let Some(text)=p["text"].as_str(){instructions.push(text.to_string());}}}
                continue;
            }
            let mut item=message.clone();
            if let Some(text)=item["content"].as_str(){item["content"]=json!([{"type":if role=="assistant"{"output_text"}else{"input_text"},"text":text}]);}
            else if let Some(parts)=item["content"].as_array_mut(){for part in parts{
                if part["type"]=="text"{part["type"]=json!(if role=="assistant"{"output_text"}else{"input_text"});}
                else if part["type"]=="image_url"{*part=json!({"type":"input_image","image_url":part["image_url"]["url"]});}
            }}
            input.push(item);
        }
        let session=self.journal.path.file_stem().and_then(|s|s.to_str()).unwrap_or("py-session");
        let mut body=json!({"model":id,"store":false,"stream":true,"instructions":instructions.join("\n"),"input":input,
            "text":{"verbosity":"medium"},"include":["reasoning.encrypted_content"],"prompt_cache_key":session});
        let effort=self.effort.as_str();
        let meta=self.reasoning_metadata(&format!("openai-codex/{id}"));
        reasoning_fields("openai-codex-responses","openai-codex",id,&meta,effort,4096)?;
        if effort!="off"||meta["reasoning_efforts"].is_array(){body["reasoning"]=json!({"effort":if effort=="off"{"none"}else if effort=="minimal"&&(id.starts_with("gpt-5.2")||id.starts_with("gpt-5.3")){"low"}else{effort},"summary":"auto"});}
        let endpoint=if url.ends_with("/codex/responses"){url.to_string()}else if url.ends_with("/codex"){format!("{url}/responses")}else{format!("{url}/codex/responses")};
        Ok(client.post(endpoint).bearer_auth(key).header("chatgpt-account-id",self.auth["openai-codex"]["accountId"].as_str().ok_or("Codex account ID missing")?)
            .header("OpenAI-Beta","responses=experimental").header("Accept","text/event-stream").header("originator","py")
            .header("User-Agent","py-rust/0.1.0").header("session-id",session).header("x-client-request-id",session).json(&body))
    }
    fn codex_sse(&mut self,request:reqwest::RequestBuilder,operation:&str)->Result<Value>{
        let mut secrets=Vec::new();credential_secret_values(&self.auth,&mut secrets);
        if let Ok((_,key,_))=self.provider_config("openai-codex/gpt-6.1-sol"){if !key.is_empty(){secrets.push(key);}}
        self.codex_wait(async{
            let mut response=request.send().await.map_err(|_|"Codex provider network error; outcome unknown")?;
            let status=response.status();
            if !status.is_success(){
                let mut bytes=Vec::new();
                while let Some(chunk)=response.chunk().await.map_err(|_|"Codex error response disconnected")?{
                    if bytes.len()+chunk.len()>65536{return Err(format!("Codex provider HTTP {status}; error body exceeds 64 KiB; no automatic retry").into());}
                    bytes.extend_from_slice(&chunk);
                }
                let detail=codex_error_detail(&bytes,&secrets);
                return Err(format!("Codex provider HTTP {status}{detail}; no automatic retry").into());
            }
            codex_read_sse(response).await
        },operation)
    }
}
fn credential_secret_values(value:&Value,out:&mut Vec<String>){
    if let Some(object)=value.as_object(){for (key,value) in object{
        if matches!(key.as_str(),"key"|"access"|"refresh"|"access_token"|"refresh_token"|"id_token"|"accountId"){
            if let Some(value)=value.as_str().filter(|v|!v.is_empty()){out.push(value.into());}
        }else{credential_secret_values(value,out);}
    }}else if let Some(values)=value.as_array(){for value in values{credential_secret_values(value,out);}}
}
fn codex_error_detail(bytes:&[u8],secrets:&[String])->String{
    let Ok(text)=std::str::from_utf8(bytes)else{return ": unreadable error response".into();};
    let mut detail=if let Ok(body)=serde_json::from_str::<Value>(text){
        let error=&body["error"];
        [error["message"].as_str().or(error.as_str()).or(body["message"].as_str()).or(body["detail"].as_str()),
            error["code"].as_str(),error["param"].as_str()].into_iter().flatten().collect::<Vec<_>>().join("; ")
    }else{text.to_string()};
    // Redact before truncation so a token crossing the preview boundary cannot
    // leak a partial prefix. Never store the raw rejection body in the journal.
    let mut secrets:Vec<_>=secrets.iter().filter(|s|!s.is_empty()).collect();secrets.sort_by_key(|s|std::cmp::Reverse(s.len()));
    for secret in secrets{detail=detail.replace(secret,"[redacted]");}
    let truncated=detail.chars().count()>1024;
    let mut safe:String=detail.chars().take(1024).map(|c|if c.is_control()||matches!(c,'\u{2028}'|'\u{2029}'){ ' ' }else{c}).collect();
    if truncated{safe.push_str(" [truncated]");}
    if safe.trim().is_empty(){String::new()}else{format!(": {}",safe.trim())}
}
async fn codex_read_sse(mut response:reqwest::Response)->Result<Value>{
    let mut bytes=Vec::new();let mut data=Vec::new();let mut delta=String::new();let mut total=0usize;
    while let Some(chunk)=response.chunk().await.map_err(|_|"Codex stream disconnected; outcome unknown")?{
        total+=chunk.len();if total>16*1024*1024{return Err("Codex stream exceeds 16 MiB; outcome unknown".into());}
        bytes.extend_from_slice(&chunk);
        let mut consumed=0usize;
        while let Some(offset)=bytes[consumed..].iter().position(|b|*b==b'\n'){
            let end=consumed+offset;let line=&bytes[consumed..end];consumed=end+1;
            let line=line.strip_suffix(b"\r").unwrap_or(line);
            if line.is_empty(){
                if data.is_empty(){continue;}
                let text=std::str::from_utf8(&data).map_err(|_|"Codex SSE has invalid UTF-8")?;
                if text.trim()=="[DONE]"{data.clear();continue;}
                let event:Value=serde_json::from_str(text).map_err(|_|"Codex SSE has invalid JSON")?;data.clear();
                match event["type"].as_str().unwrap_or(""){
                    "response.output_text.delta"=>{if let Some(text)=event["delta"].as_str(){delta.push_str(text);}},
                    "error"|"response.failed"|"response.incomplete"=>return Err("Codex response failed or incomplete; no automatic retry".into()),
                    "response.done"|"response.completed"=>{
                        let mut body=event["response"].clone();
                        if body["status"]!="completed"{return Err("Codex response did not complete successfully; no automatic retry".into());}
                        if (body["output"].is_null()||body["output"].as_array().is_some_and(Vec::is_empty))&&!delta.is_empty(){
                            body["output"]=json!([{"type":"message","content":[{"type":"output_text","text":delta}]}]);
                        }
                        return Ok(body);
                    },
                    _=>{}
                }
            }else if let Some(part)=line.strip_prefix(b"data:"){
                if !data.is_empty(){data.push(b'\n');}
                data.extend_from_slice(part.strip_prefix(b" ").unwrap_or(part));
                if data.len()>2*1024*1024{return Err("Codex SSE frame exceeds 2 MiB; outcome unknown".into());}
            }
        }
        bytes.drain(..consumed);
        if bytes.len()>2*1024*1024{return Err("Codex SSE line exceeds 2 MiB; outcome unknown".into());}
        tokio::task::yield_now().await;
    }
    Err("Codex stream ended without successful terminal event; outcome unknown".into())
}

#[cfg(test)]
mod codex_e2e {
    fn fixture(body:&str){
        let prefix=r#"
import os,sys,json,tempfile,threading,subprocess,time,base64,stat
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
home=tempfile.mkdtemp(prefix='py-codex-e2e-')
requests=[];responses=[]
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_POST(self):
  raw=self.rfile.read(int(self.headers.get('Content-Length',0)))
  requests.append((self.path,dict(self.headers),raw.decode()))
  if not responses:self.send_error(500);return
  status,data,kind=responses.pop(0)
  self.send_response(status);self.send_header('Content-Type',kind)
  if status==307:self.send_header('Location',base+'/unexpected')
  self.end_headers()
  if callable(data):data=data()
  if isinstance(data,dict):data=json.dumps(data)
  if isinstance(data,list):
   for part in data:self.wfile.write(part);self.wfile.flush();time.sleep(.001)
  else:self.wfile.write(data.encode())
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
base='http://127.0.0.1:'+str(server.server_port)
def jwt(account='acct_fixture'):
 return 'header.'+base64.urlsafe_b64encode(json.dumps({'https://api.openai.com/auth':{'chatgpt_account_id':account}}).encode()).decode().rstrip('=')+'.signature'
def tokens(account='acct_fixture',ttl=3600):return {'access_token':jwt(account),'refresh_token':'refresh-SECRET','expires_in':ttl}
def jsonreply(data,status=200):return (status,data,'application/json')
def sse(text,status='completed',event='response.completed'):
 return 'data: '+json.dumps({'type':event,'response':{'status':status,'output':[{'type':'message','content':[{'type':'output_text','text':text}]}],'usage':{'input_tokens':13,'output_tokens':7,'input_tokens_details':{'cached_tokens':3}}}})+'\r\n\r\n'
def auth(expires=0):
 with open(home+'/auth.json','w') as f:json.dump({'openai-codex':{'type':'oauth','access':jwt(),'refresh':'refresh-SECRET','expires':int(expires),'accountId':'acct_fixture'}},f)
 os.chmod(home+'/auth.json',0o600)
with open(home+'/config.json','w') as f:json.dump({'model':'openai-codex/gpt-5.3-codex','effort':'high','providers':{'openai-codex':{'base_url':base}}},f)
env=dict(os.environ,PY_HOME=home,PY_CODEX_AUTH_BASE_URL=base)
def run(commands,extra=None):
 p=subprocess.Popen([sys.argv[1],'--json','--json-input','--no-model'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=dict(env,**(extra or {})))
 out,err=p.communicate(''.join(json.dumps(c)+'\n' for c in commands).encode(),timeout=12)
 assert p.returncode==0,(out,err)
 return [json.loads(l) for l in out.splitlines()]
def py(source):return {'id':'python','kind':'python','source':source}
def login():return {'id':'login','kind':'login','provider':'codex','method':'oauth'}
def journals():
 return ''.join(open(home+'/sessions/'+n).read() for n in os.listdir(home+'/sessions') if n.endswith('.jsonl'))
def completed(events,id):return next(v for v in events if v.get('kind')=='completed' and v.get('command_id')==id)
"#;
        let script=format!("{prefix}\n{body}");
        let out=crate::test_command("python3").args(["-c",&script,
            &std::env::var("PY_HARNESS_BIN").expect("build CLI and set PY_HARNESS_BIN")]).output().unwrap();
        assert!(out.status.success(),"{}\n{}",String::from_utf8_lossy(&out.stderr),String::from_utf8_lossy(&out.stdout));
    }
    #[test]
    fn e2e_codex_default_login_selects_current_model_and_outer_turn(){fixture(r#"
json.dump({'providers':{'openai-codex':{'base_url':base}}},open(home+'/config.json','w'))
responses.extend([jsonreply({'device_auth_id':'d','user_code':'ABCD','interval':0}),jsonreply({'authorization_code':'code-SECRET','code_verifier':'verifier-SECRET'}),jsonreply(tokens()),(200,sse("agent.say('2')\nagent.loop.stop()"),'text/event-stream')])
p=subprocess.run([sys.argv[1],'--json','--json-input'],input=''.join(json.dumps(c)+'\n' for c in [login(),{'id':'outer','kind':'submit','text':'1+1'}]),capture_output=True,text=True,env=env,timeout=12)
assert p.returncode==0,(p.stdout,p.stderr)
e=[json.loads(l) for l in p.stdout.splitlines()]
assert completed(e,'login')['status']=='ok' and completed(e,'outer')['status']=='ok',e
body=json.loads(requests[-1][2]);assert body['model']=='gpt-6.1-sol' and body['text']['verbosity']=='medium',body
assert body['reasoning']['effort']=='medium' and requests[-1][0]=='/codex/responses',requests
assert any(v['kind']=='say' and v.get('text')=='2' for v in e),e
assert 'openai-codex/gpt-6.1-sol' in journals()
# Later processes use stored Codex only for an implicit, credentialless default.
e=run([{'id':'status','kind':'status'}]);assert next(v for v in e if v['kind']=='ready')['model']=='openai-codex/gpt-6.1-sol',e
config=json.load(open(home+'/config.json'));config['model']='openai/gpt-4o';json.dump(config,open(home+'/config.json','w'))
e=run([{'id':'status','kind':'status'}]);assert next(v for v in e if v['kind']=='ready')['model']=='openai/gpt-4o',e
# Explicit saved selections remain authoritative on resume.
config.pop('model');json.dump(config,open(home+'/config.json','w'))
path=next(v['session'] for v in e if v['kind']=='ready')
p=subprocess.run([sys.argv[1],'--json','--json-input','--no-model','--resume',path],input='',capture_output=True,text=True,env=env,timeout=12)
assert p.returncode==0,(p.stdout,p.stderr)
assert next(json.loads(l) for l in p.stdout.splitlines() if json.loads(l)['kind']=='ready')['model']=='openai/gpt-4o',p.stdout
assert json.load(open(home+'/config.json')).get('model') is None
assert all(s not in journals() for s in ['code-SECRET','verifier-SECRET','refresh-SECRET',jwt()])
"#);}
    #[test]
    fn e2e_codex_login_preserves_usable_model_and_cancelled_login(){fixture(r#"
json.dump({'providers':{'openai-codex':{'base_url':base}}},open(home+'/config.json','w'))
json.dump({'openai':{'type':'api_key','key':'working-api-SECRET'}},open(home+'/auth.json','w'))
responses.extend([jsonreply({'device_auth_id':'d','user_code':'ABCD','interval':0}),jsonreply({'authorization_code':'c','code_verifier':'v'}),jsonreply(tokens())])
e=run([login(),{'id':'status','kind':'status'}])
assert completed(e,'login')['status']=='ok',e
assert next(v for v in e if v['kind']=='status')['model']=='openai/gpt-4.1',e
os.unlink(home+'/auth.json')
responses.append(jsonreply({'error':'access_denied'},403))
e=run([login(),{'id':'status','kind':'status'}]);assert any(v['kind']=='error' and v.get('command_id')=='login' for v in e),e
assert next(v for v in e if v['kind']=='status')['model']=='openai/gpt-4.1',e
assert 'working-api-SECRET' not in journals(),journals()
"#);}
    #[test]
    fn e2e_codex_http_rejection_details_redaction_bounds_and_no_retry(){fixture(r#"
auth(time.time()*1000+3600000)
responses.append(jsonreply({'error':{'message':'Model unavailable for subscription. '+jwt()+' refresh-SECRET acct_fixture\n\x1b[31m','code':'model_not_found','param':'model'}},400))
e=run([py("agent.llm('hello',model='codex/gpt-5.3-codex')")]);assert completed(e,'python')['status']=='error',e
assert len(requests)==1,requests
j=journals();assert 'Model unavailable for subscription' in j and 'model_not_found' in j and '[redacted]' in j,j
assert all(s not in j and s not in json.dumps(e) for s in [jwt(),'refresh-SECRET','acct_fixture']),e
assert '\\u001b' not in j,j
responses.append(jsonreply({'error':{'message':'x'*80000}},400))
e=run([py("agent.llm('hello',model='codex/gpt-5.3-codex')")]);assert completed(e,'python')['status']=='error',e
assert len(requests)==2 and 'error body exceeds 64 KiB' in journals(),requests
"#);}
    #[test]
    fn e2e_codex_separate_input_limit_accounting_and_predispatch_rejection(){
        assert_eq!(super::model_input_budget(1050000,&serde_json::json!({"max_input_tokens":922000}),4096),922000);
        assert_eq!(super::model_input_budget(32000,&serde_json::json!({"max_input_tokens":922000}),4096),27904);
        fixture(r#"
config=json.load(open(home+'/config.json'));config['model']='openai-codex/gpt-6.1-sol';json.dump(config,open(home+'/config.json','w'))
e=run([py("agent.llm('x'*(258400*3),model='codex/gpt-6.1-sol')")])
ready=next(v for v in e if v['kind']=='ready');usage=ready['context_usage']
assert usage['model_context_limit']==272000 and usage['input_token_budget']==258400,usage
assert usage['remaining_input_tokens']==258400-usage['estimated_input_tokens'],usage
assert completed(e,'python')['status']=='error',e
assert not requests,requests
assert 'exceeds model input budget 258400' in journals(),journals()
"#);}
    #[test]
    fn e2e_codex_current_model_alias_effort_and_catalog(){fixture(r#"
auth(time.time()*1000+3600000)
responses.extend([(200,sse('sol-answer'),'text/event-stream'),(200,sse('luna-answer'),'text/event-stream')])
commands='/model codex/sol61\n/effort max\n@print(agent.llm("hello",model="codex/gpt-6.1-sol"))\n/effort minimal\n/model codex/luna6\n/effort off\n@print(agent.llm("hello",model="codex/gpt-6-luna"))\n'
p=subprocess.run([sys.argv[1],'--json','--no-model'],input=commands,capture_output=True,text=True,env=env,timeout=12)
assert p.returncode==0,(p.stdout,p.stderr)
e=[json.loads(l) for l in p.stdout.splitlines()]
completed_python=[v for v in e if v['kind']=='completed'];assert len(completed_python)==2 and all(v['status']=='ok' for v in completed_python),e
assert any(v['kind']=='error' and 'unsupported' in v.get('error','') for v in e),e
assert len(requests)==2,requests
bodies=[json.loads(r[2]) for r in requests]
assert bodies[0]['model']=='gpt-6.1-sol' and bodies[0]['reasoning']['effort']=='max',bodies
assert bodies[1]['model']=='gpt-6-luna' and bodies[1]['reasoning']['effort']=='none',bodies
"#);}
    #[test]
    fn e2e_codex_device_login_pending_and_protocol_sse(){fixture(r#"
responses.extend([jsonreply({'device_auth_id':'device-SECRET','user_code':'ABCD','interval':'0'}),jsonreply('',403),jsonreply({'authorization_code':'code-SECRET','code_verifier':'verifier-SECRET'}),jsonreply(tokens()),(200,sse('answer'),'text/event-stream')])
e=run([login(),py("print(agent.llm('hello',model='codex/gpt-5.3-codex'))")])
assert completed(e,'login')['status']=='ok',e
assert completed(e,'python')['status']=='ok',e
assert [r[0] for r in requests]==['/api/accounts/deviceauth/usercode','/api/accounts/deviceauth/token','/api/accounts/deviceauth/token','/oauth/token','/codex/responses'],requests
from urllib.parse import parse_qs
form=parse_qs(requests[3][2]);assert form['code']==['code-SECRET'] and form['code_verifier']==['verifier-SECRET']
assert form['redirect_uri']==['https://auth.openai.com/deviceauth/callback'] and form['client_id']==['app_EMoamEEZ73f0CkXaXp7hrann']
h={k.lower():v for k,v in requests[4][1].items()};body=json.loads(requests[4][2])
assert h['authorization']=='Bearer '+jwt() and h['chatgpt-account-id']=='acct_fixture'
assert h['accept']=='text/event-stream' and h['originator']=='py' and h['openai-beta']=='responses=experimental'
assert h['session-id']==h['x-client-request-id']==body['prompt_cache_key'] and 'session_id' not in h
assert body['instructions']=='You are a helpful assistant.' and not any(m['role'] in ('system','developer') for m in body['input'])
assert body['reasoning']=={'effort':'high','summary':'auto'} and body['include']==['reasoning.encrypted_content']
assert body['store']==False and body['stream']==True and 'max_output_tokens' not in body
stored=json.load(open(home+'/auth.json'));assert stored['openai-codex']['accountId']=='acct_fixture'
assert stat.S_IMODE(os.stat(home+'/auth.json').st_mode)==0o600
j=journals();assert all(s not in j for s in ['refresh-SECRET','code-SECRET','verifier-SECRET','device-SECRET',jwt()]),j
assert 'answer' in j and 'input_tokens' in j
"#);}
    #[test]
    fn e2e_codex_refresh_once_then_offline_model_inventory(){fixture(r#"
auth(0)
responses.extend([jsonreply(tokens()),(200,sse('one'),'text/event-stream'),(200,sse('two'),'text/event-stream')])
e=run([{'id':'models','kind':'models'},py("print(agent.llm('one')); print(agent.llm('two'))")])
assert completed(e,'python')['status']=='ok',e
models=next(v['models'] for v in e if v['kind']=='models');assert any(m['id']=='openai-codex/gpt-5.3-codex' for m in models)
assert [r[0] for r in requests]==['/oauth/token','/codex/responses','/codex/responses'],requests
from urllib.parse import parse_qs
assert parse_qs(requests[0][2])['grant_type']==['refresh_token']
assert json.load(open(home+'/auth.json'))['openai-codex']['expires']>time.time()*1000
assert 'refresh-SECRET' not in journals()
"#);}
    #[test]
    fn e2e_codex_terminal_failures_and_truncated_sse_never_succeed(){fixture(r#"
auth(time.time()*1000+3600000)
for stream in [sse('BAD','incomplete'),sse('BAD','failed','response.failed'),'data: '+json.dumps({'type':'response.output_text.delta','delta':'BAD'})+'\n\n', 'data: not-json\n\n',sse('BAD','cancelled'),'data: [DONE]\n\n','data: '+json.dumps({'type':'response.completed','response':{'output':[]}})+'\n\n']:
 responses.append((200,stream,'text/event-stream'))
 e=run([py("print(agent.llm('hello'))")]);assert completed(e,'python')['status']=='error',e
assert len(requests)==7 and not any('"kind":"operation_complete"' in line for line in journals().splitlines()),journals()
"#);}
    #[test]
    fn e2e_codex_invalid_login_and_failed_refresh_preserve_credentials(){fixture(r#"
for bad in [{'access_token':'bad-SECRET','refresh_token':'r-SECRET','expires_in':3600},tokens(ttl=-1),dict(tokens(),refresh_token=''),dict(tokens(),expires_in='3600'),dict(tokens(),access_token=jwt('')),dict(tokens(),access_token=None)]:
 before=len(requests)
 auth(0);original=open(home+'/auth.json').read()
 responses.extend([jsonreply({'device_auth_id':'d','user_code':'ABCD','interval':0}),jsonreply({'authorization_code':'code-SECRET','code_verifier':'v'}),jsonreply(bad)])
 e=run([login()]);assert any(v['kind']=='error' for v in e),e
 assert len(requests)==before+3,requests
 assert open(home+'/auth.json').read()==original
 assert all(s not in journals() for s in ['bad-SECRET','r-SECRET','refresh-SECRET','code-SECRET',jwt()])
auth(0);original=open(home+'/auth.json').read();responses.append(jsonreply({'error':'refresh-SECRET'},401))
e=run([py("agent.llm('hello')")]);assert completed(e,'python')['status']=='error'
assert open(home+'/auth.json').read()==original and 'refresh-SECRET' not in journals()
"#);}
    #[test]
    fn e2e_codex_device_denied_expired_and_slowdown(){fixture(r#"
responses.extend([jsonreply({'device_auth_id':'d','user_code':'ABCD','interval':0}),jsonreply({'error':'access_denied'},400)])
e=run([login()]);assert any(v['kind']=='error' for v in e) and not os.path.exists(home+'/auth.json')
responses.extend([jsonreply({'device_auth_id':'d','user_code':'ABCD','interval':1})])
e=run([login()],{'PY_CODEX_DEVICE_TIMEOUT_SECONDS':'0.02'});assert any('expired' in v.get('error','') for v in e),e
responses.extend([jsonreply({'device_auth_id':'d','user_code':'ABCD','interval':0}),jsonreply({'error':{'code':'slow_down'}},429),jsonreply({'authorization_code':'c','code_verifier':'v'}),jsonreply(tokens())])
t=time.monotonic();e=run([login()]);assert completed(e,'login')['status']=='ok' and time.monotonic()-t>=5
"#);}
    #[test]
    fn e2e_codex_sse_deltas_crlf_split_and_image_input(){fixture(r#"
auth(time.time()*1000+3600000)
stream=': comment\r\n\r\ndata: '+json.dumps({'type':'response.output_text.delta','delta':'hello '})+'\r\n\r\ndata: '+json.dumps({'type':'response.output_text.delta','delta':'world'})+'\r\n\r\ndata: '+json.dumps({'type':'response.done','response':{'status':'completed'}})+'\r\n\r\n'
raw=stream.encode();responses.append((200,[raw[i:i+3] for i in range(0,len(raw),3)],'text/event-stream'))
import struct,zlib
def chunk(t,d):return struct.pack('>I',len(d))+t+d+struct.pack('>I',zlib.crc32(t+d))
png=base64.b64encode(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\0\0\0\0'))+chunk(b'IEND',b'')).decode()
e=run([py("print(agent.llm('look',images=[{'base64':"+repr(png)+"}]))")])
assert completed(e,'python')['status']=='ok',e
body=json.loads(requests[0][2]);parts=body['input'][0]['content'];assert parts[0]['type']=='input_text' and parts[1]['type']=='input_image',body
assert 'hello world' in journals()
"#);}
    #[test]
    fn e2e_auth_two_live_sessions_merge_updates(){fixture(r#"
p1=subprocess.Popen([sys.argv[1],'--json','--json-input','--no-model'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
p2=subprocess.Popen([sys.argv[1],'--json','--json-input','--no-model'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
assert json.loads(p1.stdout.readline())['kind']=='ready';assert json.loads(p2.stdout.readline())['kind']=='ready'
def command(p,v):
 p.stdin.write((json.dumps(v)+'\n').encode());p.stdin.flush()
 while True:
  e=json.loads(p.stdout.readline())
  if e['kind']=='completed':return e
assert command(p1,{'id':'a','kind':'login','provider':'openai','key':'key-one-SECRET'})['status']=='ok'
assert command(p2,{'id':'b','kind':'login','provider':'anthropic','key':'key-two-SECRET'})['status']=='ok'
assert set(json.load(open(home+'/auth.json')))=={'openai','anthropic'}
assert command(p1,{'id':'c','kind':'logout','provider':'openai'})['status']=='ok'
assert set(json.load(open(home+'/auth.json')))=={'anthropic'}
for p in [p1,p2]:p.communicate(timeout=4)
assert all(s not in journals() for s in ['key-one-SECRET','key-two-SECRET'])
"#);}
    #[test]
    fn e2e_codex_cancel_during_device_wait_and_network(){fixture(r#"
import queue
for phase in ['wait','signal-wait','device-network','provider-network']:
 auth(0 if phase!='provider-network' else int(time.time()*1000+3600000));original=open(home+'/auth.json').read()
 responses.clear();requests.clear()
 if phase in ('wait','signal-wait'):responses.append(jsonreply({'device_auth_id':'d','user_code':'ABCD','interval':30}))
 elif phase=='device-network':responses.append((200,lambda:(time.sleep(2) or '{}'),'application/json'))
 else:responses.append((200,lambda:(time.sleep(2) or sse('late')),'text/event-stream'))
 p=subprocess.Popen([sys.argv[1],'--json','--json-input','--no-model'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
 q=queue.Queue()
 def reader():
  for line in p.stdout:q.put(json.loads(line))
 threading.Thread(target=reader,daemon=True).start()
 def until(kind,id=None):
  end=time.monotonic()+4
  while True:
   e=q.get(timeout=max(.01,end-time.monotonic()))
   if e['kind']==kind and (id is None or e.get('command_id')==id):return e
 def send(v):p.stdin.write((json.dumps(v)+'\n').encode());p.stdin.flush()
 until('ready');send(py("agent.llm('hello')") if phase=='provider-network' else login())
 if phase in ('wait','signal-wait'):until('login_prompt')
 else:
  end=time.monotonic()+3
  while not requests:assert time.monotonic()<end;time.sleep(.01)
 if phase=='signal-wait':
  import signal;os.kill(p.pid,signal.SIGINT)
 else:send({'id':'cancel','kind':'interrupt'});assert until('completed','cancel')['status']=='ok'
 if phase=='provider-network':assert until('completed','python')['status']=='cancelled'
 else:assert 'cancelled' in until('error')['error']
 assert open(home+'/auth.json').read()==original
 send({'id':'next','kind':'python','source':"print('still-alive')"});assert until('completed','next')['status']=='ok'
 p.communicate(timeout=4)
 assert 'refresh-SECRET' not in journals()
"#);}
    #[test]
    fn e2e_codex_parallel_refresh_is_serialized(){fixture(r#"
auth(0)
responses.extend([jsonreply(tokens()),(200,sse('one'),'text/event-stream'),(200,sse('two'),'text/event-stream')])
processes=[]
for _ in range(2):
 p=subprocess.Popen([sys.argv[1],'--json','--json-input','--no-model'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
 assert json.loads(p.stdout.readline())['kind']=='ready'
 processes.append(p)
for p in processes:p.stdin.write((json.dumps(py("print(agent.llm('hello'))"))+'\n').encode());p.stdin.flush()
for p in processes:
 out,err=p.communicate(timeout=6);events=[json.loads(s) for s in out.splitlines()]
 assert completed(events,'python')['status']=='ok',(events,err)
assert sum(path=='/oauth/token' for path,_,_ in requests)==1,requests
"#);}
    #[test]
    fn e2e_codex_inventory_is_offline_and_initial_device_fields_validate(){fixture(r#"
e=run([{'id':'models','kind':'models'}]);models=next(v['models'] for v in e if v['kind']=='models')
assert not requests
assert any(m['id']=='openai-codex/gpt-5.3-codex' and not m['credential_backed'] and m['api']=='openai-codex-responses' for m in models)
for bad in [{'device_auth_id':'d','user_code':'ABCD'}, {'device_auth_id':'d','user_code':'ABCD','interval':-1},{'device_auth_id':'','user_code':'ABCD','interval':0},{'device_auth_id':'d','user_code':'ABCD\x1b','interval':0}]:
 before=len(requests);responses.append(jsonreply(bad));e=run([login()])
 assert any(v['kind']=='error' for v in e) and len(requests)==before+1
 assert not os.path.exists(home+'/auth.json')
"#);}
    #[test]
    fn e2e_codex_http_failures_hide_bodies_and_never_retry(){fixture(r#"
auth(int(time.time()*1000+3600000))
for status in [307,401,429,503]:
 responses.append(jsonreply({'error':'REFLECTED-'+jwt()+' refresh-SECRET'},status));before=len(requests)
 e=run([py("agent.llm('hello')")]);assert completed(e,'python')['status']=='error'
 assert len(requests)==before+1
 assert jwt() not in journals() and 'refresh-SECRET' not in journals()
"#);}
    #[test]
    fn e2e_codex_device_pending_404_and_json_error_code(){fixture(r#"
responses.extend([jsonreply({'device_auth_id':'d','user_code':'ABCD','interval':0}),jsonreply('not-json',404),jsonreply({'error':'deviceauth_authorization_pending'},400),jsonreply({'authorization_code':'c','code_verifier':'v'}),jsonreply(tokens())])
e=run([login()]);assert completed(e,'login')['status']=='ok',e
assert [r[0] for r in requests]==['/api/accounts/deviceauth/usercode']+['/api/accounts/deviceauth/token']*3+['/oauth/token'],requests
assert json.load(open(home+'/auth.json'))['openai-codex']['accountId']=='acct_fixture'
"#);}
    #[test]
    fn e2e_codex_outer_loop_executes_source_from_completed_sse(){fixture(r#"
auth(int(time.time()*1000+3600000))
code="agent.say('codex-ready'); agent.loop.stop()"
responses.append((200,sse(code),'text/event-stream'))
p=subprocess.Popen([sys.argv[1],'--json','--json-input'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
out,err=p.communicate((json.dumps({'id':'turn','kind':'submit','text':'hello'})+'\n').encode(),timeout=6)
e=[json.loads(s) for s in out.splitlines()]
assert p.returncode==0 and completed(e,'turn')['status']=='ok',(e,err)
assert any(v['kind']=='say' and v['text']=='codex-ready' for v in e),e
assert len(requests)==1 and requests[0][0]=='/codex/responses'
body=json.loads(requests[0][2]);assert body['instructions'].startswith('You are a Python coding agent.')
assert not any(m['role']=='system' for m in body['input']) and 'max_output_tokens' not in body
assert code in journals() and jwt() not in journals()
"#);}
}

#[cfg(test)]
mod completion_e2e {
    fn pty_fixture(body:&str){
        let setup=r#"
import os,sys,pty,fcntl,termios,struct,subprocess,select,time,json,tempfile,re,stat
home=tempfile.mkdtemp(prefix='py-completion-');cwd=tempfile.mkdtemp(prefix='py-completion-cwd-')
master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',36,100,0,0))
def controlling():
 os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
env=dict(os.environ,PY_HOME=home,HOME=cwd,PY_MODEL='openai/gpt-5',TERM='xterm-256color')
env['PATH']=cwd+os.pathsep+env.get('PATH','')
p=subprocess.Popen([sys.argv[1],'--no-model'],stdin=slave,stdout=slave,stderr=slave,cwd=cwd,env=env,preexec_fn=controlling)
os.close(slave);transcript=bytearray()
def pump():
 if select.select([master],[],[],.02)[0]:
  try:transcript.extend(os.read(master,65536))
  except OSError:pass
 return transcript.decode(errors='replace')
def wait_for(predicate,label):
 deadline=time.monotonic()+6
 while time.monotonic()<deadline:
  pump()
  if predicate():return
  if p.poll() is not None:raise AssertionError(('CLI exited',p.returncode,bytes(transcript)))
 raise AssertionError((label,bytes(transcript)))
def journal():
 try:
  f=os.path.join(home,'sessions',os.listdir(os.path.join(home,'sessions'))[0])
  return [json.loads(l) for l in open(f) if l.endswith('\n')]
 except (OSError,IndexError,json.JSONDecodeError):return []
def send(s):os.write(master,s.encode() if isinstance(s,str) else s)
last_prompt_count=0
def enter(s):
 global last_prompt_count
 last_prompt_count=transcript.count(b'\x1b[?2004h')
 # Ambiguous Tab now opens an explicit picker. Preserve every original source
 # assertion while selecting its ranked first option, rather than assuming an
 # ambiguous Tab silently inserted that option as the old editor did.
 pieces=s.split('\t')
 for piece in pieces[:-1]:
  send(piece)
  # Let queued prompt/typing redraws finish; only a response after the actual
  # Tab is allowed to decide whether this completion opened a picker.
  end=time.monotonic()+.5
  while time.monotonic()<end and select.select([master],[],[],.04)[0]:pump()
  start=len(transcript);send('\t')
  wait_for(lambda:b'\x1b[?25l' in transcript[start:] or b'\x1b[?25h' in transcript[start:],'completion response')
  if b'\x1b[?25l' in transcript[start:]:
   send('\r');wait_for(lambda:b'\x1b[0J' in transcript[start:],'completion picker selection')
 send(pieces[-1]+'\r')
def paste(s):
 global last_prompt_count
 last_prompt_count=transcript.count(b'\x1b[?2004h');send('\x1b[200~'+s+'\x1b[201~\r')
def command(s):
 enter(s);wait_for(lambda:transcript.count(b'\x1b[?2004h')>last_prompt_count,'next ready editor')
def code(source):
 paste('@'+source);expect_source(source)
def expect_source(source):
 wait_for(lambda:any(v['kind']=='code' and v['payload'].get('source')==source for v in journal()),'completed source '+source)
 op=next(v['payload']['operation'] for v in journal() if v['kind']=='code' and v['payload'].get('source')==source)
 wait_for(lambda:any(v['kind']=='completion' and v['payload'].get('command_id')==op for v in journal()),'cell completion')
 result=next(v for v in journal() if v['kind']=='completion' and v['payload'].get('command_id')==op)
 assert result['payload']['status']=='ok',(result,bytes(transcript))
 wait_for(lambda:transcript.count(b'\x1b[?2004h')>last_prompt_count,'ready editor after cell')
def stdout():return ''.join(v['payload']['text'] for v in journal() if v['kind']=='stream' and v['payload'].get('collection')=='stdout')
wait_for(lambda:b'\x1b[?2004h' in transcript,'initial editor')
try:
"#;
        let teardown=r#"
 if p.poll() is None:enter('/quit')
 deadline=time.monotonic()+3
 while p.poll() is None and time.monotonic()<deadline:pump()
 assert p.poll()==0,bytes(transcript)
 print('PTY-OK')
finally:
 if p.poll() is None:p.kill();p.wait()
 os.close(master)
"#;
        let script=format!("{setup}{}{teardown}",body.lines().map(|l|format!(" {l}\n")).collect::<String>());
        let output=crate::test_command("python3").args(["-c",&script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(output.status.success(),"PTY fixture failed:\n{}\n{}",String::from_utf8_lossy(&output.stderr),String::from_utf8_lossy(&output.stdout));
    }
    #[test]
    fn e2e_tab_fuzzy_python_namespace_and_modules(){pty_fixture(r#"
code("unusual_variable=41\nimport os")
enter('@print(unsv\t+1)');expect_source('print(unusual_variable+1)')
enter('@print(os.path.bsna\t(\'/tmp/leaf\'))');expect_source("print(os.path.basename('/tmp/leaf'))")
assert '42\n' in stdout() and 'leaf\n' in stdout(),stdout()
"#);}
    #[test]
    fn e2e_tab_shell_executables_and_unicode_spaced_files(){pty_fixture(r#"
path=os.path.join(cwd,'zzuniqueexecute');open(path,'w').write('#!/bin/sh\nprintf "shell-completion-ok\\n"\n');os.chmod(path,0o700)
open(os.path.join(cwd,'unusual λ spaced.txt'),'w').write('file-completion-ok\n')
command('!zznqxc\t')
wait_for(lambda:'shell-completion-ok\n' in stdout(),'PATH executable fuzzy completion')
command('!cat unλsp\t')
wait_for(lambda:'file-completion-ok\n' in stdout(),'quoted file completion')
open(os.path.join(cwd,'python λ spaced.txt'),'w').write('python-file-ok')
enter('@print(open(\'pyλsp\t\').read())');expect_source("print(open('python λ spaced.txt').read())")
assert 'python-file-ok\n' in stdout(),stdout()
"#);}
    #[test]
    fn e2e_tab_slash_commands_and_model_effort_arguments(){pty_fixture(r#"
command('/mdl\t opai/gpt52\t')
code("print('slash-model-ok')")
command('/efrt\t md\t')
code("print('slash-effort-ok')")
command('/model li\t')
code("print('slash-list-ok')")
command('/logout grq\t')
code("print('slash-provider-ok')")
enter('/quit')
wait_for(lambda:p.poll() is not None,'quit saves editor history')
history=open(os.path.join(home,'editor-history')).read()
assert '/model openai/gpt-5.2' in history,history
assert '/effort medium' in history,history
assert '/model list' in history,history
assert '/logout groq' in history,history
"#);}
    #[test]
    fn e2e_tab_never_invokes_python_properties_or_dir(){pty_fixture(r#"
source="class Trap:\n @property\n def explosive(self):\n  open('BAD-property','w').write('bad')\n  return 4\n def __dir__(self):\n  open('BAD-dir','w').write('bad')\n  return ['explosive']\n def __getattr__(self,name):\n  open('BAD-getattr','w').write('bad')\n  return 4\ntrap=Trap()"
code(source)
enter("@print('safe-completion-ok') # trap.exp\t")
expect_source("print('safe-completion-ok') # trap.explosive")
assert not any(n.startswith('BAD-') for n in os.listdir(cwd)),os.listdir(cwd)
# A user-defined __dict__ property must not run during instance inspection.
code("class DictTrap:\n @property\n def __dict__(self):\n  open('BAD-dict','w').write('bad')\n  return {}\n @property\n def explosive(self):\n  return 9\ndicttrap=DictTrap()")
enter("@print('dict-safe') # dicttrap.exp\t");expect_source("print('dict-safe') # dicttrap.explosive")
assert not any(n.startswith('BAD-') for n in os.listdir(cwd)),os.listdir(cwd)
# Explicit execution, not completion, may of course read the property.
code('print(trap.explosive)');assert os.path.exists(os.path.join(cwd,'BAD-property'))
"#);}
    #[test]
    fn e2e_multiline_continuation_and_bracketed_paste(){pty_fixture(r#"
# First Enter must continue the same ordinary cell, not execute a syntax error.
enter('@answer=(40+');time.sleep(.1);pump();assert not any(v['kind']=='code' for v in journal()),journal()
enter('2)');expect_source('answer=(40+\n2)')
source="values=[]\nfor i in range(3):\n values.append(i)\nprint(answer, values)"
code(source);assert '42 [0, 1, 2]\n' in stdout(),stdout()
enter('@text="""first');time.sleep(.1);pump();enter('second"""');expect_source('text="""first\nsecond"""')
code('print(repr(text))');assert "'first\\nsecond'" in stdout(),stdout()
enter('@for x in range(2):');time.sleep(.08);pump();enter(' print(x)');expect_source('for x in range(2):\n print(x)')
enter('@mapping={');time.sleep(.08);pump();enter(" 'key': 42}");expect_source("mapping={\n 'key': 42}")
code('print(mapping)');assert "{'key': 42}" in stdout(),stdout()
"#);}
    #[test]
    fn e2e_tab_quoted_shell_paths_and_directory_continuation(){pty_fixture(r#"
name="zz O'Brien λ.txt";open(os.path.join(cwd,name),'w').write('apostrophe-file-ok\n')
command("!cat 'zzOBλ\t'");assert 'apostrophe-file-ok\n' in stdout(),stdout()
os.mkdir(os.path.join(cwd,'zz spaced directory'));open(os.path.join(cwd,'zz spaced directory','unusual leaf.txt'),'w').write('nested-file-ok\n')
command('!cat zzspdir\tunslf\t');assert 'nested-file-ok\n' in stdout(),stdout()
name='zz $HOME `echo unsafe`.txt';open(os.path.join(cwd,name),'w').write('quoted-literal-file-ok\n')
command('!cat "zzHOMEunsafe\t"');assert 'quoted-literal-file-ok\n' in stdout(),stdout()
command('!echo pipe-start | "prif\t" "pipeline-completion-ok\\n"');assert 'pipeline-completion-ok\n' in stdout(),stdout()
command('!uns\t COMPLETION_UNUSED');assert any(v['kind']=='user' and v['payload'].get('text')=='unset COMPLETION_UNUSED' for v in journal()),journal()
"#);}
    #[test]
    fn e2e_tab_absolute_tilde_python_cwd_and_visibility(){pty_fixture(r#"
name='zz absolute λ leaf.txt';open(os.path.join(cwd,name),'w').write('absolute-file-ok\n')
command('!!cat '+cwd+'/zzabλlf\t');assert 'absolute-file-ok\n' in stdout(),stdout()
# The worker may chdir without changing the host/shell working directory.
os.mkdir(os.path.join(cwd,'pycwd'));open(os.path.join(cwd,'pycwd','zz python λ leaf.txt'),'w').write('python-cwd-ok')
code("import os\nos.chdir('pycwd')")
enter("@@print(open('zzpyλlf\t').read())");expect_source("print(open('zz python λ leaf.txt').read())")
assert 'python-cwd-ok\n' in stdout(),stdout()
visible=[v['payload']['text'] for v in journal() if v['kind']=='user' and v['payload'].get('visible')]
assert any('absolute-file-ok' in s for s in visible) and any('python-cwd-ok' in s for s in visible),visible
# Tilde search expands for shell but preserves ordinary Python/shell execution.
# HOME is isolated to cwd by this test's launch configuration.
open(os.path.join(cwd,'zz home λ leaf.txt'),'w').write('home-file-ok\n')
command('!cat ~/zzhmλlf\t');assert 'home-file-ok\n' in stdout(),stdout()
"#);}
}

#[cfg(test)]
mod effort_e2e {
    use super::*;
    fn wire(cases:Value,checks:&str){
        let bin=std::env::var("PY_HARNESS_BIN").expect("actual CLI required");
        let script=r#"
import os,sys,tempfile,json,subprocess,threading,http.server,glob
cases=json.loads(sys.argv[2]); checks=sys.argv[3]; home=tempfile.mkdtemp(prefix='py-effort-')
seen=[]
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  seen.append((self.path,dict(self.headers),body))
  if self.path.endswith('/responses'):out={'output':[{'type':'message','content':[{'type':'output_text','text':'fixture-ok'}]}]}
  elif self.path.endswith('/messages'):out={'content':[{'type':'thinking','thinking':'private'},{'type':'text','text':'fixture-ok'}]}
  elif ':generateContent' in self.path:out={'candidates':[{'content':{'parts':[{'thought':True,'text':'private'},{'text':'fixture-ok'}]}}]}
  else:out={'choices':[{'message':{'content':'fixture-ok'}}]}
  out['usage']={'input_tokens':1,'output_tokens':1}; data=json.dumps(out).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
providers={}
for c in cases:
 p,id=c['model'].split('/',1)
 cfg=providers.setdefault(p,{'base_url':'http://127.0.0.1:'+str(server.server_port),'models':[]})
 meta=dict(c.get('metadata',{}),id=id,api=c['api'])
 if not any(m['id']==id for m in cfg['models']):cfg['models'].append(meta)
with open(home+'/config.json','w') as f:json.dump({'effort':'medium','providers':providers},f)
commands=[]
for n,c in enumerate(cases):
 opts=dict(c.get('options',{}),model=c['model'])
 if 'effort' in c:opts['effort']=c['effort']
 args=', '.join(k+'='+repr(v) for k,v in opts.items())
 src="assert agent.llm('question', "+args+")== 'fixture-ok'"
 if c.get('invalid'):src="agent.llm('question', "+args+")"
 commands.append({'id':str(n),'kind':'python','source':src})
env=dict(os.environ,PY_HOME=home,PY_MODEL='openai/gpt-4.1')
run=subprocess.run([sys.argv[1],'--json','--json-input','--no-model'],input=''.join(json.dumps(c)+'\n' for c in commands),text=True,capture_output=True,env=env,timeout=15)
server.shutdown();server.server_close()
assert run.returncode==0,(run.returncode,run.stdout,run.stderr)
events=[json.loads(s) for s in run.stdout.splitlines()]
done={v.get('command_id'):v for v in events if v['kind']=='completed'}
for n,c in enumerate(cases):assert done[str(n)]['status']==('error' if c.get('invalid') else 'ok'),(c,done)
with open(glob.glob(home+'/sessions/*.jsonl')[0]) as f:journal=[json.loads(s) for s in f]
assert len(seen)==sum(not c.get('invalid') for c in cases),(cases,seen)
exec(checks)
"#;
        let output=crate::test_command("python3").args(["-c",script,&bin,&cases.to_string(),checks]).output().unwrap();
        assert!(output.status.success(),"{}\n{}",String::from_utf8_lossy(&output.stdout),String::from_utf8_lossy(&output.stderr));
    }
    fn case(model:&str,api:&str,effort:&str,meta:Value)->Value{
        json!({"model":model,"api":api,"effort":effort,"metadata":meta})
    }
    #[test]
    fn e2e_effort_responses_reasoning_metadata_and_clamping(){wire(json!([
        case("openai/gpt-5.2","openai-responses","xhigh",json!({"reasoning":true})),
        case("openai/gpt-5","openai-responses","xhigh",json!({"reasoning":true})),
        case("openai/gpt-5.2","openai-responses","off",json!({"reasoning":true})),
        case("openai/gpt-4.1","openai-responses","high",json!({"reasoning":false}))
    ]),r#"
assert seen[0][2]['reasoning']=={'effort':'xhigh','summary':'auto'},seen
assert seen[0][2]['include']==['reasoning.encrypted_content']
assert seen[1][2]['reasoning']['effort']=='high'
assert all('reasoning' not in r[2] for r in seen[2:]),seen
assert all(r[2]['max_output_tokens']==2048 and r[2]['store']==False and 'tools' not in r[2] for r in seen)
"#);}
    #[test]
    fn e2e_effort_chat_compatibility_and_binary_thinking(){wire(json!([
        case("cerebras/gpt-oss-120b","openai-completions","high",json!({"reasoning":true})),
        case("zai/glm-4.7","openai-completions","low",json!({"reasoning":true})),
        case("zai/glm-4.7","openai-completions","off",json!({"reasoning":true})),
        case("custom-qwen/fixture","openai-completions","medium",json!({"reasoning":true,"compat":{"thinkingFormat":"qwen"}})),
        case("custom-qwen/fixture","openai-completions","off",json!({"reasoning":true,"compat":{"thinkingFormat":"qwen"}})),
        case("custom-style/fixture","openai-completions","low",json!({"reasoning":true,"compat":{"supportsReasoningEffort":true}}))
    ]),r#"
assert seen[0][2]['reasoning_effort']=='high',seen
assert seen[1][2]['thinking']=={'type':'enabled'} and seen[2][2]['thinking']=={'type':'disabled'}
assert seen[3][2]['enable_thinking']==True and seen[4][2]['enable_thinking']==False
assert seen[5][2]['reasoning_effort']=='low'
"#);}
    #[test]
    fn e2e_effort_unknown_and_nonreasoning_omit_wire_fields(){wire(json!([
        case("unknown/fixture","openai-completions","high",json!({"reasoning":true})),
        case("disabled/fixture","openai-completions","high",json!({"reasoning":true,"compat":{"supportsReasoningEffort":false}})),
        case("xai/grok-4","openai-completions","high",json!({"reasoning":true})),
        case("openai/gpt-4o","openai-completions","high",json!({"reasoning":false})),
        case("no-metadata/fixture","openai-responses","high",json!({}))
    ]),r#"
for path,headers,body in seen:
 assert not any(k in body for k in ('reasoning_effort','reasoning','thinking','enable_thinking')),body
"#);}
    #[test]
    fn e2e_effort_anthropic_adaptive_and_budget_reservation(){wire(json!([
        case("anthropic/claude-opus-4-6","anthropic-messages","xhigh",json!({"reasoning":true,"max_tokens":128000})),
        case("anthropic/claude-opus-4-6","anthropic-messages","minimal",json!({"reasoning":true,"max_tokens":128000})),
        case("anthropic/claude-opus-4-6","anthropic-messages","off",json!({"reasoning":true,"max_tokens":128000})),
        case("anthropic/claude-sonnet-4-6","anthropic-messages","high",json!({"reasoning":true,"max_tokens":64000})),
        case("budget-fixture/small","anthropic-messages","high",json!({"reasoning":true,"max_tokens":4096})),
        case("plain/fixture","anthropic-messages","high",json!({"reasoning":false}))
    ]),r#"
assert seen[0][2]['thinking']=={'type':'adaptive'} and seen[0][2]['output_config']=={'effort':'max'},seen
assert seen[1][2]['output_config']=={'effort':'low'}
assert 'thinking' not in seen[2][2] and 'output_config' not in seen[2][2]
assert seen[3][2]['thinking']=={'type':'enabled','budget_tokens':16384},seen[3]
assert seen[3][2]['max_tokens']==18432
assert seen[4][2]['max_tokens']==4096 and seen[4][2]['thinking']['budget_tokens']==3072
assert 'thinking' not in seen[5][2]
assert all(1024<=r[2]['thinking']['budget_tokens']<=r[2]['max_tokens']-1024 for r in seen[3:5])
assert 'interleaved-thinking-2025-05-14' in {k.lower():v for k,v in seen[3][1].items()}['anthropic-beta']
"#);}
    #[test]
    fn e2e_effort_google_budget_levels_and_off(){wire(json!([
        case("google/gemini-2.5-flash","google-generative-ai","high",json!({"reasoning":true})),
        case("google/gemini-2.5-pro","google-generative-ai","minimal",json!({"reasoning":true})),
        case("google/gemini-3-pro-preview","google-generative-ai","medium",json!({"reasoning":true})),
        case("google/gemini-3-flash-preview","google-generative-ai","medium",json!({"reasoning":true})),
        case("google/gemini-2.5-flash","google-generative-ai","off",json!({"reasoning":true})),
        case("google/gemini-2.5-pro","google-generative-ai","off",json!({"reasoning":true})),
        case("plain-google/fixture","google-generative-ai","high",json!({"reasoning":false}))
    ]),r#"
configs=[r[2]['generationConfig'] for r in seen]
assert configs[0]['thinkingConfig']=={'includeThoughts':True,'thinkingBudget':24576},configs
assert configs[1]['thinkingConfig']['thinkingBudget']==128
assert configs[2]['thinkingConfig']['thinkingLevel']=='HIGH'
assert configs[3]['thinkingConfig']['thinkingLevel']=='MEDIUM'
assert configs[4]['thinkingConfig']=={'includeThoughts':False,'thinkingBudget':0}
assert 'thinkingConfig' not in configs[5] and 'thinkingConfig' not in configs[6]
assert all(c['maxOutputTokens']==2048 for c in configs)
"#);}
    #[test]
    fn e2e_effort_override_is_call_local_and_invalid_never_sends(){wire(json!([
        case("openai/gpt-5.2","openai-responses","low",json!({"reasoning":true})),
        {"model":"openai/gpt-5.2","api":"openai-responses","metadata":{"reasoning":true}},
        {"model":"openai/gpt-5.2","api":"openai-responses","effort":"nonsense","metadata":{"reasoning":true},"invalid":true},
        {"model":"budget-too-small/fixture","api":"anthropic-messages","effort":"high","metadata":{"reasoning":true,"max_tokens":1100},"invalid":true}
    ]),r#"
assert seen[0][2]['reasoning']['effort']=='low' and seen[1][2]['reasoning']['effort']=='medium',seen
requests=[v['payload'] for v in journal if v['kind']=='request']
assert [v['effort'] for v in requests[:2]]==['low','medium'],requests
"#);}
    #[test]
    fn e2e_effort_invalid_options_restore_default_and_caps_stay_valid(){wire(json!([
        {"model":"openai/gpt-5.2","api":"openai-responses","effort":42,"metadata":{"reasoning":true},"invalid":true},
        {"model":"openai/gpt-5.2","api":"openai-responses","effort":"max","metadata":{"reasoning":true},"invalid":true},
        {"model":"openai/gpt-5.2","api":"openai-responses","effort":"low","metadata":{"reasoning":true},"options":{"max_tokens":0},"invalid":true},
        {"model":"openai/gpt-5.2","api":"openai-responses","metadata":{"reasoning":true}},
        case("anthropic/claude-opus-4-6","anthropic-messages","max",json!({"reasoning":true})),
        case("minimum-budget/fixture","anthropic-messages","high",json!({"reasoning":true,"max_tokens":2048}))
    ]),r#"
assert seen[0][2]['reasoning']['effort']=='medium',seen
assert seen[1][2]['output_config']=={'effort':'max'}
assert seen[2][2]['max_tokens']==2048 and seen[2][2]['thinking']['budget_tokens']==1024
requests=[v['payload'] for v in journal if v['kind']=='request']
assert [v['effort'] for v in requests]==['medium','max','high'],requests
"#);}
    #[test]
    fn e2e_effort_image_transform_stays_intact(){
        let mut png=std::io::Cursor::new(Vec::new());
        image::DynamicImage::new_rgb8(2,2).write_to(&mut png,image::ImageFormat::Png).unwrap();
        let data=B64.encode(png.into_inner());
        let mut cases=vec![];
        for (model,api) in [("openai/gpt-5.2","openai-responses"),("anthropic/claude-opus-4-6","anthropic-messages"),("google/gemini-2.5-flash","google-generative-ai")]{
            let mut c=case(model,api,"low",json!({"reasoning":true,"max_tokens":64000}));
            c["options"]=json!({"images":[{"base64":data}]});cases.push(c);
        }
        wire(json!(cases),r#"
assert seen[0][2]['input'][1]['content'][1]['type']=='input_image'
assert seen[0][2]['reasoning']['effort']=='low'
assert seen[1][2]['messages'][0]['content'][1]['source']['media_type']=='image/png'
assert seen[1][2]['thinking']['type']=='adaptive'
assert seen[2][2]['contents'][0]['parts'][1]['inlineData']['mimeType']=='image/png'
assert seen[2][2]['generationConfig']['thinkingConfig']['thinkingBudget']==2048
"#);
    }
}

#[cfg(test)]
mod ux_e2e {
    fn fixture(check:&str){
        let script=format!(r#"
import os,sys,tempfile,subprocess,json,glob
home=tempfile.mkdtemp(prefix='py-ux-'); env=dict(os.environ,PY_HOME=home)
for k in ('PY_MODEL','PY_CONTEXT_LIMIT'):env.pop(k,None)
def run(lines,extra=()):
 p=subprocess.run([sys.argv[1],'--no-model',*extra],input='\n'.join(lines)+'\n',text=True,capture_output=True,env=env,timeout=12)
 assert p.returncode==0,(p.stdout,p.stderr)
 return p.stdout+p.stderr
def journals():return [json.loads(l) for f in glob.glob(home+'/sessions/*.jsonl') for l in open(f)]
{check}
"#);
        let out=crate::test_command("python3").args(["-c",&script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(out.status.success(),"{}\n{}",String::from_utf8_lossy(&out.stdout),String::from_utf8_lossy(&out.stderr));
    }
    #[test]
    fn e2e_model_list_fuzzy_selection_and_ambiguity(){fixture(r#"
out=run(['/model list anthropic/haiku','/model anthropic/son46','/model','/model codex/53cod','/model','/model gpt','/model no-such-known-model','/model','/quit'])
assert 'claude-haiku-4-5' in out and 'Models' in out,out
changes=[v['payload']['model'] for v in journals() if v['kind']=='settings_change']
assert changes==['anthropic/claude-sonnet-4-6','openai-codex/gpt-5.3-codex'],(out,changes)
assert 'ambiguous' in out.lower() and 'No model matches' in out,out
"#);}
    #[test]
    fn e2e_effort_validation_command_errors_do_not_exit(){fixture(r#"
out=run(['/effort low','/effort nonsense','/think high','/effort max','/status','/model anthropic/opus46','/thinking max','/model codex/53cod','/status','/wat','@print("still-alive")','/quit'])
changes=[v['payload'] for v in journals() if v['kind']=='settings_change']
assert any(v.get('effort')=='low' for v in changes) and any(v.get('effort')=='high' for v in changes),(out,changes)
assert any(v.get('effort')=='max' and v.get('model')=='anthropic/claude-opus-4-6' for v in changes),(out,changes)
assert 'nonsense' not in [v.get('effort') for v in changes],changes
assert 'still-alive' in out and 'Unknown command' in out and 'unsupported' in out.lower(),out
assert 'openai-codex/gpt-5.3-codex' in out and 'medium' in out,out
"#);}
    #[test]
    fn e2e_login_inventory_and_unknown_provider_are_nonblocking(){fixture(r#"
out=run(['/login','/login nonexistent-provider','/logout','/auth','/quit'])
assert 'device' in out and 'codex' in out and 'API key' in out,out
assert 'Unknown provider' in out and 'No stored credentials' in out,out
assert not os.path.exists(home+'/auth.json'),out
history=open(home+'/editor-history').read()
assert '/login' not in history,history
"#);}
    #[test]
    fn e2e_session_model_effort_resume_and_new_never_replay(){fixture(r#"
run(['/model anthropic/son46','/effort high','@marker=42; print("only-once")','/quit'])
session=glob.glob(home+'/sessions/*.jsonl')[0]
out=run(['/model','/effort','@print("marker" in globals())','/new','@print(len(H.stdout))','/resume '+session,'@print("marker" in globals())','/quit'],['--session',session])
assert 'anthropic/claude-sonnet-4-6' in out and 'high' in out,out
sources=[v['payload']['source'] for v in journals() if v['kind']=='code']
assert sources.count('marker=42; print("only-once")')==1,sources
assert len(glob.glob(home+'/sessions/*.jsonl'))==2,out
streams=''.join(v['payload'].get('text','') for v in journals() if v['kind']=='stream' and v['payload']['collection']=='stdout')
assert streams.count('only-once\n')==1 and streams.count('False\n')==2 and '0\n' in streams,streams
"#);}
    #[test]
    fn e2e_help_cell_shell_status_and_word_wrapped_errors(){fixture(r#"
env['COLUMNS']='35'
out=run(['/help','!printf "shell-ok\\n"','/no-such-command-with-long-arguments foo bar baz quux','@print("recovered")','/quit'])
assert 'status: ok' in out and '── stdout' in out and 'shell-ok' in out,out
assert 'Tab' in out and '/login' in out and '/new' in out,out
assert all(len(l)<=35 for l in out.splitlines()),out
"#);}
}

// No getpass subprocess or echoed stdin fallback: secrets belong only to the
// foreground controlling terminal. The guard restores terminal state on errors
// and unwinding; cancellation also discards any unfinished canonical line.
struct SecretTerminal {
    tty:File,
    original:libc::termios,
    active:bool,
}
impl SecretTerminal {
    fn restore(&mut self,discard:bool)->io::Result<()> {
        if !self.active{return Ok(());}
        if discard{unsafe{libc::tcflush(self.tty.as_raw_fd(),libc::TCIFLUSH);}}
        if unsafe{libc::tcsetattr(self.tty.as_raw_fd(),libc::TCSANOW,&self.original)}!=0 {
            return Err(io::Error::last_os_error());
        }
        self.active=false;Ok(())
    }
}
impl Drop for SecretTerminal {
    fn drop(&mut self){let _=self.restore(true);}
}
struct SecretBytes(Vec<u8>);
impl Drop for SecretBytes {
    fn drop(&mut self){
        for byte in &mut self.0{unsafe{std::ptr::write_volatile(byte,0);}}
        std::sync::atomic::compiler_fence(std::sync::atomic::Ordering::SeqCst);
    }
}
fn terminal_secret_service(prompt:&str,deadline:Option<std::time::Instant>,mut service:impl FnMut()->Result<()>)->Result<String> {
    let tty=OpenOptions::new().read(true).write(true)
        .custom_flags(libc::O_NOCTTY|libc::O_CLOEXEC|libc::O_NONBLOCK).open("/dev/tty")
        .map_err(|_|"API-key entry requires a controlling terminal; use an environment variable or JSON login for automation")?;
    let fd=tty.as_raw_fd();
    if unsafe{libc::tcgetpgrp(fd)}!=unsafe{libc::getpgrp()} {
        return Err("API-key entry requires the foreground terminal".into());
    }
    let mut original=unsafe{std::mem::zeroed::<libc::termios>()};
    if unsafe{libc::tcgetattr(fd,&mut original)}!=0 {
        return Err("API-key entry requires a terminal with controllable echo".into());
    }
    let mut secret_mode=original;
    secret_mode.c_lflag|=libc::ICANON|libc::ISIG;
    secret_mode.c_lflag&=!(libc::ECHO|libc::ECHONL|libc::ECHOCTL);
    if unsafe{libc::tcsetattr(fd,libc::TCSAFLUSH,&secret_mode)}!=0 {
        return Err("could not disable terminal echo; API key was not read".into());
    }
    let mut guard=SecretTerminal{tty,original,active:true};
    let result=(||->Result<String>{
        guard.tty.write_all(terminal_safe(prompt).as_bytes())?;
        guard.tty.flush()?;
        // Canonical read preserves the terminal's native erase/kill bindings.
        // A partial line returned by Ctrl-D is cancellation, never a stored key.
        let mut bytes=SecretBytes(vec![0;65536]);
        loop {
            service()?;
            if deadline.is_some_and(|d|std::time::Instant::now()>=d){return Err("browser login timed out; retry /login <provider> manual".into());}
            if INTERRUPT.swap(false,std::sync::atomic::Ordering::SeqCst){return Err("login cancelled".into());}
            let mut poll=libc::pollfd{fd,events:libc::POLLIN,revents:0};
            let ready=unsafe{libc::poll(&mut poll,1,100)};
            if ready<0 {
                let error=io::Error::last_os_error();
                if error.kind()==io::ErrorKind::Interrupted{continue;}
                return Err("could not poll terminal for API key".into());
            }
            if ready==0{continue;}
            if poll.revents&(libc::POLLERR|libc::POLLNVAL)!=0 {
                return Err("terminal disconnected during API-key entry".into());
            }
            let count=unsafe{libc::read(fd,bytes.0.as_mut_ptr().cast(),bytes.0.len())};
            if count<0 {
                let error=io::Error::last_os_error();
                if matches!(error.kind(),io::ErrorKind::Interrupted|io::ErrorKind::WouldBlock){continue;}
                return Err("could not read API key from terminal".into());
            }
            if INTERRUPT.swap(false,std::sync::atomic::Ordering::SeqCst){return Err("login cancelled".into());}
            if count==0{return Err("login cancelled".into());}
            let length=count as usize;
            if length==bytes.0.len(){return Err("API key exceeds the input limit".into());}
            if bytes.0[length-1]!=b'\n'{return Err("login cancelled".into());}
            let mut length=length-1;
            if length>0&&bytes.0[length-1]==b'\r'{length-=1;}
            let text=std::str::from_utf8(&bytes.0[..length]).map_err(|_|"API key must be valid UTF-8")?;
            if text.trim().is_empty(){return Err("empty API key; login cancelled".into());}
            return Ok(text.to_owned());
        }
    })();
    guard.restore(result.is_err()).map_err(|_|"could not restore terminal after API-key entry")?;
    guard.tty.write_all(b"\n")?;guard.tty.flush()?;
    result
}

#[cfg(test)]
mod secret_e2e {
    fn pty(check:&str){
        let script=format!(r#"
import os,sys,tempfile,subprocess,json,glob,pty,fcntl,termios,select,time,signal
home=tempfile.mkdtemp(prefix='py-secret-');env=dict(os.environ,PY_HOME=home,TERM='xterm-256color')
for k in ('PY_MODEL','PY_CONTEXT_LIMIT'):env.pop(k,None)
master,slave=pty.openpty();original=termios.tcgetattr(slave);transcript=bytearray()
def session():
 os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
p=subprocess.Popen([sys.argv[1],'--no-model'],stdin=slave,stdout=slave,stderr=slave,env=env,preexec_fn=session)
def read_until(needle):
 deadline=time.monotonic()+5
 while needle not in transcript:
  assert p.poll() is None,(p.returncode,bytes(transcript))
  assert time.monotonic()<deadline,bytes(transcript)
  if select.select([master],[],[],.05)[0]:
   try:transcript.extend(os.read(master,65536))
   except OSError:raise AssertionError(bytes(transcript))
def fresh(needle):
 transcript.clear();read_until(needle)
def send(text):
 value=text.encode() if isinstance(text,str) else text
 # Simulate physical Enter in the raw editor, not Ctrl-J. Canonical hidden
 # prompts still receive their original newline/EOF/editing bytes unchanged.
 if not termios.tcgetattr(slave)[3]&termios.ICANON and value.endswith(b'\n'):value=value[:-1]+b'\r'
 os.write(master,value)
def events():return [json.loads(l) for f in glob.glob(home+'/sessions/*.jsonl') for l in open(f)]
def finish():
 send('/quit\n');p.wait(timeout=5)
 assert p.returncode==0,bytes(transcript)
 assert termios.tcgetattr(slave)==original,('terminal attributes not restored',termios.tcgetattr(slave),original)
try:
 read_until(b'\x1b[?2004h')
 {check}
finally:
 if p.poll() is None:p.kill();p.wait()
 os.close(master);os.close(slave)
"#);
        // Indent only the fixture-specific block, keeping the rest readable.
        let script=script.replace(&format!(" {check}"),&check.lines().map(|l|format!(" {l}")).collect::<Vec<_>>().join("\n"));
        let out=crate::test_command("python3").args(["-c",&script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(out.status.success(),"{}\n{}",String::from_utf8_lossy(&out.stdout),String::from_utf8_lossy(&out.stderr));
    }
    #[test]
    fn e2e_secret_api_key_unicode_hidden_private_and_not_journaled(){pty(r#"send('/login openai\n');fresh(b'API key (hidden): ')
flags=termios.tcgetattr(slave)[3]
assert flags&termios.ICANON and flags&termios.ISIG and not flags&(termios.ECHO|termios.ECHONL),flags
secret='fixture-secret-λ🙂-only-auth'
send(secret+'x\x7f\n');fresh(b'\x1b[?2004h')
assert secret.encode() not in transcript,bytes(transcript)
assert json.load(open(home+'/auth.json'))['openai']['key']==secret
assert os.stat(home+'/auth.json').st_mode&0o777==0o600
send('@print("editor-after-secret")\n');fresh(b'\x1b[?2004h')
assert any(v['kind']=='stream' and v['payload'].get('text')=='editor-after-secret\n' for v in events()),bytes(transcript)
finish()
for f in glob.glob(home+'/sessions/*')+[home+'/editor-history']:
 assert secret not in open(f).read(),f
"#);}
    #[test]
    fn e2e_secret_ctrl_c_and_eof_restore_terminal_without_login(){pty(r#"for action in (b'\x03',b'\x04',b'partial',b'\xff\n',b'\n'):
 send('/login openai\n');fresh(b'API key (hidden): ')
 if action in (b'\x03',b'partial'):send('never-store-cancelled')
 send(b'\x04' if action==b'partial' else action);fresh(b'\x1b[?2004h')
 assert not os.path.exists(home+'/auth.json'),bytes(transcript)
 send('@print("alive-after-cancel")\n');fresh(b'\x1b[?2004h')
 assert any(v['kind']=='stream' and v['payload'].get('text')=='alive-after-cancel\n' for v in events()),bytes(transcript)
finish()
for f in glob.glob(home+'/sessions/*')+[home+'/editor-history']:
 assert 'never-store-cancelled' not in open(f).read(),f
"#);}
    #[test]
    fn e2e_secret_sigint_while_polling_is_cancellable(){pty(r#"send('/login openai\n');fresh(b'API key (hidden): ')
os.kill(p.pid,signal.SIGINT);fresh(b'\x1b[?2004h')
assert not os.path.exists(home+'/auth.json'),bytes(transcript)
send('@print("sticky-interrupt-cleared")\n');fresh(b'\x1b[?2004h')
assert any(v['kind']=='stream' and v['payload'].get('text')=='sticky-interrupt-cleared\n' for v in events()),bytes(transcript)
finish()
"#);}
    #[test]
    fn e2e_secret_no_tty_refuses_instead_of_echo_fallback(){
        let script=r#"
import os,sys,tempfile,subprocess,glob,json
home=tempfile.mkdtemp(prefix='py-secret-no-tty-')
p=subprocess.run([sys.argv[1],'--no-model'],input='/login openai\n@print("still-safe")\n/quit\n',text=True,capture_output=True,
 env=dict(os.environ,PY_HOME=home),start_new_session=True,timeout=5)
assert p.returncode==0,(p.stdout,p.stderr)
assert 'terminal' in p.stderr.lower() and 'still-safe' in p.stdout,(p.stdout,p.stderr)
assert 'getpass' not in p.stderr and 'Password input may be echoed' not in p.stderr,p.stderr
assert not os.path.exists(home+'/auth.json')
"#;
        let out=crate::test_command("python3").args(["-c",script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(out.status.success(),"{}\n{}",String::from_utf8_lossy(&out.stdout),String::from_utf8_lossy(&out.stderr));
    }
}

#[cfg(test)]
mod rpc_e2e {
    fn fixture(check:&str){
        let check=serde_json::to_string(check).unwrap();
        let script=format!(r#"
import os,sys,tempfile,subprocess,json,glob,time,queue,threading,signal
home=tempfile.mkdtemp(prefix='py-rpc-race-')
p=subprocess.Popen([sys.argv[1],'--json','--json-input','--no-model'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=dict(os.environ,PY_HOME=home))
events=queue.Queue()
def reader():
 for line in p.stdout:events.put(json.loads(line))
threading.Thread(target=reader,daemon=True).start()
def send(v):p.stdin.write(json.dumps(v)+'\n');p.stdin.flush()
def until(kind,id=None):
 end=time.monotonic()+6
 while time.monotonic()<end:
  v=events.get(timeout=max(.01,end-time.monotonic()))
  assert v['kind']!='error',v
  if v['kind']==kind and (id is None or v.get('command_id')==id):return v
 raise AssertionError((kind,id,'timed out'))
def code(id,source):send(dict(id=id,kind='python',source=source))
def journal():return [json.loads(l) for f in glob.glob(home+'/sessions/*.jsonl') for l in open(f)]
try:
 until('ready')
 exec({check})
 send(dict(id='quit',kind='quit'));p.stdin.close();p.wait(timeout=5)
 assert p.returncode==0,p.stderr.read()
finally:
 if p.poll() is None:p.kill()
 p.wait()
"#);
        let out=crate::test_command("python3").args(["-c",&script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(out.status.success(),"{}\n{}",String::from_utf8_lossy(&out.stdout),String::from_utf8_lossy(&out.stderr));
    }
    #[test]
    fn e2e_rpc_input_reply_sigint_window_and_repeated_interrupts(){fixture(r#"
for repeats in (1,3):
 marker=home+'/recv-window-'+str(repeats)
 source='''import time
bridge=agent.sh.__globals__
_original_recv=bridge['_recv']
def receive_window():
    bridge['_recv']=_original_recv
    open(MARKER,'w').close()
    time.sleep(1)
    return _original_recv()
bridge['_recv']=receive_window
try:
    input('race?')
except KeyboardInterrupt:
    for operation in (lambda: H.stdout[0], lambda: agent.say('must-not-dispatch')):
        try: print('BAD-RETURN',repr(operation()))
        except KeyboardInterrupt: print('helper-cancelled')
    print('caught-input-cancel')
'''.replace('MARKER',repr(marker))
 operation='race-'+str(repeats);code(operation,source)
 prompt=until('input_prompt')
 end=time.monotonic()+3
 while not os.path.exists(marker):
  assert time.monotonic()<end
  time.sleep(.005)
 reply=dict(id='reply-'+str(repeats),kind='stdin_reply',prompt_id=prompt['prompt_id'],operation=prompt['operation'],worker_generation=prompt['worker_generation'],value='OLD-INPUT-REPLY')
 send(reply);until('completed',reply['id'])
 for _ in range(repeats):os.kill(p.pid,signal.SIGINT);time.sleep(.06)
 assert until('completed',operation)['status']=='cancelled'
 next_id='next-'+str(repeats)
 code(next_id,"print('next-cell-ok'); print('OLD-INPUT-REPLY' in H.stdout[0])")
 assert until('completed',next_id)['status']=='ok'
streams=''.join(v['payload'].get('text','') for v in journal() if v['kind']=='stream' and v['payload']['collection']=='stdout')
assert streams.count('helper-cancelled\n')==4 and streams.count('caught-input-cancel\n')==2,streams
assert streams.count('next-cell-ok\nFalse\n')==2 and 'BAD-RETURN' not in streams and 'OLD-INPUT-REPLY' not in streams,streams
assert not any(v['kind']=='say' and v['payload']['text']=='must-not-dispatch' for v in journal())
"#);}
    #[test]
    fn e2e_rpc_stale_reply_is_not_a_helper_result_or_command(){fixture(r#"
code('seed',"print('seed-data')");assert until('completed','seed')['status']=='ok'
code('stale',"""bridge=agent.sh.__globals__
_original_recv=bridge['_recv']
def stale_once():
    bridge['_recv']=_original_recv
    return {'kind':'rpc_reply','rpc_id':0,'ok':True,'value':'stale-poison'}
bridge['_recv']=stale_once
assert H.stdout[0]=='seed-data\\n'
print('stale-discarded')
""")
assert until('completed','stale')['status']=='ok'
code('next',"print('after-stale'); assert H.stdout[1]=='stale-discarded\\n'")
assert until('completed','next')['status']=='ok'
"#);}
    #[test]
    fn e2e_worker_pre_started_failure_captures_partial_and_resets(){fixture(r#"
code('setup',"""import os
bridge=agent.sh.__globals__
_original_send=bridge['_send']
def sabotage_started(message):
    if message.get('kind')=='started':
        os.write(1,b'pre-start-prefix\\n')
        os.write(2,b'pre-start-error\\n')
        os._exit(23)
    return _original_send(message)
bridge['_send']=sabotage_started
print('seed-output')
""")
assert until('completed','setup')['status']=='ok'
code('pre-start',"print('never-ran')")
result=until('completed','pre-start')
assert result['status']=='worker_crashed' and result['exit_code']==23,result
assert result['stdout']['chars']==17 and result['stderr']['chars']==16,result
assert result['stdout']['complete'] is False and result['stderr']['complete'] is False,result
code('recovered',"print('recovered-fresh'); assert 'bridge' not in globals(); assert H.stdout[1]=='pre-start-prefix\\n'")
assert until('completed','recovered')['status']=='ok'
j=journal();assert len([v for v in j if v['kind']=='worker_reset'])==1,j
assert len([v for v in j if v['kind']=='code' and v['payload']['source']=="print('never-ran')"])==1
"#);}
    #[test]
    fn e2e_worker_dead_before_send_does_not_recapture_previous_output(){fixture(r#"
marker=home+'/worker-exited'
source='''import os
bridge=agent.sh.__globals__
def exit_before_next_command():
    open(MARKER,'w').close()
    os._exit(17)
bridge['_recv']=exit_before_next_command
print('seed-not-recaptured')
'''.replace('MARKER',repr(marker))
code('setup-dead',source);assert until('completed','setup-dead')['status']=='ok'
end=time.monotonic()+3
while not os.path.exists(marker):
 assert time.monotonic()<end
 time.sleep(.005)
time.sleep(.05)
code('dead-send',"print('never-ran-dead')")
result=until('completed','dead-send')
assert result['status']=='worker_crashed' and result['exit_code']==17,result
assert result['stdout']['chars']==0 and result['stderr']['chars']==0,result
assert result['stdout']['complete'] is False and result['stderr']['complete'] is False,result
code('fresh-after-dead',"print('fresh-after-dead'); assert H.stdout[0]=='seed-not-recaptured\\n'; assert H.stdout[1]==''")
assert until('completed','fresh-after-dead')['status']=='ok'
assert len([v for v in journal() if v['kind']=='worker_reset'])==1
"#);}
    #[test]
    fn e2e_rpc_stale_reply_is_discarded_between_execution_commands(){fixture(r#"
code('setup-obsolete',"""bridge=agent.sh.__globals__
_original_recv=bridge['_recv']
def obsolete_command_frame():
    bridge['_recv']=_original_recv
    return {'kind':'rpc_reply','rpc_id':0,'ok':True,'value':'old-command-poison'}
bridge['_recv']=obsolete_command_frame
print('ready-for-next-command')
""")
assert until('completed','setup-obsolete')['status']=='ok'
code('next-after-obsolete',"print('command-not-poisoned'); assert H.stdout[0]=='ready-for-next-command\\n'")
assert until('completed','next-after-obsolete')['status']=='ok'
assert not any(v['kind']=='worker_reset' for v in journal())
"#);}
}

#[cfg(test)]
mod responsiveness_e2e {
    use super::*;
    #[test]
    fn prepared_table_costs_match_rendered_wrap(){
        let mut examples=vec!["".into(),"alpha beta gamma".into(),"  indent   with  spaces".into(),
            "abcdefghi".into(),"界a界b界".into(),"a\u{200d}b\u{200d}c\u{200d}d".into(),
            "👩\u{200d}👩\u{200d}👧\u{200d}👦 👩🏽\u{200d}💻".into(),
            "e\u{301}  界  界\nsecond\n".into(),"\u{301}界".into(),"a\x1b[31m b\tfoo".into()];
        for seed in 0..32{
            let mut text=String::new();
            for i in 0..64{ text.push_str(["a","界","\u{301}"," ","  ","\n","x\u{200d}","🏽","1\u{fe0f}\u{20e3}"][(seed*7+i*13)%9]); }
            examples.push(text);
        }
        for text in examples{
            let costs=terminal_cell_costs(&text,80);
            for (width,cost) in costs.iter().enumerate().take(81).skip(1){
                let wrapped=terminal_wrap(&text,width);
                assert_eq!(*cost,(wrapped.0.len(),wrapped.1),"width {width}, text {text:?}");
            }
        }
    }
    fn check(body:&str){
        let script=format!(r#"
import os,sys,tempfile,subprocess,json,glob,time
home=tempfile.mkdtemp(prefix='py-render-responsive-')
env=dict(os.environ,PY_HOME=home)
for key in ('PY_MODEL','PY_CONTEXT_LIMIT'):env.pop(key,None)
def run(source,width=512,deadline=3):
 env['COLUMNS']=str(width)
 commands=[{{'id':'table','kind':'python','source':source}},{{'id':'next','kind':'python','source':"print('after-render-ok')"}}]
 began=time.monotonic()
 p=subprocess.run([sys.argv[1],'--json-input','--no-model'],input=''.join(json.dumps(c)+'\n' for c in commands),text=True,capture_output=True,env=env,timeout=deadline)
 elapsed=time.monotonic()-began
 assert p.returncode==0,(p.stdout[-2000:],p.stderr)
 assert elapsed<deadline,elapsed
 events=[json.loads(l) for f in glob.glob(home+'/sessions/*.jsonl') for l in open(f)]
 assert any(v['kind']=='completion' and v['payload']['command_id']=='next' and v['payload']['status']=='ok' for v in events),(p.stdout[-2000:],events[-10:])
 assert any(v['kind']=='stream' and v['payload'].get('text')=='after-render-ok\n' for v in events),events[-10:]
 return p.stdout,events,elapsed
{body}
"#);
        let out=crate::test_command("python3").args(["-c",&script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(out.status.success(),"{}\n{}",String::from_utf8_lossy(&out.stdout),String::from_utf8_lossy(&out.stderr));
        print!("{}",String::from_utf8_lossy(&out.stdout));
    }
    #[test]
    fn e2e_large_repeated_table_is_responsive_and_history_exact(){check(r#"
source="table='| a | b | c | d |\\n| --- | --- | --- | --- |\\n'+('| '+('x'*512+' | ')*4+'\\n')*128\nagent.say(table)"
out,events,elapsed=run(source)
say=[v['payload']['text'] for v in events if v['kind']=='say']
expected='| a | b | c | d |\n| --- | --- | --- | --- |\n'+('| '+('x'*512+' | ')*4+'\n')*128
assert say==[expected],(len(say),len(say[0]) if say else 0)
assert out.count('├')==128 and out.count('└')==1,out[-2000:]
print('repeated-table-seconds',elapsed)
"#);}
    #[test]
    fn e2e_large_unique_table_is_responsive_and_history_exact(){check(r#"
source="table='| a | b | c | d |\\n| --- | --- | --- | --- |\\n'+'\\n'.join('| '+' | '.join((str(r)+':'+str(c)+' '+('word '+str(r)+' ')*90)[:512] for c in range(4))+' |' for r in range(128))\nagent.say(table)"
out,events,elapsed=run(source)
say=[v['payload']['text'] for v in events if v['kind']=='say']
expected='| a | b | c | d |\n| --- | --- | --- | --- |\n'+'\n'.join('| '+' | '.join((str(r)+':'+str(c)+' '+('word '+str(r)+' ')*90)[:512] for c in range(4))+' |' for r in range(128))
assert say==[expected],(len(say),len(say[0]) if say else 0)
assert out.count('├')==128 and out.count('└')==1,out[-2000:]
print('unique-table-seconds',elapsed)
"#);}
    #[test]
    fn e2e_large_mixed_cluster_table_is_responsive(){check(r#"
source="table='| a | b | c | d |\\n| --- | --- | --- | --- |\\n'+'\\n'.join('| '+' | '.join((str(r)+':'+str(c)+' '+('界x'*256))[:512] for c in range(4))+' |' for r in range(128))\nagent.say(table)"
out,events,elapsed=run(source)
assert out.count('├')==128 and out.count('└')==1,out[-2000:]
assert all(sum(2 if '一'<=c<='鿿' else (0 if c=='\u200d' else 1) for c in line)<=512 for line in out.splitlines()),out[-2000:]
print('mixed-cluster-table-seconds',elapsed)
"#);}
    #[test]
    fn e2e_invalid_zwj_ascii_wraps_but_emoji_joins_stay_together(){check(r#"
out,events,elapsed=run("agent.say('a\\u200db\\u200dc\\u200dd\\u200de')",3)
# Semantic cell/state headers now precede say; isolate its actual joined text,
# not the first transcript paragraph. The source preview has escaped ZWJs.
content='\n'.join(line for line in out.splitlines() if '\u200d' in line)
assert content.splitlines()==['a\u200db\u200dc\u200d','d\u200de'],repr(content)
say=[v['payload']['text'] for v in events if v['kind']=='say']
assert say==['a\u200db\u200dc\u200dd\u200de'],say
# Family/profession emoji clusters remain two columns, not split across lines.
out,events,elapsed=run("agent.say('👩\\u200d👩\\u200d👧\\u200d👦 👩🏽\\u200d💻')",4)
content='\n'.join(line for line in out.splitlines() if '\u200d' in line)
assert content.splitlines()==['👩\u200d👩\u200d👧\u200d👦','👩🏽\u200d💻'],repr(content)
"#);}
}

#[cfg(test)]
mod boundary_e2e{
    fn check(script:&str){
        let out=crate::test_command("python3").args(["-c",script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(out.status.success(),"{}\n{}",String::from_utf8_lossy(&out.stdout),String::from_utf8_lossy(&out.stderr));
    }
    #[test]
    fn e2e_idle_sigint_never_cancels_next_cell(){check(r#"
import os,sys,tempfile,pty,fcntl,termios,subprocess,select,time,signal,json,glob
m,s=pty.openpty(); home=tempfile.mkdtemp(prefix='py-idle-')
def tty():os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
p=subprocess.Popen([sys.argv[1],'--no-model'],stdin=s,stdout=s,stderr=s,preexec_fn=tty,env=dict(os.environ,PY_HOME=home,TERM='xterm'))
os.close(s);data=bytearray()
def ready(start):
 end=time.monotonic()+5
 while b'\x1b[?2004h' not in data[start:]:
  assert time.monotonic()<end,data
  if select.select([m],[],[],.05)[0]:data.extend(os.read(m,65536))
try:
 ready(0);start=len(data);os.kill(p.pid,signal.SIGINT);ready(start)
 start=len(data);os.write(m,b'@print("idle-external-ok")\r');ready(start)
 start=len(data);os.write(m,b'\x03');ready(start)
 start=len(data);os.write(m,b'@print("idle-key-ok")\r');ready(start)
 os.write(m,b'/quit\r');p.wait(timeout=3)
 j=[json.loads(l) for f in glob.glob(home+'/sessions/*.jsonl') for l in open(f)]
 text=''.join(v['payload'].get('text','') for v in j if v['kind']=='stream' and v['payload']['collection']=='stdout')
 assert text=='idle-external-ok\nidle-key-ok\n',(text,data)
 assert not any(v['kind']=='cancel' for v in j),j
finally:
 if p.poll() is None:p.kill();p.wait()
 os.close(m)
"#);}
    #[test]
    fn e2e_json_command_errors_are_correlated_without_auth_leaks(){check(r#"
import os,sys,json,tempfile,subprocess,glob
home=tempfile.mkdtemp(prefix='py-command-error-')
lines=[{'id':'bad-login','kind':'login','provider':'unknown-provider','key':'fixture-secret'},
 {'id':'bad-python','kind':'python','source':42},
 {'id':'alive','kind':'python','source':'print("error-recovered")'}]
p=subprocess.run([sys.argv[1],'--json','--json-input','--no-model'],input='\n'.join(map(json.dumps,lines))+'\n',text=True,capture_output=True,timeout=6,env=dict(os.environ,PY_HOME=home))
assert p.returncode==0,(p.stdout,p.stderr)
values=[json.loads(l) for l in p.stdout.splitlines()];errors=[v for v in values if v['kind']=='error']
assert [v.get('command_id') for v in errors]==['bad-login','bad-python'],values
assert any(v['kind']=='completed' and v['command_id']=='alive' and v['status']=='ok' for v in values),values
assert 'fixture-secret' not in p.stdout+p.stderr
for path in glob.glob(home+'/sessions/*.jsonl'):assert 'fixture-secret' not in open(path).read()
"#);}
    #[test]
    fn e2e_json_idle_interrupt_preserves_namespace(){check(r#"
import os,sys,signal,json,tempfile,subprocess,threading,queue,time,glob
home=tempfile.mkdtemp(prefix='py-json-idle-');q=queue.Queue()
p=subprocess.Popen([sys.argv[1],'--json','--json-input','--no-model'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=dict(os.environ,PY_HOME=home))
threading.Thread(target=lambda:[q.put(json.loads(l)) for l in p.stdout],daemon=True).start()
def until(kind,id=None):
 end=time.monotonic()+5
 while True:
  v=q.get(timeout=max(.01,end-time.monotonic()))
  if v['kind']==kind and (id is None or v.get('command_id')==id):return v
  if v['kind'] in ('error','rejected'):raise AssertionError(v)
def send(**v):p.stdin.write(json.dumps(v)+'\n');p.stdin.flush()
try:
 until('ready');send(id='seed',kind='python',source='marker=42');assert until('completed','seed')['status']=='ok'
 send(id='idle',kind='interrupt');assert until('completed','idle')['active']==False
 os.kill(p.pid,signal.SIGINT);time.sleep(.06)
 send(id='next',kind='python',source='print(marker)');assert until('completed','next')['status']=='ok'
 send(id='quit',kind='quit');p.wait(timeout=3)
 j=[json.loads(l) for f in glob.glob(home+'/sessions/*.jsonl') for l in open(f)]
 assert not any(v['kind']=='worker_reset' for v in j),j
 assert '42\n' in ''.join(v['payload'].get('text','') for v in j if v['kind']=='stream' and v['payload']['collection']=='stdout'),j
finally:
 if p.poll() is None:p.kill();p.wait()
"#);}
    #[test]
    fn e2e_two_sessions_reload_api_auth_and_logout_before_network(){check(r#"
import sys,os,json,tempfile,subprocess,threading,http.server,queue,time
home=tempfile.mkdtemp(prefix='py-auth-read-');seen=[]
class Server(http.server.BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_POST(self):
  seen.append(self.headers.get('Authorization'));self.rfile.read(int(self.headers.get('Content-Length',0)))
  data=json.dumps({'data':[{'b64_json':'iVBORw0KGgo='}]} if self.path.endswith('/images/generations') else {'choices':[{'message':{'content':'ok'}}]}).encode();self.send_response(200);self.end_headers();self.wfile.write(data)
s=http.server.ThreadingHTTPServer(('127.0.0.1',0),Server);threading.Thread(target=s.serve_forever,daemon=True).start()
base='http://127.0.0.1:'+str(s.server_port)
json.dump({'providers':{'fresh':{'base_url':base,'models':[{'id':'m','api':'openai-completions'}]},'openai':{'base_url':base}}},open(home+'/config.json','w'))
class Client:
 def __init__(self):
  self.p=subprocess.Popen([sys.argv[1],'--json','--json-input','--no-model'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=dict(os.environ,PY_HOME=home));self.q=queue.Queue()
  threading.Thread(target=lambda:[self.q.put(json.loads(l)) for l in self.p.stdout],daemon=True).start();self.until(lambda v:v['kind']=='ready')
 def until(self,predicate):
  end=time.monotonic()+5
  while True:
   v=self.q.get(timeout=max(.01,end-time.monotonic()))
   if predicate(v):return v
 def send(self,**v):self.p.stdin.write(json.dumps(v)+'\n');self.p.stdin.flush()
 def cmd(self,**v):
  self.send(**v);return self.until(lambda e:e.get('command_id')==v['id'] and e['kind'] in ('completed','auth','models','error'))
 def close(self):self.send(id='quit',kind='quit');self.p.wait(timeout=3)
a=Client();b=Client()
try:
 b.cmd(id='key1',kind='login',provider='fresh',key='fixture-one')
 assert a.cmd(id='call1',kind='python',source="agent.llm('x',model='fresh/m')")['status']=='ok'
 b.cmd(id='key2',kind='login',provider='fresh',key='fixture-two')
 assert a.cmd(id='call2',kind='python',source="agent.llm('x',model='fresh/m')")['status']=='ok'
 assert seen==['Bearer fixture-one','Bearer fixture-two'],seen
 b.cmd(id='logout',kind='logout',provider='fresh')
 auth=a.cmd(id='auth',kind='auth')['providers'];assert next(v for v in auth if v['provider']=='fresh')['stored']==False,auth
 # Local fixtures may invoke anonymously after logout, but may not reuse the old key.
 a.cmd(id='call3',kind='python',source="agent.llm('x',model='fresh/m')")
 assert seen[-1]!='Bearer fixture-two',seen
 models=a.cmd(id='models',kind='models')['models']
 local=next(v for v in models if v['id']=='fresh/m');assert not local['credential_backed'] and local['ready'],local
 b.cmd(id='image-key1',kind='login',provider='openai',key='fixture-image-one')
 assert a.cmd(id='image1',kind='python',source="agent.llm.image('x')")['status']=='ok'
 b.cmd(id='image-key2',kind='login',provider='openai',key='fixture-image-two')
 assert a.cmd(id='image2',kind='python',source="agent.llm.image('x')")['status']=='ok'
 b.cmd(id='image-logout',kind='logout',provider='openai')
 assert a.cmd(id='image3',kind='python',source="agent.llm.image('x')")['status']=='ok'
 assert seen[-3:]==['Bearer fixture-image-one','Bearer fixture-image-two','Bearer '],seen
finally:
 a.close();b.close();s.shutdown()
"#);}
    #[test]
    fn e2e_invalid_codex_effort_never_refreshes(){check(r#"
import os,sys,json,tempfile,subprocess,threading,http.server,glob
home=tempfile.mkdtemp(prefix='py-codex-effort-');seen=[]
class Server(http.server.BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_POST(self):
  seen.append(self.path);self.send_response(400);self.end_headers();self.wfile.write(b'{}')
s=http.server.ThreadingHTTPServer(('127.0.0.1',0),Server);threading.Thread(target=s.serve_forever,daemon=True).start()
json.dump({'openai-codex':{'type':'oauth','refresh':'fixture','access':'fixture','expires':0,'accountId':'fixture'}},open(home+'/auth.json','w'))
line=json.dumps({'id':'invalid','kind':'python','source':"agent.llm('x',model='codex/gpt-5.3-codex',effort='max')"})+'\n'
p=subprocess.run([sys.argv[1],'--json','--json-input','--no-model'],input=line,text=True,capture_output=True,timeout=6,env=dict(os.environ,PY_HOME=home,PY_CODEX_AUTH_BASE_URL='http://127.0.0.1:'+str(s.server_port)))
s.shutdown();assert p.returncode==0,(p.stdout,p.stderr)
assert not seen,seen
j=[json.loads(l) for f in glob.glob(home+'/sessions/*.jsonl') for l in open(f)]
assert not any(v['kind']=='request' or (v['kind']=='intent' and v['payload']['type']=='provider') for v in j),j
"#);}
}

#[cfg(test)]
mod flags_e2e{
    fn check(body:&str){
        let script=format!(r#"
import os,sys,tempfile,subprocess,json,glob
root=tempfile.mkdtemp(prefix='py-flags-');home=os.path.join(root,'home')
env=dict(os.environ,PY_HOME=home)
for k in ('PY_MODEL','PY_CONTEXT_LIMIT'):env.pop(k,None)
def run(args,lines='',**kw):
 return subprocess.run([sys.argv[1],*args],input=lines,text=True,capture_output=True,timeout=8,env=env,**kw)
def events(p):
 assert p.returncode==0,(p.stdout,p.stderr)
 return [json.loads(l) for l in p.stdout.splitlines()]
def records(path=None):
 return [json.loads(l) for f in ([path] if path else glob.glob(home+'/sessions/*.jsonl')) for l in open(f)]
{body}
"#);
        let p=crate::test_command("python3").args(["-c",&script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(p.status.success(),"{}\n{}",String::from_utf8_lossy(&p.stdout),String::from_utf8_lossy(&p.stderr));
    }
    #[test]
    fn e2e_help_version_spec_never_create_home(){check(r#"
env['PY_PYTHON']='/no/such/python-executable'
for args in [['--help'],['-h'],['--version'],['-V'],['--spec']]:
 p=run(args);assert p.returncode==0,(args,p.stdout,p.stderr)
 assert not os.path.exists(home),(args,p.stdout,p.stderr)
 if args[0] in ('--help','-h'):
  assert 'Usage:' in p.stdout and '--effort' in p.stdout and '--resume' in p.stdout,p.stdout
 elif args[0] in ('--version','-V'):assert p.stdout.strip().startswith('py 0.1.0'),p.stdout
 else:assert '# Rust Python harness' in p.stdout,p.stdout
"#);}
    #[test]
    fn e2e_bad_flags_and_values_fail_before_session_creation(){check(r#"
for args in [['--wat'],['--model'],['--effort'],['--session'],['--resume'],['--model','--no-model'],['--effort','nonsense'],['--session','absent.jsonl'],['--effort','low','--effort','high'],['--session','a','--resume','b'],['unexpected-prompt'],['--']]:
 p=run(args);assert p.returncode!=0,(args,p.stdout,p.stderr)
 assert not os.path.exists(home),(args,p.stdout,p.stderr)
 assert p.stderr.strip(),(args,p.stdout,p.stderr)
"#);}
    #[test]
    fn e2e_json_startup_fuzzy_model_effort_and_configured_exact(){check(r#"
os.makedirs(home)
json.dump({'model':'openai/gpt-4o','providers':{'unlisted':{'base_url':'http://localhost:1','models':[{'id':'fixture-model','api':'openai-completions','context_limit':64000}]},'openai-codex':{'models':[{'id':'fixture-6.1','api':'openai-codex-responses','context_limit':512000,'reasoning':True}]}}},open(home+'/config.json','w'))
commands=json.dumps({'id':'status','kind':'status'})+'\n'
for model,expected in [('codex/53cod','openai-codex/gpt-5.3-codex'),('unlisted/fixture-model','unlisted/fixture-model'),('codex/fixture61','openai-codex/fixture-6.1')]:
 p=run(['--json','--json-input','--no-model','--model',model,'--effort','low'],commands)
 values=events(p)
 status=next(v for v in values if v['kind']=='status')
 ready=next(v for v in values if v['kind']=='ready')
 assert ready['context_usage']['model_context_limit']=={'codex/53cod':400000,'unlisted/fixture-model':64000,'codex/fixture61':512000}[model],values
 assert status['model']==expected and status['effort']=='low',values
 assert any(v['kind']=='notice' and expected in v['text'] for v in values),values
 assert any(v['kind']=='settings_change' and v['payload']['effort']=='low' for v in records()),records()
"#);}
    #[test]
    fn e2e_resume_alias_overrides_settings_without_replay(){check(r#"
p=run(['--no-model'], '/model anthropic/opus46\n/effort max\n@marker=42; print("run-once")\n/quit\n');assert p.returncode==0,(p.stdout,p.stderr)
session=glob.glob(home+'/sessions/*.jsonl')[0]
commands=json.dumps({'id':'state','kind':'status'})+'\n'+json.dumps({'id':'probe','kind':'python','source':"print('marker' in globals())"})+'\n'
p=run(['--json','--json-input','--no-model','--resume',session,'--model','codex/53cod','--effort','low'],commands)
values=events(p)
streams=[v['payload']['text'] for v in records(session) if v['kind']=='stream' and v['payload']['collection']=='stdout']
assert streams[-1]=='False\n',streams
status=next(v for v in values if v['kind']=='status');assert status['model']=='openai-codex/gpt-5.3-codex' and status['effort']=='low',values
assert len(glob.glob(home+'/sessions/*.jsonl'))==1
source=[v['payload']['source'] for v in records(session) if v['kind']=='code']
assert source.count('marker=42; print("run-once")')==1,source
p=run(['--json','--json-input','--no-model','--session',session],commands.replace('probe','probe2').replace('state','state2'))
values=events(p)
streams=[v['payload']['text'] for v in records(session) if v['kind']=='stream' and v['payload']['collection']=='stdout']
assert streams[-1]=='False\n',streams
status=next(v for v in values if v['kind']=='status');assert status['model']=='openai-codex/gpt-5.3-codex' and status['effort']=='low',values
"#);}
    #[test]
    fn e2e_empty_editor_lines_never_start_a_model_request(){check(r#"
import threading,http.server
seen=[]
class Server(http.server.BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_POST(self):
  seen.append(self.path);self.rfile.read(int(self.headers.get('Content-Length',0)))
  self.send_response(200);self.end_headers();self.wfile.write(json.dumps({'choices':[{'message':{'content':'agent.loop.stop()'}}]}).encode())
s=http.server.ThreadingHTTPServer(('127.0.0.1',0),Server);threading.Thread(target=s.serve_forever,daemon=True).start()
os.makedirs(home);json.dump({'model':'local/m','providers':{'local':{'base_url':'http://127.0.0.1:'+str(s.server_port),'models':[{'id':'m','api':'openai-completions'}]}}},open(home+'/config.json','w'))
p=run([], '\n  \n\t\n@print("ok")\n/quit\n');s.shutdown()
assert p.returncode==0,(p.stdout,p.stderr)
assert not seen,seen
accepted=[v['payload']['command'] for v in records() if v['kind']=='accepted']
assert len(accepted)==1 and accepted[0]['kind']=='python',accepted
assert accepted[0]['source']=='print("ok")',accepted
p=run(['--no-model'],'\n  \n');assert p.returncode==0,(p.stdout,p.stderr)
"#);}
    #[test]
    fn e2e_tab_effort_capabilities_and_unlisted_login_provider(){check(r#"
import pty,fcntl,termios,select,time
os.makedirs(home);json.dump({'providers':{'custom-unlisted':{'base_url':'http://localhost:1'},'openai-codex':{'models':[{'id':'fixture-6.1','api':'openai-codex-responses','context_limit':512000,'reasoning':True}]}}},open(home+'/config.json','w'))
def probe(model,want_max):
 m,s=pty.openpty()
 def tty():os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
 p=subprocess.Popen([sys.argv[1],'--no-model','--model',model],stdin=s,stdout=s,stderr=s,preexec_fn=tty,env=dict(env,TERM='xterm'))
 os.close(s);data=bytearray()
 def ready(start):
  end=time.monotonic()+5
  while b'\x1b[?2004h' not in data[start:]:
   assert time.monotonic()<end,data
   if select.select([m],[],[],.05)[0]:data.extend(os.read(m,65536))
 def drain():
  end=time.monotonic()+.2
  while time.monotonic()<end:
   if select.select([m],[],[],.02)[0]:data.extend(os.read(m,65536))
 try:
  ready(0);start=len(data);os.write(m,b'/effort ma\t');drain()
  if b'\x1b[?25l' in data[start:]:os.write(m,b'\r');drain()
  assert (b'/effort max' in data[start:])==want_max,(model,data[start:])
  os.write(m,b'\x03');ready(start)
  start=len(data);os.write(m,b'/logout cstmted\t\r');ready(start)
  start=len(data);os.write(m,b'/model codex/sol61\t\r');ready(start)
  assert any(v['kind']=='settings_change' and v['payload']['model']=='openai-codex/gpt-6.1-sol' for v in records()),records()
  start=len(data);os.write(m,b'/model codex/fixture61\t\r');ready(start)
  os.write(m,b'/quit\r');p.wait(timeout=3)
  history=open(home+'/editor-history').read()
  assert '/logout custom-unlisted' in history and '/model codex/fixture-6.1' in history,(history,data)
  assert any(v['kind']=='settings_change' and v['payload']['model']=='openai-codex/fixture-6.1' for v in records()),records()
 finally:
  if p.poll() is None:p.kill();p.wait()
  os.close(m)
probe('openai/gpt-4.1',False);probe('anthropic/claude-opus-4-6',True);probe('codex/sol61',True)
"#);}
}

// Browser authorization constants and subscription compatibility follow the Pi
// revision pinned in SPEC (not installed newer docs, and not Pig's newer endpoints).
const ANTHROPIC_CLIENT_ID:&str="9d1c250a-e61b-44d9-88ed-5944d1962f5e";
const ANTHROPIC_REDIRECT:&str="https://console.anthropic.com/oauth/code/callback";
const ANTHROPIC_OAUTH_SYSTEM:&str="You are Claude Code, Anthropic's official CLI for Claude.";
fn oauth_random(length:usize)->Result<Vec<u8>>{
    use std::io::Read as _;
    let mut bytes=vec![0;length];File::open("/dev/urandom")?.read_exact(&mut bytes)?;Ok(bytes)
}
fn oauth_pkce()->Result<(String,String)>{
    let verifier=base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(oauth_random(32)?);
    let digest=ring::digest::digest(&ring::digest::SHA256,verifier.as_bytes());
    let challenge=base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(digest.as_ref());
    Ok((verifier,challenge))
}
fn oauth_token<'a>(body:&'a Value,name:&str)->Result<&'a str>{
    body[name].as_str().filter(|s|!s.is_empty()&&s.len()<=32768&&s.bytes().all(|c|(33..=126).contains(&c)))
        .ok_or_else(||"OAuth response contains an invalid or missing token".into())
}
fn anthropic_token_value(body:&Value)->Result<Value>{
    let access=oauth_token(body,"access_token")?;let refresh=oauth_token(body,"refresh_token")?;
    let seconds=body["expires_in"].as_f64().filter(|s|s.is_finite()&&*s>300.0&&*s<=31536000.0)
        .ok_or("Anthropic OAuth response has invalid token expiry")?;
    Ok(json!({"type":"oauth","access":access,"refresh":refresh,
        "expires":now_ms() as u64+(seconds*1000.0) as u64-300000}))
}
fn oauth_query(url:&reqwest::Url,name:&str)->Result<Option<String>>{
    let values:Vec<_>=url.query_pairs().filter(|(key,_)|key==name).map(|(_,v)|v.into_owned()).collect();
    if values.len()>1{return Err("OAuth callback contains duplicate parameters".into());}
    Ok(values.into_iter().next())
}
fn oauth_code(url:&reqwest::Url,state:&str)->Result<String>{
    if oauth_query(url,"state")?.as_deref()!=Some(state){return Err("OAuth callback state mismatch; login not saved".into());}
    if oauth_query(url,"error")?.is_some(){return Err("OAuth authorization denied; login not saved".into());}
    let code=oauth_query(url,"code")?.filter(|s|!s.is_empty()&&s.len()<=32768&&s.bytes().all(|c|(33..=126).contains(&c)))
        .ok_or("OAuth callback has no valid authorization code")?;
    Ok(code)
}
fn oauth_pasted_code(input:&str,redirect:&str,state:&str)->Result<String>{
    let input=input.trim();
    if input.contains("://"){
        let url=reqwest::Url::parse(input).map_err(|_|"Invalid OAuth redirect URL")?;
        let expected=reqwest::Url::parse(redirect).map_err(|_|"Invalid OAuth callback configuration")?;
        if url.origin()!=expected.origin()||url.path()!=expected.path()||!url.username().is_empty()||url.password().is_some(){
            return Err("OAuth redirect URL does not match this login's callback".into());
        }
        return oauth_code(&url,state);
    }
    let (code,received)=input.split_once('#').ok_or("Paste the full redirect URL or code#state from this login")?;
    if received!=state{return Err("OAuth callback state mismatch; login not saved".into());}
    if code.is_empty()||code.len()>32768||!code.bytes().all(|c|(33..=126).contains(&c)){
        return Err("OAuth callback has no valid authorization code".into());
    }
    Ok(code.into())
}
fn oauth_browser_open(url:&str){
    // Browser processes may outlive login; launch without waiting, reap in a
    // detached thread, and never route their output/arguments through H.
    let program=std::env::var("PY_OAUTH_BROWSER").unwrap_or_else(|_|if cfg!(target_os="macos"){"open"}else{"xdg-open"}.into());
    if program.is_empty(){return;}
    if let Ok(mut child)=Command::new(program).arg(url).stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null()).spawn(){
        std::thread::spawn(move||{let _=child.wait();});
    }
}
impl Host{
    fn anthropic_auth_base()->String{
        std::env::var("PY_ANTHROPIC_AUTH_BASE_URL").unwrap_or_else(|_|"https://console.anthropic.com".into()).trim_end_matches('/').into()
    }
    fn browser_deadline()->Result<std::time::Instant>{
        let seconds=std::env::var("PY_OAUTH_TIMEOUT_SECONDS").ok().map(|s|s.parse::<f64>()).transpose()?
            .unwrap_or(300.0);
        if !seconds.is_finite()||seconds<=0.0||seconds>900.0{return Err("Invalid browser OAuth timeout (0–900 seconds)".into());}
        Ok(std::time::Instant::now()+std::time::Duration::from_secs_f64(seconds))
    }
    fn browser_callback(&mut self,listener:&std::net::TcpListener,state:&str,deadline:std::time::Instant)->Result<String>{
        use std::io::Read as _;
        listener.set_nonblocking(true)?;
        loop{
            if self.codex_cancel("auth")?{return Err("browser login cancelled".into());}
            if std::time::Instant::now()>=deadline{return Err("browser login timed out; use /login codex manual for remote callback paste".into());}
            let (mut stream,peer)=match listener.accept(){
                Ok(v)=>v,
                Err(e)if matches!(e.kind(),io::ErrorKind::WouldBlock|io::ErrorKind::Interrupted)=>{std::thread::sleep(std::time::Duration::from_millis(15));continue;},
                Err(_)=>return Err("Could not receive browser OAuth callback".into())
            };
            if !peer.ip().is_loopback(){continue;}
            stream.set_nonblocking(true)?;
            let until=(std::time::Instant::now()+std::time::Duration::from_secs(1)).min(deadline);
            let mut bytes=SecretBytes(Vec::new());let mut complete=false;
            loop{
                if self.codex_cancel("auth")?{return Err("browser login cancelled".into());}
                if std::time::Instant::now()>=until{break;}
                let mut buffer=[0;1024];
                match stream.read(&mut buffer){
                    Ok(0)=>break,
                    Ok(n)=>{bytes.0.extend_from_slice(&buffer[..n]);if bytes.0.len()>8192{break;}
                        if bytes.0.windows(4).any(|w|w==b"\r\n\r\n"){complete=true;break;}},
                    Err(e)if matches!(e.kind(),io::ErrorKind::WouldBlock|io::ErrorKind::Interrupted)=>std::thread::sleep(std::time::Duration::from_millis(15)),
                    Err(_)=>break
                }
            }
            let parsed=if complete&&bytes.0.len()<=8192{
                std::str::from_utf8(&bytes.0).ok().and_then(|text|text.lines().next()).and_then(|line|{
                    let parts:Vec<_>=line.split(' ').collect();
                    if parts.len()!=3||parts[0]!="GET"||!parts[1].starts_with('/')||!matches!(parts[2],"HTTP/1.0"|"HTTP/1.1"){return None;}
                    reqwest::Url::parse(&format!("http://localhost{}",parts[1])).ok()
                })
            }else{None};
            let outcome=parsed.as_ref().filter(|url|url.path()=="/auth/callback").map(|url|oauth_code(url,state));
            let status=if parsed.as_ref().is_some_and(|url|url.path()!="/auth/callback"){"404 Not Found"}
                else if outcome.as_ref().is_some_and(|result|result.is_ok()){ "200 OK" }else{"400 Bad Request"};
            let body=if status=="200 OK"{"Authorization received. Return to py."}else{"Invalid authorization callback."};
            let response=format!("HTTP/1.1 {status}\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len());
            let _=stream.write_all(response.as_bytes());let _=stream.shutdown(std::net::Shutdown::Both);
            if let Some(Ok(code))=outcome{return Ok(code);}
            // A provider denial with the valid state ends the flow. Invalid
            // requests never consume the valid callback or reveal credentials.
            if parsed.as_ref().is_some_and(|url|url.path()=="/auth/callback"
                &&oauth_query(url,"state").ok().flatten().as_deref()==Some(state)
                &&oauth_query(url,"error").ok().flatten().is_some()){
                return Err("OAuth authorization denied; login not saved".into());
            }
        }
    }
    fn browser_login(&mut self,provider:&str,manual:bool)->Result<()>{
        self.with_state(UiState::Login,None,|host|host.browser_login_inner(provider,manual))
    }
    fn browser_login_inner(&mut self,provider:&str,manual:bool)->Result<()>{
        let provider=provider_alias(provider);
        if !matches!(provider,"anthropic"|"openai-codex"){return Err("Browser OAuth unsupported for this provider; /login lists methods".into());}
        let deadline=Self::browser_deadline()?;
        let (verifier,challenge)=oauth_pkce()?;
        let state=if provider=="anthropic"{verifier.clone()}else{base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(oauth_random(24)?)};
        let port=std::env::var("PY_OAUTH_CALLBACK_PORT").ok().map(|s|s.parse::<u16>()).transpose()?.unwrap_or(1455);
        let listener=if provider=="openai-codex"&&!manual{
            std::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST,port)).ok()
        }else{None};
        if listener.is_none()&&provider=="openai-codex"&&!manual&&self.json{
            return Err("Could not bind Codex callback; use a free callback port, device method, or interactive manual paste".into());
        }
        let callback_port=listener.as_ref().map(|l|l.local_addr().map(|addr|addr.port())).transpose()?.unwrap_or(port);
        let redirect=if provider=="anthropic"{ANTHROPIC_REDIRECT.to_string()}
            else{format!("http://localhost:{callback_port}/auth/callback")};
        let authorize=if provider=="anthropic"{
            std::env::var("PY_ANTHROPIC_AUTHORIZE_URL").unwrap_or_else(|_|"https://claude.ai/oauth/authorize".into())
        }else{format!("{}/oauth/authorize",Self::codex_auth_base())};
        let mut url=reqwest::Url::parse(&authorize).map_err(|_|"Invalid OAuth authorize endpoint")?;
        {
            let mut query=url.query_pairs_mut();
            query.append_pair("client_id",if provider=="anthropic"{ANTHROPIC_CLIENT_ID}else{CODEX_CLIENT_ID})
                .append_pair("response_type","code").append_pair("redirect_uri",&redirect)
                .append_pair("scope",if provider=="anthropic"{"org:create_api_key user:profile user:inference"}else{"openid profile email offline_access"})
                .append_pair("code_challenge",&challenge).append_pair("code_challenge_method","S256").append_pair("state",&state);
            if provider=="anthropic"{query.append_pair("code","true");}
            else{query.append_pair("id_token_add_organizations","true").append_pair("codex_cli_simplified_flow","true").append_pair("originator","py");}
        }
        self.event("login_prompt",json!({"provider":provider,"method":if manual{"manual"}else{"browser"},"url":url.as_str()}));
        if !self.json{
            ui_text(&format!("Open this URL in your browser (automatic opener is best-effort):\n{url}\nCtrl-C cancels; authorization codes are entered only in the hidden prompt."));
            if provider=="anthropic"{ui_text("Anthropic subscription compatibility uses the provider's Claude Code protocol marker/headers; py identity is also sent. Your original system instructions follow unchanged. Live compatibility is unverified; API-key login is available.");}
            if provider=="openai-codex"&&listener.is_none()&&!manual{ui_text("Callback port unavailable; paste this login's full redirect URL in the hidden prompt instead.");}
        }
        oauth_browser_open(url.as_str());
        let code=if let Some(listener)=listener.as_ref(){self.browser_callback(listener,&state,deadline)?}
            else{let pasted=terminal_secret_service("Authorization code (hidden): ",Some(deadline),||{if self.codex_cancel("auth")?{Err("login cancelled".into())}else{Ok(())}})?;
                oauth_pasted_code(&pasted,&redirect,&state)?};
        // Drop the loopback listener before any exchange: no second callback can
        // influence an exchange or be falsely acknowledged as another login.
        drop(listener);
        if std::time::Instant::now()>=deadline{return Err("browser login timed out; credentials unchanged".into());}
        let client=reqwest::Client::builder().redirect(reqwest::redirect::Policy::none())
            .timeout(deadline.saturating_duration_since(std::time::Instant::now()).min(std::time::Duration::from_secs(30))).build()?;
        let request=if provider=="anthropic"{
            client.post(format!("{}/v1/oauth/token",Self::anthropic_auth_base())).json(&json!({
                "grant_type":"authorization_code","client_id":ANTHROPIC_CLIENT_ID,"code":code,"state":state,
                "redirect_uri":redirect,"code_verifier":verifier}))
        }else{
            client.post(format!("{}/oauth/token",Self::codex_auth_base())).form(&[
                ("grant_type","authorization_code"),("client_id",CODEX_CLIENT_ID),("code",code.as_str()),
                ("code_verifier",verifier.as_str()),("redirect_uri",redirect.as_str())])
        };
        let (status,body)=self.codex_http(request).map_err(|e|e.to_string().replace("Codex","OAuth"))?;
        if status!=200{return Err(format!("OAuth token exchange rejected (HTTP {status}); login not saved").into());}
        let credential=if provider=="anthropic"{anthropic_token_value(&body)?}else{codex_token_value(&body)?};
        if self.codex_cancel("auth")?{return Err("browser login cancelled; credentials unchanged".into());}
        self.store_credential_until(provider,Some(credential),Some(deadline))
    }
    fn refresh_anthropic(&mut self)->Result<()>{
        let _lock=self.auth_lock()?;self.auth=load_json(&self.home.join("auth.json"))?;
        let credential=&self.auth["anthropic"];
        if credential["type"]!="oauth"{return Err("Anthropic subscription credential changed; retry operation without replaying code".into());}
        if credential["expires"].as_u64().unwrap_or(0)>now_ms() as u64{
            oauth_token(credential,"access")?;oauth_token(credential,"refresh")?;return Ok(());
        }
        let refresh=oauth_token(credential,"refresh")?.to_string();
        let client=reqwest::Client::builder().timeout(std::time::Duration::from_secs(30)).redirect(reqwest::redirect::Policy::none()).build()?;
        let (status,body)=self.codex_http(client.post(format!("{}/v1/oauth/token",Self::anthropic_auth_base()))
            .json(&json!({"grant_type":"refresh_token","client_id":ANTHROPIC_CLIENT_ID,"refresh_token":refresh})))
            .map_err(|e|e.to_string().replace("Codex","Anthropic OAuth"))?;
        if status!=200{return Err(format!("Anthropic OAuth refresh rejected (HTTP {status}); stored credential unchanged").into());}
        let next=anthropic_token_value(&body)?;let mut auth=self.auth.clone();auth["anthropic"]=next;
        if self.codex_cancel("auth")?{return Err("Anthropic OAuth refresh cancelled; stored credential unchanged".into());}
        write_private_json(&self.home.join("auth.json"),&auth)?;self.auth=auth;Ok(())
    }
}

#[cfg(test)]
mod browser_auth_e2e{
    fn check(body:&str){
        let script=format!(r#"
import os,sys,tempfile,json,subprocess,threading,queue,time,base64,hashlib,urllib.parse,http.client,http.server,socket,signal,glob,pty,select,fcntl,termios,struct,re
home=tempfile.mkdtemp(prefix='py-browser-auth-'); env=dict(os.environ,PY_HOME=home,NO_COLOR='1',TERM='xterm',PY_OAUTH_TIMEOUT_SECONDS='2',PY_OAUTH_CALLBACK_PORT='0')
for k in ['PY_OAUTH_BROWSER','PY_ANTHROPIC_AUTH_BASE_URL','PY_ANTHROPIC_AUTHORIZE_URL','PY_MODEL','PY_CONTEXT_LIMIT']:env.pop(k,None)
claims={{'https://api.openai.com/auth':{{'chatgpt_account_id':'fixture-account'}}}}
access='x.'+base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')+'.z'
tokens={{'access_token':access,'refresh_token':'fixture-refresh-SECRET','expires_in':3600}}
seen=[];status=200;response=tokens
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  b=self.rfile.read(int(self.headers.get('Content-Length','0'))).decode()
  value=json.loads(b) if self.headers.get('Content-Type','').startswith('application/json') else {{k:v[0] for k,v in urllib.parse.parse_qs(b).items()}}
  seen.append((self.path,dict(self.headers),value)); self.send_response(status);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(response).encode())
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
base='http://127.0.0.1:'+str(server.server_port)
env['PY_CODEX_AUTH_BASE_URL']=base;env['PY_ANTHROPIC_AUTH_BASE_URL']=base;env['PY_ANTHROPIC_AUTHORIZE_URL']=base+'/authorize'
binpath=home+'/bin';os.mkdir(binpath);opener=binpath+'/xdg-open'
open(opener,'w').write('#!/usr/bin/env python3\nimport os,sys\nopen(os.environ["OAUTH_URL_FILE"],"a").write(sys.argv[1]+"\\n")\n')
os.chmod(opener,0o700);env['PATH']=binpath+':'+env.get('PATH','');env['OAUTH_URL_FILE']=home+'/opened-url'
def history():return ''.join(open(f).read() for f in glob.glob(home+'/sessions/*.jsonl'))
class Client:
 def __init__(self):
  self.p=subprocess.Popen([sys.argv[1],'--json','--json-input','--no-model'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env);self.q=queue.Queue();self.all=[]
  def read():
   for l in self.p.stdout:self.q.put(json.loads(l))
  threading.Thread(target=read,daemon=True).start();self.until('ready')
 def send(self,**v):self.p.stdin.write(json.dumps(v)+'\n');self.p.stdin.flush()
 def until(self,kind,timeout=7):
  end=time.monotonic()+timeout
  while time.monotonic()<end:
   v=self.q.get(timeout=max(.01,end-time.monotonic()));self.all.append(v)
   if v['kind']==kind:return v
  raise AssertionError(self.all)
 def close(self):
  self.p.stdin.close()
  try:self.p.wait(timeout=5)
  except subprocess.TimeoutExpired:self.p.kill();self.p.wait()
  assert self.p.returncode==0,(self.all,self.p.stderr.read())
def callback(url,query=None,path=None):
 info=urllib.parse.urlsplit(url); params=urllib.parse.parse_qs(info.query);redirect=urllib.parse.urlsplit(params['redirect_uri'][0]);state=params['state'][0]
 conn=http.client.HTTPConnection(redirect.hostname,redirect.port,timeout=2)
 conn.request('GET',(path or redirect.path)+'?'+(query or urllib.parse.urlencode({{'state':state,'code':'fixture-code-SECRET'}})));r=conn.getresponse();result=r.status;r.read();conn.close();return result
class Terminal:
 def __init__(self):
  self.master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',30,160,0,0))
  def setup():os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
  self.p=subprocess.Popen([sys.argv[1],'--no-model'],stdin=slave,stdout=slave,stderr=slave,env=env,preexec_fn=setup);os.close(slave);self.data=b'';self.fresh(b'> ')
 def send(self,s):
  value=s.encode() if isinstance(s,str) else s
  if value.endswith(b'\n') and value.startswith((b'/',b'@')):
   if termios.tcgetattr(self.master)[3]&termios.ICANON:self.fresh(b'\x1b[?2004h')
   value=value[:-1]+b'\r'
  elif not termios.tcgetattr(self.master)[3]&termios.ICANON and value.endswith(b'\n'):value=value[:-1]+b'\r'
  os.write(self.master,value)
 def fresh(self,s,timeout=7):
  if s==b'> ':s=b'\x1b[?2004h'
  start=len(self.data);end=time.monotonic()+timeout
  while time.monotonic()<end:
   if s in self.data[start:]:return self.data[start:]
   if select.select([self.master],[],[],.05)[0]:
    try:self.data+=os.read(self.master,65536)
    except OSError:break
  raise AssertionError((s,self.data[-5000:]))
 def url(self):
  # Browser opening is asynchronous. Never use a stale preceding flow's URL
  # merely because the file exists; it must equal this flow's displayed URL.
  displayed=self.data.rsplit(b'Open this URL in your browser (automatic opener is best-effort):',1)[1].split(b'Ctrl-C cancels',1)[0]
  expected=b''.join(displayed.split()).decode()
  end=time.monotonic()+4
  while time.monotonic()<end:
   if os.path.exists(env['OAUTH_URL_FILE']):
    value=open(env['OAUTH_URL_FILE']).read()
    if expected in value.splitlines():return expected
   time.sleep(.02)
  raise AssertionError((expected,self.data))
 def close(self):
  self.send('/quit\n')
  try:self.p.wait(timeout=5)
  except subprocess.TimeoutExpired:self.p.kill();self.p.wait()
  if select.select([self.master],[],[],.05)[0]:
   try:self.data+=os.read(self.master,65536)
   except OSError:pass
  os.close(self.master);assert self.p.returncode==0,self.data
{body}
server.shutdown()
"#);
        let output=crate::test_command("python3").args(["-c",&script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(output.status.success(),"{}\n{}",String::from_utf8_lossy(&output.stdout),String::from_utf8_lossy(&output.stderr));
    }
    #[test]
    fn e2e_codex_browser_pkce_callback_validation_and_private_merge(){check(r#"
open(home+'/auth.json','w').write(json.dumps({'anthropic':{'type':'api_key','key':'keep-old-secret'}}));os.chmod(home+'/auth.json',0o600)
c=Client();c.send(id='browser',kind='login',provider='codex',method='browser');prompt=c.until('login_prompt');url=prompt['url'];params=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
assert params['originator']==['py'] and params['scope']==['openid profile email offline_access'],params
assert callback(url,path='/wrong')==404
assert callback(url,query='state=wrong&code=bad-SECRET')==400
state=params['state'][0]
assert callback(url,query=urllib.parse.urlencode([('state',state),('state',state),('code','bad-SECRET')]))==400
assert not seen,seen
assert callback(url)==200
assert c.until('completed')['status']=='ok',c.all
c.close();form=seen[0][2]
assert form['grant_type']=='authorization_code' and form['code']=='fixture-code-SECRET' and form['redirect_uri']==params['redirect_uri'][0],form
assert base64.urlsafe_b64encode(hashlib.sha256(form['code_verifier'].encode()).digest()).decode().rstrip('=')==params['code_challenge'][0],form
assert form['code_verifier']!=state and len(form['code_verifier'])>=43,form
auth=json.load(open(home+'/auth.json'));assert auth['anthropic']['key']=='keep-old-secret' and auth['openai-codex']['accountId']=='fixture-account',auth
assert os.stat(home+'/auth.json').st_mode&0o777==0o600
for secret in ['fixture-code-SECRET','fixture-refresh-SECRET',access,form['code_verifier']]:assert secret not in history(),history()
try:callback(url);assert False,'listener still open'
except OSError:pass
"#);}
    #[test]
    fn e2e_browser_opener_failure_still_allows_callback_and_login_fields_redact(){check(r#"
env['PY_OAUTH_BROWSER']=home+'/no-such-browser'
c=Client();c.send(id='browser-fallback',kind='login',provider='codex',method='browser',code='accidental-code-SECRET',access_token='accidental-token-SECRET')
url=c.until('login_prompt')['url'];assert callback(url)==200;assert c.until('completed')['status']=='ok';c.close()
assert 'accidental-code-SECRET' not in history() and 'accidental-token-SECRET' not in history(),history()
assert len(seen)==1,seen
"#);}
    #[test]
    fn e2e_codex_browser_failure_timeout_cancel_never_overwrite(){check(r#"
old={'openai-codex':{'type':'api_key','key':'keep-secret'}}
for mode in ['denied','malformed','timeout','cancel','slow-client']:
 open(home+'/auth.json','w').write(json.dumps(old));os.chmod(home+'/auth.json',0o600)
 response={'error':'fixture-token-SECRET'} if mode=='denied' else {'access_token':'malformed-token-SECRET','refresh_token':'fixture-refresh-SECRET','expires_in':3600}
 status=401 if mode=='denied' else 200
 c=Client();c.send(id=mode,kind='login',provider='codex',method='browser');url=c.until('login_prompt')['url']
 if mode in ['denied','malformed']:assert callback(url)==200
 elif mode=='cancel':c.send(id='cancel',kind='interrupt')
 elif mode=='slow-client':
  redirect=urllib.parse.urlsplit(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)['redirect_uri'][0]);s=socket.create_connection((redirect.hostname,redirect.port));s.sendall(b'GET /auth/callback?');c.send(id='cancel',kind='interrupt')
 error=c.until('error',timeout=5);assert 'SECRET' not in json.dumps(error),error
 c.send(id='after',kind='python',source='print("still-usable")');assert c.until('preview')['status']=='ok';assert c.until('completed')['status']=='ok'
 c.close();assert json.load(open(home+'/auth.json'))==old
 if mode=='slow-client':s.close()
"#);}
    #[test]
    fn e2e_browser_manual_timeout_bind_fallback_and_exchange_cancel(){check(r#"
old={'anthropic':{'type':'api_key','key':'keep-secret'}};open(home+'/auth.json','w').write(json.dumps(old));os.chmod(home+'/auth.json',0o600)
t=Terminal();t.send('/login anthropic manual\n');t.fresh(b'Authorization code (hidden): ');t.fresh(b'> ',timeout=5)
assert json.load(open(home+'/auth.json'))==old and not seen,t.data
occupied=socket.socket();occupied.bind(('127.0.0.1',0));occupied.listen();env['PY_OAUTH_CALLBACK_PORT']=str(occupied.getsockname()[1])
t.close();t=Terminal();t.send('/login codex\n');t.fresh(b'Authorization code (hidden): ');t.send(b'\x03');t.fresh(b'> ');t.send('@print("echo-restored")\n');t.fresh(b'status: ok');t.close();occupied.close()
assert json.load(open(home+'/auth.json'))==old and not seen,t.data
env['PY_OAUTH_CALLBACK_PORT']='0';env['PY_OAUTH_TIMEOUT_SECONDS']='6';entered=threading.Event();release=threading.Event()
def stall(self):
 self.rfile.read(int(self.headers.get('Content-Length','0')));entered.set();release.wait(3)
 self.send_response(200);self.end_headers()
 try:self.wfile.write(json.dumps(tokens).encode())
 except OSError:pass
Handler.do_POST=stall
c=Client();c.send(id='exchange',kind='login',provider='codex',method='browser');url=c.until('login_prompt')['url'];assert callback(url)==200;assert entered.wait(2)
c.send(id='cancel-exchange',kind='interrupt');error=c.until('error',timeout=2);assert 'cancelled' in error['error'] and 'SECRET' not in json.dumps(error),error
assert json.load(open(home+'/auth.json'))==old
c.send(id='after',kind='python',source='print("still-usable")');assert c.until('preview')['status']=='ok';c.until('completed');c.close();release.set()
"#);}
    #[test]
    fn e2e_anthropic_browser_manual_pkce_state_and_hidden_secret(){check(r#"
t=Terminal();t.send('/login anthropic\n');t.fresh(b'Authorization code (hidden): ');url=t.url();params=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
assert params['scope']==['org:create_api_key user:profile user:inference'] and params['redirect_uri']==['https://console.anthropic.com/oauth/code/callback'],params
state=params['state'][0];t.send('anthropic-code-SECRET#'+state+'\n');t.fresh(b'Stored credentials for anthropic');t.send('@print("after-auth")\n');t.fresh(b'status: ok');t.close()
form=seen[0][2];assert seen[0][0]=='/v1/oauth/token' and form['state']==state and form['code_verifier']==state and form['code']=='anthropic-code-SECRET',seen
assert base64.urlsafe_b64encode(hashlib.sha256(state.encode()).digest()).decode().rstrip('=')==params['code_challenge'][0],params
assert b'anthropic-code-SECRET' not in t.data,t.data
assert 'anthropic-code-SECRET' not in history() and 'fixture-refresh-SECRET' not in history()
auth=json.load(open(home+'/auth.json'));assert auth['anthropic']['type']=='oauth' and auth['anthropic']['expires']>time.time()*1000+3000000,auth
assert os.stat(home+'/auth.json').st_mode&0o777==0o600
assert '/login' not in open(home+'/editor-history').read()
"#);}
    #[test]
    fn e2e_browser_manual_bad_state_cancel_and_codex_redirect_hidden(){check(r#"
old={'anthropic':{'type':'api_key','key':'keep-secret'}};open(home+'/auth.json','w').write(json.dumps(old));os.chmod(home+'/auth.json',0o600)
t=Terminal()
for provider,value in [('anthropic','bad-code-SECRET#wrong-state\n'),('anthropic',b'\x03'),('codex',b'\x04')]:
 if os.path.exists(env['OAUTH_URL_FILE']):os.unlink(env['OAUTH_URL_FILE'])
 t.send('/login '+provider+' manual\n');t.fresh(b'Authorization code (hidden): ');t.send(value);t.fresh(b'> ')
 assert json.load(open(home+'/auth.json'))==old and not seen,(seen,t.data)
if os.path.exists(env['OAUTH_URL_FILE']):os.unlink(env['OAUTH_URL_FILE'])
t.send('/login codex manual\n');t.fresh(b'Authorization code (hidden): ');url=t.url();params=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
redirect=params['redirect_uri'][0]+'?'+urllib.parse.urlencode({'code':'manual-code-SECRET','state':params['state'][0]})
t.send(redirect+'\n');t.fresh(b'Stored credentials for openai-codex');t.send('@print("terminal-restored")\n');t.fresh(b'status: ok');t.close()
assert b'manual-code-SECRET' not in t.data and b'bad-code-SECRET' not in t.data,t.data
assert seen[-1][2]['code']=='manual-code-SECRET' and json.load(open(home+'/auth.json'))['anthropic']==old['anthropic']
for secret in ['manual-code-SECRET','bad-code-SECRET','fixture-refresh-SECRET']:assert secret not in history()
"#);}
    #[test]
    fn e2e_anthropic_failed_refresh_and_parallel_rotation_preserve_store(){check(r#"
config={'providers':{'anthropic':{'base_url':base,'models':[{'id':'fixture','api':'anthropic-messages','reasoning':False}]}}};open(home+'/config.json','w').write(json.dumps(config))
old={'anthropic':{'type':'oauth','access':'old-access-SECRET','refresh':'old-refresh-SECRET','expires':1},'openai':{'type':'api_key','key':'preserve-key'}}
for mode in ['denied','malformed']:
 open(home+'/auth.json','w').write(json.dumps(old));os.chmod(home+'/auth.json',0o600)
 status=401 if mode=='denied' else 200;response={'error':'returned-provider-secret-SECRET'} if mode=='denied' else {'access_token':'returned-provider-secret-SECRET','refresh_token':'rotated-refresh-SECRET','expires_in':'invalid'}
 c=Client();c.send(id=mode,kind='python',source="agent.llm('hello',model='anthropic/fixture')");preview=c.until('preview');assert preview['status']=='error',c.all;c.until('completed');c.close()
 assert json.load(open(home+'/auth.json'))==old and 'returned-provider-secret-SECRET' not in history(),history()
status=200;response={'access_token':'sk-ant-oat-fixture-SECRET','refresh_token':'rotated-refresh-SECRET','expires_in':3600};seen.clear()
open(home+'/auth.json','w').write(json.dumps(old));os.chmod(home+'/auth.json',0o600)
original=Handler.do_POST
def post(self):
 if self.path=='/messages':
  b=json.loads(self.rfile.read(int(self.headers['Content-Length'])).decode());seen.append((self.path,dict(self.headers),b));self.send_response(200);self.end_headers();self.wfile.write(json.dumps({'content':[{'type':'text','text':'done'}]}).encode())
 else:time.sleep(.15);original(self)
Handler.do_POST=post
a=Client();b=Client();a.send(id='one',kind='python',source="agent.llm('one',model='anthropic/fixture')");b.send(id='two',kind='python',source="agent.llm('two',model='anthropic/fixture')")
assert a.until('preview')['status']=='ok';a.until('completed');assert b.until('preview')['status']=='ok';b.until('completed');a.close();b.close()
assert len([v for v in seen if v[0]=='/v1/oauth/token'])==1 and len([v for v in seen if v[0]=='/messages'])==2,seen
assert json.load(open(home+'/auth.json'))['openai']==old['openai']
"#);}
    #[test]
    fn e2e_anthropic_refresh_bearer_protocol_and_api_key_unchanged(){check(r#"
response={'access_token':'sk-ant-oat-fixture-SECRET','refresh_token':'rotated-refresh-SECRET','expires_in':3600}
config={'providers':{'anthropic':{'base_url':base,'models':[{'id':'fixture','api':'anthropic-messages','reasoning':False}]}}}
open(home+'/config.json','w').write(json.dumps(config));old={'anthropic':{'type':'oauth','access':'old-access-SECRET','refresh':'old-refresh-SECRET','expires':1},'openai':{'type':'api_key','key':'preserve-key'}}
open(home+'/auth.json','w').write(json.dumps(old));os.chmod(home+'/auth.json',0o600)
original=Handler.do_POST
def post(self):
 global response
 if self.path=='/messages':
  b=json.loads(self.rfile.read(int(self.headers['Content-Length'])).decode());seen.append((self.path,dict(self.headers),b));self.send_response(200);self.end_headers();self.wfile.write(json.dumps({'content':[{'type':'text','text':'fixture-answer'}]}).encode())
 else:original(self)
Handler.do_POST=post
c=Client();c.send(id='invoke',kind='python',source="print(agent.llm('hello',model='anthropic/fixture'))");assert c.until('preview')['status']=='ok';c.until('completed')
assert seen[0][0]=='/v1/oauth/token' and seen[0][2]['grant_type']=='refresh_token' and seen[0][2]['refresh_token']=='old-refresh-SECRET',seen
headers={k.lower():v for k,v in seen[1][1].items()};assert headers['authorization']=='Bearer sk-ant-oat-fixture-SECRET' and 'x-api-key' not in headers,headers
assert 'oauth-2025-04-20' in headers['anthropic-beta'] and 'claude-code-20250219' in headers['anthropic-beta'] and 'py-rust' in headers['user-agent'],headers
assert seen[1][2]['system'][0]['text']=="You are Claude Code, Anthropic's official CLI for Claude." and 'py' in history(),seen[1]
auth=json.load(open(home+'/auth.json'));assert auth['openai']==old['openai'] and auth['anthropic']['refresh']=='rotated-refresh-SECRET',auth
c.send(id='invoke2',kind='python',source="agent.llm('again',model='anthropic/fixture')");c.until('completed');assert len([v for v in seen if v[0]=='/v1/oauth/token'])==1,seen
c.close();assert 'rotated-refresh-SECRET' not in history() and 'sk-ant-oat-fixture-SECRET' not in history(),history()
# Explicit API-key credentials do not receive subscription headers/system marker.
auth['anthropic']={'type':'api_key','key':'api-key-fixture'};open(home+'/auth.json','w').write(json.dumps(auth));os.chmod(home+'/auth.json',0o600)
c=Client();c.send(id='key-invoke',kind='python',source="agent.llm('hello',model='anthropic/fixture')");c.until('completed');c.close()
headers={k.lower():v for k,v in seen[-1][1].items()};assert headers['x-api-key']=='api-key-fixture' and 'authorization' not in headers and 'oauth-2025-04-20' not in headers.get('anthropic-beta',''),headers
assert isinstance(seen[-1][2]['system'],str),seen[-1]
"#);}
}

#[cfg(test)]
mod env_e2e{
    use super::*;
    #[test]
    fn e2e_test_fixture_environment_is_sanitized(){
        let keys:Vec<&str>=PROVIDERS.iter().map(|p|p.2).chain([
            "PY_HOME","PY_MODEL","PY_CONTEXT_LIMIT","PY_CODEX_AUTH_BASE_URL",
            "PY_CODEX_DEVICE_TIMEOUT_SECONDS","PY_OAUTH_BROWSER","PY_OAUTH_TIMEOUT_SECONDS",
            "PY_OAUTH_CALLBACK_PORT","PY_ANTHROPIC_AUTH_BASE_URL","PY_ANTHROPIC_AUTHORIZE_URL"]).collect();
        let mut command=crate::test_command(std::env::var("PY_HARNESS_BIN").unwrap());
        // Inspect only explicit removals, never values inherited from the developer.
        for key in &keys{
            assert!(command.get_envs().any(|(name,value)|name==*key&&value.is_none()),
                "missing fixture environment removal for {key}");
        }
        // PY_HOME is an intentional per-fixture override, not an inherited path.
        let home=std::env::temp_dir().join(format!("py-env-fixture-{}-{}",std::process::id(),
            std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos()));
        let observed:Vec<_>=keys.iter().filter(|key|**key!="PY_HOME").copied().collect();
        let source=format!("import os; print(all(key not in os.environ for key in {}))",
            serde_json::to_string(&observed).unwrap());
        command.args(["--json","--json-input","--no-model"]).env("PY_HOME",&home)
            .stdin(std::process::Stdio::piped()).stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped());
        let mut child=command.spawn().unwrap();
        writeln!(child.stdin.as_mut().unwrap(),"{}",json!({"id":"env-proof","kind":"python","source":source})).unwrap();
        drop(child.stdin.take());
        let output=child.wait_with_output().unwrap();
        assert!(output.status.success(),"{}",String::from_utf8_lossy(&output.stderr));
        let events:Vec<Value>=String::from_utf8(output.stdout).unwrap().lines()
            .map(|line|serde_json::from_str(line).unwrap()).collect();
        assert!(events.iter().any(|v|v["kind"]=="completed"&&v["command_id"]=="env-proof"&&v["status"]=="ok"));
        let journal_path=fs::read_dir(home.join("sessions")).unwrap().next().unwrap().unwrap().path();
        let journal=fs::read_to_string(journal_path).unwrap();
        assert!(journal.lines().map(|line|serde_json::from_str::<Value>(line).unwrap())
            .any(|v|v["kind"]=="stream"&&v["payload"]["collection"]=="stdout"&&v["payload"]["text"]=="True\n"));
    }
}

#[cfg(test)]
mod newline_e2e {
    use super::*;
    fn pty(check:&str){
        let script=format!(r#"
import os,sys,pty,fcntl,termios,select,time,subprocess,tempfile,glob,json,struct,signal
home=tempfile.mkdtemp(prefix='py-newline-');env=dict(os.environ,PY_HOME=home,TERM='xterm-256color',NO_COLOR='1')
m,s=pty.openpty();fcntl.ioctl(s,termios.TIOCSWINSZ,struct.pack('HHHH',32,160,0,0))
def controlling():os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
p=subprocess.Popen([sys.argv[1],'--no-model'],stdin=s,stdout=s,stderr=s,env=env,preexec_fn=controlling)
os.close(s);data=bytearray()
def pump(duration=.08):
 end=time.monotonic()+duration
 while time.monotonic()<end:
  if select.select([m],[],[],.02)[0]:
   try:data.extend(os.read(m,65536))
   except OSError:break
def wait(predicate,label):
 end=time.monotonic()+6
 while not predicate():
  pump(.02)
  if time.monotonic()>end:raise AssertionError((label,bytes(data)))
def send(text):os.write(m,text.encode())
def events():return [json.loads(line) for path in glob.glob(home+'/sessions/*.jsonl') for line in open(path)]
def codes():return [e['payload']['source'] for e in events() if e['kind']=='code']
key=os.environ.get('PY_TEST_NEWLINE','\n')
try:
 wait(lambda:b'\x1b[?2004h' in data,'ready')
 {check}
finally:
 try:os.killpg(p.pid,signal.SIGKILL)
 except ProcessLookupError:pass
 p.wait(timeout=3);os.close(m)
"#);
        let out=test_command("python3").args(["-c",&script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(out.status.success(),"{}\n{}",String::from_utf8_lossy(&out.stdout),String::from_utf8_lossy(&out.stderr));
    }
    #[test]
    fn e2e_ctrl_j_inserts_newline_without_submitting_complete_python(){pty(r#"
 send('@n=1');pump();send(key);pump()
 assert not codes(),(codes(),bytes(data))
 send('print(n)\r');wait(lambda:len(codes())==1,'one cell')
 assert codes()==['n=1\nprint(n)'],(codes(),bytes(data))
 wait(lambda:any(e['kind']=='stream' and e['payload'].get('text')=='1\n' for e in events()),'executed')
"#);}
    #[test]
    fn e2e_ctrl_j_repeated_midline_and_leading_whitespace_are_preserved(){pty(r#"
 send('@print("a");print("b")');pump();send('\x01\x1b[C');send(key);pump()
 assert not codes(),(codes(),bytes(data))
 send('\x05');send(key);send(key);send('print("c")\r')
 wait(lambda:len(codes())==1,'one multiline cell')
 assert codes()==['\nprint("a");print("b")\n\nprint("c")'],(codes(),bytes(data))
"#);}
    #[test]
    fn e2e_ctrl_j_plain_text_is_one_user_message(){pty(r#"
 send('first paragraph');pump();send(key);pump()
 assert not any(e['kind']=='user' for e in events()),bytes(data)
 send('second paragraph\r')
 wait(lambda:any(e['kind']=='user' for e in events()),'one message')
 texts=[e['payload']['text'] for e in events() if e['kind']=='user']
 assert texts==['first paragraph\nsecond paragraph'],(texts,bytes(data))
"#);}
}

#[cfg(test)]
mod picker_e2e{
    fn fixture(body:&str){
        let setup=r#"
import os,sys,pty,fcntl,termios,struct,subprocess,select,time,json,tempfile,re,unicodedata
home=tempfile.mkdtemp(prefix='py-picker-');cwd=tempfile.mkdtemp(prefix='py-picker-cwd-')
m,s=pty.openpty();fcntl.ioctl(s,termios.TIOCSWINSZ,struct.pack('HHHH',24,80,0,0))
def controlling():os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
env=dict(os.environ,PY_HOME=home,HOME=cwd,PY_MODEL='openai/gpt-5',TERM='xterm-256color',NO_COLOR='1');env['PATH']=cwd+os.pathsep+env.get('PATH','')
p=subprocess.Popen([sys.argv[1],'--no-model'],stdin=s,stdout=s,stderr=s,cwd=cwd,env=env,preexec_fn=controlling);os.close(s);data=bytearray()
def pump():
 if select.select([m],[],[],.02)[0]:
  try:data.extend(os.read(m,65536))
  except OSError:pass
 return data.decode(errors='replace')
def wait(f,label):
 end=time.monotonic()+6
 while time.monotonic()<end:
  pump()
  if f():return
  if p.poll() is not None:raise AssertionError((label,p.returncode,bytes(data)))
 raise AssertionError((label,bytes(data)))
def send(v):os.write(m,v.encode() if isinstance(v,str) else v)
def records():
 try:
  import glob
  return [json.loads(l) for path in glob.glob(home+'/sessions/*.jsonl') for l in open(path) if l.endswith('\n')]
 except (OSError,json.JSONDecodeError):return []
def sources():return [v['payload']['source'] for v in records() if v['kind']=='code']
def streams():return ''.join(v['payload']['text'] for v in records() if v['kind']=='stream' and v['payload']['collection']=='stdout')
def command(line):
 count=data.count(b'\x1b[?2004h');send(line+'\r');wait(lambda:data.count(b'\x1b[?2004h')>count,'ready after '+line)
def code(source):
 command('@'+source);assert source in sources(),(source,records(),data)
def menu(line):
 start=len(data);send(line+'\t');wait(lambda:b'Fuzzy select' in data[start:],'picker opens');return start
def choose(query=''):
 start=len(data);send(query+'\r');wait(lambda:b'\x1b[0J' in data[start:],'picker closes on selection')
wait(lambda:b'\x1b[?2004h' in data,'editor ready')
try:
"#;
        let teardown=r#"
 command('/quit');p.wait(timeout=3);assert p.returncode==0,data
finally:
 if p.poll() is None:p.kill();p.wait()
 os.close(m)
"#;
        // /quit ends rather than starts a new editor; handle it in teardown.
        let teardown=teardown.replace(" command('/quit');p.wait", " send('/quit\\r');p.wait");
        let script=format!("{setup}{}{teardown}",body.lines().map(|l|format!(" {l}\n")).collect::<String>());
        let output=crate::test_command("python3").args(["-c",&script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(output.status.success(),"picker PTY fixture failed:\n{}\n{}",String::from_utf8_lossy(&output.stderr),String::from_utf8_lossy(&output.stdout));
    }
    #[test]
    fn e2e_picker_slash_and_model_live_filter_nonfirst_choice(){fixture(r#"
menu('/');send('mdl');time.sleep(.05);pump();send('\x1b[B');choose()
assert not any(v['kind']=='settings_change' for v in records()),records()
command('');assert 'Models' in pump(),data
menu('/model ');choose('anthropic/ha45');assert not any(v['kind']=='settings_change' for v in records()),records()
command('');assert any(v['kind']=='settings_change' and v['payload']['model']=='anthropic/claude-haiku-4-5' for v in records()),records()
"#);}
    #[test]
    fn e2e_picker_effort_and_provider_arguments(){fixture(r#"
menu('/effort ');send('l');time.sleep(.04);pump();send('\t\x1b[A\x1b[B');choose();command('')
assert any(v['kind']=='settings_change' and v['payload']['effort']=='minimal' for v in records()),records()
menu('/logout ');choose('gq');command('');assert os.path.exists(home+'/auth.json'),data
"#);}
    #[test]
    fn e2e_picker_python_names_refilter_without_execution(){fixture(r#"
code('zz_picker_alpha=11; zz_picker_beta=22')
menu('@print(zz_picker_');choose('bt');assert len(sources())==1,records()
command(')');assert 'print(zz_picker_beta)' in sources() and '22\n' in streams(),records()
"#);}
    #[test]
    fn e2e_picker_executables_and_quoted_unicode_files(){fixture(r#"
for name,label in [('zzpickalpha','alpha'),('zzpickbeta','beta')]:
 path=cwd+'/'+name;open(path,'w').write('#!/bin/sh\nprintf "'+label+'-picker-ok\\n"\n');os.chmod(path,0o700)
# Prefix ranking prefers the shorter beta name; Down chooses non-first alpha.
menu('!zzpick');send('\x1b[B');choose();command('');assert 'alpha-picker-ok\n' in streams(),records()
open(cwd+'/zz α spaced.txt','w').write('alpha-file');open(cwd+'/zz β spaced.txt','w').write('beta-file')
menu('!cat ');choose('βsp');command('');assert 'beta-file' in streams(),records()
menu("@print(open('zz ");choose('β');command("').read())")
assert "print(open('zz β spaced.txt').read())" in sources(),sources()
"#);}
    #[test]
    fn e2e_picker_escape_ctrl_c_and_no_match_recovery(){fixture(r#"
code('zz_picker_alpha=11; zz_picker_beta=22')
start=menu('@print(zz_picker_');send('no-match-xyz');wait(lambda:b'No matches' in data[start:],'no-match display')
send('\x15zz_picker_bt');choose();command(')');assert 'print(zz_picker_beta)' in sources(),sources()
menu('@print(zz_picker_');send('discard-me\x1b');time.sleep(.15);pump();command('alpha)')
assert 'print(zz_picker_alpha)' in sources() and all('discard-me' not in s for s in sources()),sources()
menu('@print(zz_picker_');send('discard-me\x03');time.sleep(.08);pump();command('beta)')
assert sources().count('print(zz_picker_beta)')==2 and streams().count('22\n')==2,(sources(),streams())
start=menu('@print(zz_picker_');os.kill(p.pid,2);wait(lambda:b'\x1b[0J' in data[start:],'external SIGINT closes picker')
command('alpha)');assert sources().count('print(zz_picker_alpha)')==2 and streams().count('11\n')==2,(sources(),streams())
"#);}
    #[test]
    fn e2e_picker_narrow_no_color_and_terminal_restoration(){fixture(r#"
fcntl.ioctl(m,termios.TIOCSWINSZ,struct.pack('HHHH',9,28,0,0));os.kill(p.pid,28)
start=menu('/model ');send('haiku');time.sleep(.05);pump();choose()
segment=bytes(data[start:]);assert not re.search(rb'\x1b\[[0-9;]*m',segment),segment
for frame in segment.split(b'\x1b[?25l')[1:]:
 text=re.sub(rb'\x1b\[[0-9;?]*[a-zA-Z]',b'',frame.split(b'\x1b[0J')[0]).decode(errors='replace')
 for line in text.splitlines():
  if not line:continue
  width=sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W','F') else 1 for c in line)
  assert width<=28,(width,line,segment)
send('\x15');command('@print("terminal-restored")');assert 'terminal-restored\n' in streams(),records()
"#);}
    #[test]
    fn e2e_picker_never_appears_in_json_or_non_tty(){
        let script=r#"
import os,sys,tempfile,subprocess,json
env=dict(os.environ,PY_HOME=tempfile.mkdtemp(prefix='py-picker-json-'),TERM='xterm-256color')
p=subprocess.run([sys.argv[1],'--json','--json-input','--no-model'],input=json.dumps({'id':'plain','kind':'python','source':'print("plain")'})+'\n',text=True,capture_output=True,env=env,timeout=7)
assert p.returncode==0,p.stderr
values=[json.loads(l) for l in p.stdout.splitlines()]
assert any(v['kind']=='completed' and v['command_id']=='plain' for v in values),values
assert '\x1b' not in p.stdout and 'Fuzzy select' not in p.stdout,p.stdout
env['PY_HOME']=tempfile.mkdtemp(prefix='py-picker-pipe-')
p=subprocess.run([sys.argv[1],'--no-model'],input='/help\n/quit\n',text=True,capture_output=True,env=env,timeout=7)
assert p.returncode==0 and '\x1b' not in p.stdout+p.stderr,(p.stdout,p.stderr)
"#;
        let output=crate::test_command("python3").args(["-c",script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(output.status.success(),"{}",String::from_utf8_lossy(&output.stderr));
    }
}

#[cfg(test)]
mod editor_e2e{
    use super::*;
    fn pty(body:&str){
        let script=format!(r#"
import os,sys,pty,fcntl,termios,select,time,subprocess,tempfile,glob,json,struct,signal,re
home=tempfile.mkdtemp(prefix='py-editor-');env=dict(os.environ,PY_HOME=home,TERM='xterm-256color',NO_COLOR='1')
m,s=pty.openpty();fcntl.ioctl(s,termios.TIOCSWINSZ,struct.pack('HHHH',30,100,0,0));initial=termios.tcgetattr(s)
def controlling():os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
p=subprocess.Popen([sys.argv[1],'--no-model'],stdin=s,stdout=s,stderr=s,env=env,preexec_fn=controlling)
os.close(s);data=bytearray()
def pump(duration=.05):
 end=time.monotonic()+duration
 while time.monotonic()<end:
  if select.select([m],[],[],.01)[0]:
   try:data.extend(os.read(m,65536))
   except OSError:break
def wait(f,label):
 end=time.monotonic()+7
 while not f():
  pump(.02)
  if time.monotonic()>end:raise AssertionError((label,bytes(data)))
def send(v):os.write(m,v.encode() if isinstance(v,str) else v)
def records():
 try:return [json.loads(l) for path in glob.glob(home+'/sessions/*.jsonl') for l in open(path) if l.endswith('\n')]
 except (OSError,json.JSONDecodeError):return []
def sources():return [e['payload']['source'] for e in records() if e['kind']=='code']
def output():return ''.join(e['payload'].get('text','') for e in records() if e['kind']=='stream' and e['payload']['collection']=='stdout')
def command(text):
 count=data.count(b'\x1b[?2004h');send(text+'\r');wait(lambda:data.count(b'\x1b[?2004h')>count,'ready '+text)
try:
 wait(lambda:b'\x1b[?2004h' in data,'ready')
{body}
finally:
 if p.poll() is None:os.killpg(p.pid,signal.SIGKILL)
 p.wait(timeout=3);os.close(m)
"#,body=body.lines().map(|l|format!(" {l}\n")).collect::<String>());
        let out=test_command("python3").args(["-c",&script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(out.status.success(),"{}\n{}",String::from_utf8_lossy(&out.stdout),String::from_utf8_lossy(&out.stderr));
    }
    #[test]
    fn e2e_editor_shift_enter_kitty_and_xterm_one_cell(){pty(r#"
for key in ('\x1b[13;2u','\x1b[27;2;13~','\x1b[13;2~','\x1b\r'):
 before=len(sources());send('@n=7');pump();send(key);pump()
 assert len(sources())==before,(sources(),bytes(data))
 send('print(n)');pump()
 # This physical continuation is editor output, checked before execution.
 assert b'\r\n  print(n)' in data,(sources(),bytes(data))
 frame=bytes(data).split(b'\x1b[K\x1b[J')[-1]
 visible=re.sub(rb'\x1b\[[0-9;?<>:]*[a-zA-Z]',b'',frame)
 assert visible.startswith(b'> @n=7\r\n  print(n)'),visible
 command('');assert sources()[-1]=='n=7\nprint(n)',(sources(),bytes(data))
 assert output().endswith('7\n'),output()
"#);}
    #[test]
    fn e2e_editor_shift_enter_plain_text_and_reports_never_leak(){pty(r#"
send('first paragraph\x1b[13;2u');pump();assert not any(e['kind']=='user' for e in records()),records()
command('second paragraph');texts=[e['payload']['text'] for e in records() if e['kind']=='user']
assert texts==['first paragraph\nsecond paragraph'],texts
# Unsupported complete CSI is ignored, never left as source fragments.
command('@print("report-safe")\x1b[27;9;13~\x1b[999;8u\x1b['+'9'*80+'~')
assert sources()==['print("report-safe")'],sources()
"#);}
    #[test]
    fn e2e_editor_unicode_graphemes_midline_and_undo(){pty(r#"
send('@print("界e\u0301👩\u200d💻")');pump();send('\x1b[D\x1b[D\x7f');pump();command('')
assert sources()==['print("界e\u0301")'],(sources(),bytes(data))
assert output()=='界e\u0301\n',output()
send('@print("left");print("right")');pump();send('\x01\x1b[C\x1b[13;2u');send('\x05\x1b[13;2u');command('print("third")')
assert sources()[-1]=='\nprint("left");print("right")\nprint("third")',sources()
send('@print("ab")');pump();send('\x1b[D\x1b[D\x7f\x1f');command('')
assert sources()[-1]=='print("ab")',sources()
"#);}
    #[test]
    fn e2e_editor_history_multiline_arrows_and_kill_yank(){pty(r#"
command('@previous=31')
send('@draft=32\x1b[13;2uprint(draft)');pump();send('\x1b[A\x1b[B');command('')
assert sources()[-1]=='draft=32\nprint(draft)',sources()
# Up selects the preceding complete multiline cell; it is not flattened.
send('\x1b[A');pump();command('');assert sources()[-1]=='draft=32\nprint(draft)',sources()
send('@print("kill-yank")');pump();send('\x01\x0b\x19');command('')
assert sources()[-1]=='print("kill-yank")',sources()
"#);}
    #[test]
    fn e2e_editor_paste_keeps_whitespace_and_terminal_restores(){pty(r#"
source='\nfor i in range(2):\n    print("paste", i)\n'
send('\x1b[200~@'+source+'\x1b[201~');pump();assert not sources(),sources()
command('');assert sources()==[source],sources()
send('@this_must_not_execute');pump();send('\x03');wait(lambda:data.count(b'\x1b[?2004h')>=3,'CtrlC restored')
command('@print("after-cancel")');assert sources()[-1]=='print("after-cancel")' and 'this_must_not_execute' not in ''.join(sources()),sources()
count=data.count(b'\x1b[?2004h');os.kill(p.pid,signal.SIGINT);wait(lambda:data.count(b'\x1b[?2004h')>count,'signal restored')
command('@print("after-signal")');assert output().endswith('after-signal\n'),output()
send('\x04');p.wait(timeout=3);pump();assert p.returncode==0,(p.returncode,bytes(data))
flags=termios.tcgetattr(m);mask=termios.ECHO|termios.ICANON|termios.ISIG
assert flags[3]&mask==initial[3]&mask,(flags,initial)
assert b'\x1b[?2004l' in data and b'\x1b[<u' in data,bytes(data)
"#);}
    #[test]
    fn e2e_editor_word_wrap_narrow_resize_preserves_cursor_source(){pty(r#"
fcntl.ioctl(m,termios.TIOCSWINSZ,struct.pack('HHHH',30,25,0,0));os.kill(p.pid,signal.SIGWINCH)
send('@print("alpha beta gamma delta epsilon")');pump(.12)
# Word boundaries and aligned continuation are display only.
assert b'\r\n  ' in data,bytes(data)
frame=bytes(data).split(b'\x1b[K\x1b[J')[-1]
visible=re.sub(rb'\x1b\[[0-9;?<>:]*[a-zA-Z]',b'',frame)
assert visible.startswith(b'> @print("alpha beta \r\n  gamma delta epsilon")'),visible
send('\x1b[D\x1b[D!');command('')
assert sources()==['print("alpha beta gamma delta epsilon!")'],(sources(),bytes(data))
assert output()=='alpha beta gamma delta epsilon!\n',output()
"#);}
    #[test]
    fn e2e_editor_large_paste_word_erase_is_responsive(){pty(r#"
start=time.monotonic();send('\x1b[200~@'+'a'*32768+'\x1b[201~');pump(.1)
send('\x17');command('@print("large-editor-responsive")')
assert time.monotonic()-start<3,(time.monotonic()-start,bytes(data[-3000:]))
assert sources()==['print("large-editor-responsive")'],sources()
assert output()=='large-editor-responsive\n',output()
"#);}
    #[test]
    fn e2e_editor_extended_printable_keys_ctrl_j_and_release(){pty(r#"
send('@print("\x1b[97:65;2u\x1b[946u\x1b[128105u")\x1b[13;2:3u');pump()
assert not sources(),sources()
send('\x1b[106;5u');pump();assert not sources(),sources()
count=data.count(b'\x1b[?2004h');send('print("second")\x1b[13u');wait(lambda:data.count(b'\x1b[?2004h')>count,'reported Enter')
assert sources()==['print("Aβ👩")\nprint("second")'],sources()
assert output()=='Aβ👩\nsecond\n',output()
"#);}
    #[test]
    fn e2e_editor_buffered_typing_repaints_as_a_burst(){pty(r#"
source='value="'+'a'*8192+'"; print(len(value))'
start=time.monotonic();command('@'+source)
assert time.monotonic()-start<3,(time.monotonic()-start,len(data))
assert sources()==[source] and output()=='8192\n',(sources(),output())
# Includes the complete cell preview; repeated entire-buffer repaints would
# produce many megabytes rather than a short prompt and the real source.
assert len(data)<100000,len(data)
"#);}
}

#[cfg(test)]
mod ui_state_e2e{
    use super::*;
    fn fixture(check:&str){
        let script=format!(r#"
import os,sys,tempfile,subprocess,json,glob,threading,queue,time,signal
home=tempfile.mkdtemp(prefix='py-state-');env=dict(os.environ,PY_HOME=home)
for k in ('PY_MODEL','PY_CONTEXT_LIMIT'):env.pop(k,None)
class Client:
 def __init__(self,extra=()):
  self.p=subprocess.Popen([sys.argv[1],'--json','--json-input','--no-model',*extra],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env);self.q=queue.Queue();self.all=[]
  def read():
   for line in self.p.stdout:self.q.put(json.loads(line))
  threading.Thread(target=read,daemon=True).start();self.until('ready')
 def send(self,**v):self.p.stdin.write(json.dumps(v)+'\n');self.p.stdin.flush()
 def until(self,kind):
  end=time.monotonic()+8
  while time.monotonic()<end:
   v=self.q.get(timeout=max(.01,end-time.monotonic()));self.all.append(v)
   if v['kind']==kind:return v
  raise AssertionError((kind,self.all))
 def close(self):
  self.send(id='quit'+str(time.monotonic()),kind='quit');self.p.stdin.close();assert self.p.wait(timeout=8)==0,self.p.stderr.read()
def events():return [json.loads(l) for f in glob.glob(home+'/sessions/*.jsonl') for l in open(f)]
{check}
"#);
        let out=test_command("python3").args(["-c",&script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(out.status.success(),"{}\n{}",String::from_utf8_lossy(&out.stdout),String::from_utf8_lossy(&out.stderr));
    }
    #[test]
    fn e2e_state_numbered_empty_error_and_nested_shell_cells(){fixture(r#"
c=Client();sources=['pass','raise ValueError("fixture")','agent.sh("printf nested")']
for n,source in enumerate(sources):c.send(id='p'+str(n),kind='python',source=source);c.until('completed')
c.send(id='s',kind='shell',command='printf direct');c.until('completed');c.send(id='status',kind='status');status=c.until('status');c.close()
starts=[v['payload'] for v in events() if v['kind']=='cell_start'];ends=[v['payload'] for v in events() if v['kind']=='cell_end']
assert [v['cell'] for v in starts]==[1,2,3,4,5],starts
assert [v['cell'] for v in ends]==[1,2,4,3,5],ends
assert starts[3]['parent_cell']==3 and starts[4]['parent_cell'] is None,starts
assert [v['source_ref'] for v in starts[:3]]==['H.code[0]','H.code[1]','H.code[2]'],starts
assert ends[2]['stdout_ref']=='H.stdout[2]' and ends[3]['stdout_ref']=='H.stdout[3]',ends
for start,command in zip(starts[3:],['printf nested','printf direct']):
 assert eval(start['source_ref'].replace('H.events','journal'),{'journal':events()})==command,start
assert ends[0]['status']=='ok' and ends[1]['status']=='error' and all(v['elapsed_ms']>=0 for v in ends),ends
assert status['state']=='idle' and status['cells']==5,status
assert [v['payload']['source'] for v in events() if v['kind']=='code']==sources
states=[v for v in c.all if v['kind']=='state'];assert states[0]['state']=='idle' and states[-1]['state']=='idle',states
assert all('model' not in v for v in states if v['state']!='thinking'),states
"#);}
    #[test]
    fn e2e_state_input_cancel_and_reset_restore_idle(){fixture(r#"
c=Client();c.send(id='input',kind='python',source='input("name? ")');prompt=c.until('input_prompt')
assert [v['state'] for v in c.all if v['kind']=='state'][-1]=='input',c.all
c.send(id='reply',kind='stdin_reply',prompt_id=prompt['prompt_id'],operation=prompt['operation'],worker_generation=prompt['worker_generation'],action='cancel');c.until('completed')
c.send(id='next',kind='python',source='pass');c.until('completed');c.send(id='reset',kind='reset');c.until('completed');c.send(id='status',kind='status');status=c.until('status');c.close()
ends=[v['payload'] for v in events() if v['kind']=='cell_end'];assert ends[0]['status']=='cancelled' and ends[1]['status']=='ok',ends
assert status['state']=='idle' and status['cells']==2,status
states=[v['state'] for v in c.all if v['kind']=='state'];assert 'input' in states and states[-1]=='idle',states
"#);}
    #[test]
    fn e2e_state_resume_numbering_without_replay_and_new_session(){fixture(r#"
c=Client();c.send(id='first',kind='python',source='print("only once")');c.until('completed');c.close();path=glob.glob(home+'/sessions/*.jsonl')[0]
c=Client(['--session',path]);c.send(id='second',kind='python',source='pass');c.until('completed');c.close()
assert [v['payload']['cell'] for v in events() if v['kind']=='cell_start']==[1,2],events()
assert sum(v['kind']=='code' and v['payload']['source']=='print("only once")' for v in events())==1
c=Client();c.send(id='fresh',kind='python',source='pass');c.until('completed');c.close()
assert sorted(v['payload']['cell'] for v in events() if v['kind']=='cell_start')==[1,1,2],events()
"#);}
    #[test]
    fn e2e_state_provider_thinking_failure_and_login_cancel_are_scoped(){fixture(r#"
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_POST(self):
  self.rfile.read(int(self.headers['Content-Length']));time.sleep(.15);self.send_response(401);self.end_headers();self.wfile.write(b'private provider rejection')
srv=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=srv.serve_forever,daemon=True).start()
open(home+'/config.json','w').write(json.dumps({'providers':{'openai':{'base_url':'http://127.0.0.1:'+str(srv.server_port)+'/v1','models':[{'id':'fixture','api':'openai-responses'}]}}}));env['OPENAI_API_KEY']='fixture-only'
c=Client();c.send(id='invoke',kind='python',source="agent.llm('hello',model='openai/fixture')");c.until('completed')
c.send(id='bad-login',kind='login',provider='anthropic',method='browser');time.sleep(.2);os.kill(c.p.pid,signal.SIGINT);c.until('error')
c.send(id='status',kind='status');status=c.until('status');c.close();srv.shutdown()
states=[v for v in c.all if v['kind']=='state'];thinking=[v for v in states if v['state']=='thinking'];assert thinking and thinking[0]['model']=='openai/fixture',states
assert any(v['state']=='login' for v in states) and states[-1]['state']=='idle' and status['state']=='idle',states
assert not os.path.exists(home+'/auth.json')
"#);}
    #[test]
    fn e2e_state_real_pty_thinking_input_login_and_cancel_restore_idle(){fixture(r#"
import pty,fcntl,termios,select
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
release=threading.Event()
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_POST(self):
  self.rfile.read(int(self.headers['Content-Length']));release.wait(6);self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
  try:self.wfile.write(b'{"output":[{"type":"message","content":[{"type":"output_text","text":"fixture-reply"}]}]}')
  except BrokenPipeError:pass
srv=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=srv.serve_forever,daemon=True).start()
open(home+'/config.json','w').write(json.dumps({'providers':{'openai':{'base_url':'http://127.0.0.1:'+str(srv.server_port)+'/v1','models':[{'id':'fixture','api':'openai-responses'}]}}}));env.update(OPENAI_API_KEY='fixture-only',TERM='xterm',NO_COLOR='1',PY_OAUTH_BROWSER='/bin/false')
m,s=pty.openpty();fcntl.ioctl(s,termios.TIOCSWINSZ,__import__('struct').pack('HHHH',24,90,0,0))
def tty():os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
p=subprocess.Popen([sys.argv[1],'--no-model'],stdin=s,stdout=s,stderr=s,preexec_fn=tty,env=env);os.close(s);data=bytearray()
def wait(text,start=0):
 end=time.monotonic()+6
 while text not in data[start:]:
  assert time.monotonic()<end,(text,data[-3000:])
  if select.select([m],[],[],.04)[0]:data.extend(os.read(m,65536))
def ready(start):wait(b'\x1b[?2004h',start)
try:
 ready(0);assert b'openai/gpt-4.1' not in data,data
 start=len(data);os.write(m,b'@agent.llm("hello",model="openai/fixture")\r');wait(b'\xc2\xb7 thinking \xc2\xb7 openai/fixture',start)
 assert not any(v['kind']=='cell_end' for v in events()),events()
 release.set();ready(start);assert b'cell 1' in data[start:] and b'elapsed' in data[start:] and b'\xc2\xb7 idle' in data[start:],data
 start=len(data);os.write(m,b'@input("state-input? ")\r');wait(b'state-input? ',start);wait(b'\xc2\xb7 input',start)
 os.write(m,b'\x03');ready(start);assert b'cell 2' in data[start:] and b'\xc2\xb7 idle' in data[start:],data
 start=len(data);os.write(m,b'/login openai api-key\r');wait(b'API key (hidden): ',start);assert b'\xc2\xb7 login' in data[start:],data
 os.write(m,b'\x03');ready(start);assert b'\xc2\xb7 idle' in data[start:],data
 assert not os.path.exists(home+'/auth.json')
 release.clear();start=len(data);os.write(m,b'@agent.llm("cancel",model="openai/fixture")\r');wait(b'\xc2\xb7 thinking \xc2\xb7 openai/fixture',start)
 os.kill(p.pid,signal.SIGINT);ready(start);release.set();assert b'\xc2\xb7 idle' in data[start:],data
 start=len(data);os.write(m,b'@pass\r');ready(start);os.write(m,b'/quit\r');assert p.wait(timeout=5)==0
 ends=[v['payload'] for v in events() if v['kind']=='cell_end'];assert [v['cell'] for v in ends]==[1,2,3,4],ends
 assert ends[0]['status']=='ok' and ends[1]['status']=='cancelled' and ends[2]['status']=='cancelled' and ends[3]['status']=='ok',ends
finally:
 release.set();srv.shutdown()
 if p.poll() is None:p.kill();p.wait()
 os.close(m)
"#);}
    #[test]
    fn e2e_state_provider_cancel_is_sticky_even_when_python_catches_it(){fixture(r#"
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
release=threading.Event()
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_POST(self):
  self.rfile.read(int(self.headers['Content-Length']));release.wait(6);self.send_response(200);self.end_headers()
  try:self.wfile.write(b'{"output":[]}')
  except BrokenPipeError:pass
srv=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=srv.serve_forever,daemon=True).start()
open(home+'/config.json','w').write(json.dumps({'providers':{'openai':{'base_url':'http://127.0.0.1:'+str(srv.server_port)+'/v1','models':[{'id':'fixture','api':'openai-responses'}]}}}));env['OPENAI_API_KEY']='fixture-only'
c=Client();source='try:\n agent.llm("pause",model="openai/fixture")\nexcept KeyboardInterrupt:\n print("caught")\ntry:\n agent.sh("printf forbidden")\nexcept KeyboardInterrupt:\n print("sticky")'
c.send(id='work',kind='python',source=source)
while c.until('state')['state']!='thinking':pass
c.send(id='cancel',kind='interrupt');done=c.until('completed')
while done.get('command_id')!='work':done=c.until('completed')
assert done['status']=='cancelled',done
assert not any(v['kind']=='intent' and v['payload']['type']=='shell' for v in events()),events()
c.send(id='after',kind='python',source='print("after")');assert c.until('completed')['status']=='ok';c.close();release.set();srv.shutdown()
text=''.join(v['payload'].get('text','') for v in events() if v['kind']=='stream' and v['payload']['collection']=='stdout')
assert text=='caught\nsticky\nafter\n',text
"#);}
    #[test]
    fn e2e_state_worker_crash_and_prestart_shell_error_get_cell_end(){fixture(r#"
c=Client();c.send(id='crash',kind='python',source='import os; os._exit(7)');c.until('completed')
c.send(id='bad-shell',kind='shell',command='printf never',options={'cwd':home+'/no-such-dir'});c.until('error')
c.send(id='next',kind='python',source='pass');c.until('completed');c.send(id='status',kind='status');status=c.until('status');c.close()
ends=[v['payload'] for v in events() if v['kind']=='cell_end'];assert [v['cell'] for v in ends]==[1,2,3],ends
assert [v['status'] for v in ends]==['worker_crashed','error','ok'],ends
assert status['state']=='idle' and status['cells']==3,status
"#);}
    #[test]
    fn e2e_state_human_cell_end_status_refs_and_wrap(){fixture(r#"
env['COLUMNS']='39'
out=subprocess.run([sys.argv[1],'--no-model'],input='@pass\n@raise ValueError("fixture")\n!printf shell\n/quit\n',text=True,capture_output=True,env=env,timeout=12)
assert out.returncode==0,(out.stdout,out.stderr)
assert 'cell 1' in out.stdout and 'cell 2' in out.stdout and 'cell 3' in out.stdout,out.stdout
assert 'elapsed' in out.stdout and 'H.code[0]' in out.stdout and 'H.stdout[' in out.stdout,out.stdout
assert all(len(l)<=39 for l in (out.stdout+out.stderr).splitlines()),out.stdout
"#);}
}

#[cfg(test)]
mod picker_boundary_e2e{
    fn fixture(body:&str){
        let setup=r#"
import os,sys,pty,fcntl,termios,struct,subprocess,select,time,json,tempfile,glob
home=tempfile.mkdtemp(prefix='py-picker-boundary-')
m,s=pty.openpty();fcntl.ioctl(s,termios.TIOCSWINSZ,struct.pack('HHHH',24,80,0,0))
def controlling():os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
env=dict(os.environ,PY_HOME=home,PY_MODEL='openai/gpt-5',TERM='xterm-256color',NO_COLOR='1')
p=subprocess.Popen([sys.argv[1],'--no-model'],stdin=s,stdout=s,stderr=s,env=env,preexec_fn=controlling);os.close(s);data=bytearray()
def pump():
 if select.select([m],[],[],.02)[0]:
  try:data.extend(os.read(m,65536))
  except OSError:pass
 return bytes(data)
def wait(f,label):
 end=time.monotonic()+6
 while time.monotonic()<end:
  pump()
  if f():return
  if p.poll() is not None:raise AssertionError((label,p.returncode,bytes(data)))
 raise AssertionError((label,bytes(data)))
def send(v):os.write(m,v.encode() if isinstance(v,str) else v)
def records():
 return [json.loads(l) for path in glob.glob(home+'/sessions/*.jsonl') for l in open(path) if l.endswith('\n')]
def menu(line):
 start=len(data);send(line+'\t');wait(lambda:b'Fuzzy select' in data[start:],'picker opens');return start
def paste(text):send(b'\x1b[200~'+(text.encode() if isinstance(text,str) else text)+b'\x1b[201~')
def command(line):
 count=data.count(b'\x1b[?2004h');send(line+'\r');wait(lambda:data.count(b'\x1b[?2004h')>count,'next editor')
wait(lambda:b'\x1b[?2004h' in data,'editor ready')
try:
"#;
        let teardown=r#"
 send('/quit\r');p.wait(timeout=3);assert p.returncode==0,data
finally:
 if p.poll() is None:p.kill();p.wait()
 os.close(m)
"#;
        let script=format!("{setup}{}{teardown}",body.lines().map(|l|format!(" {l}\n")).collect::<String>());
        let output=crate::test_command("python3").args(["-c",&script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(output.status.success(),"{}\n{}",String::from_utf8_lossy(&output.stderr),String::from_utf8_lossy(&output.stdout));
    }
    #[test]
    fn e2e_picker_bracketed_paste_never_selects_or_submits(){fixture(r#"
start=menu('/mo')
paste('del\r\r@print("paste-must-not-run")\r\x1b[A\x03\x04')
time.sleep(.15);pump()
assert b'\x1b[0J' not in data[start:],('paste selected/cancelled picker',bytes(data[start:]))
assert b'openai/gpt-5' not in data[start:],('paste dispatched /model',bytes(data[start:]))
assert not any(v['kind'] in ('cell_start','code','user','settings_change') for v in records()),records()
send('\x1b');wait(lambda:b'\x1b[0J' in data[start:],'Escape restores original')
command('del')
assert b'openai/gpt-5' in data[start:],('original /mo input was not restored',bytes(data[start:]))
assert not any(v['kind']=='code' for v in records()),records()
"#);}
    #[test]
    fn e2e_picker_pasted_query_requires_separate_physical_selection_and_submit(){fixture(r#"
start=menu('/mo');send('\x15');paste('model\r\n\t')
time.sleep(.12);pump()
assert b'\x1b[0J' not in data[start:],('pasted newline selected',bytes(data[start:]))
send('\r');wait(lambda:b'\x1b[0J' in data[start:],'physical Enter selects')
assert b'openai/gpt-5' not in data[start:],('selection submitted',bytes(data[start:]))
command('');assert b'openai/gpt-5' in data[start:],bytes(data[start:])
start=menu('/mo');send('\x15');paste(b'model\xff\r')
time.sleep(.1);pump();assert b'\x1b[0J' not in data[start:],bytes(data[start:])
send('\x15model\r');wait(lambda:b'\x1b[0J' in data[start:],'invalid UTF8 recoverable')
command('')
"#);}
    #[test]
    fn e2e_picker_oversized_paste_is_discarded_and_recoverable(){fixture(r#"
start=menu('/mo');paste(b'model '+b'x'*1_048_577+b'\r\r')
time.sleep(.1);pump()
assert b'\x1b[0J' not in data[start:],('oversized paste selected',bytes(data[start:]))
assert b'openai/gpt-5' not in data[start:],bytes(data[start:])
send('del\r');wait(lambda:b'\x1b[0J' in data[start:],'oversized paste left original query unchanged')
command('');assert b'openai/gpt-5' in data[start:],bytes(data[start:])
"#);}
    #[test]
    fn e2e_picker_login_methods_resolve_fuzzy_provider(){fixture(r#"
start=len(data);send('/login anthr b\t')
wait(lambda:b'/login anthr browser' in data[start:],'fuzzy provider browser argument completion')
assert b'Fuzzy select' not in data[start:],bytes(data[start:])
send('\x15');command('/help')
start=menu('/login anthr ');send('man\r');wait(lambda:b'\x1b[0J' in data[start:],'fuzzy provider manual method selected')
wait(lambda:b'/login anthr manual' in data[start:],'selected method inserted without submission')
send('\x15');command('/help')
for command_name in ('/logout','/auth'):
 start=len(data);send(command_name+' anthropic \t');time.sleep(.12);pump()
 assert b'Fuzzy select' not in data[start:],('non-login command offered login methods',bytes(data[start:]))
 send('\x15');command('/help')
assert not os.path.exists(home+'/auth.json')
"#);}
}

#[cfg(test)]
mod browser_boundary_e2e{
    fn check(body:&str){
        let script=format!(r#"
import os,sys,tempfile,json,subprocess,threading,queue,time,base64,urllib.parse,http.client,http.server,signal,glob,fcntl
home=tempfile.mkdtemp(prefix='py-browser-boundary-');env=dict(os.environ,PY_HOME=home,NO_COLOR='1',PY_OAUTH_BROWSER='/bin/false',PY_OAUTH_TIMEOUT_SECONDS='1',PY_OAUTH_CALLBACK_PORT='0')
for k in ('PY_MODEL','PY_CONTEXT_LIMIT'):env.pop(k,None)
claims={{'https://api.openai.com/auth':{{'chatgpt_account_id':'fixture-account'}}}}
access='x.'+base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')+'.z'
response={{'access_token':access,'refresh_token':'fixture-refresh-SECRET','expires_in':3600}};status=200;seen=[];entered=threading.Event();release=threading.Event();stall=False
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  raw=self.rfile.read(int(self.headers.get('Content-Length','0'))).decode();seen.append((self.path,raw));entered.set()
  if stall:release.wait(8)
  self.send_response(status);self.send_header('Content-Type','application/json');self.end_headers()
  try:self.wfile.write(json.dumps(response).encode())
  except (BrokenPipeError,ConnectionResetError):pass
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start();base='http://127.0.0.1:'+str(server.server_port)
env.update(PY_CODEX_AUTH_BASE_URL=base,PY_ANTHROPIC_AUTH_BASE_URL=base,PY_ANTHROPIC_AUTHORIZE_URL=base+'/authorize')
def events():return [json.loads(l) for f in glob.glob(home+'/sessions/*.jsonl') for l in open(f)]
def auth(value):open(home+'/auth.json','w').write(json.dumps(value));os.chmod(home+'/auth.json',0o600)
def config(provider,api):open(home+'/config.json','w').write(json.dumps({{'providers':{{provider:{{'base_url':base,'models':[{{'id':'fixture','api':api}}]}}}}}}))
class Client:
 def __init__(self):
  self.p=subprocess.Popen([sys.argv[1],'--json','--json-input','--no-model'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env);self.q=queue.Queue();self.all=[]
  def read():
   for line in self.p.stdout:self.q.put(json.loads(line))
  threading.Thread(target=read,daemon=True).start();self.until('ready')
 def send(self,**v):self.p.stdin.write(json.dumps(v)+'\n');self.p.stdin.flush()
 def until(self,kind,id=None,timeout=5):
  end=time.monotonic()+timeout
  while time.monotonic()<end:
   v=self.q.get(timeout=max(.01,end-time.monotonic()));self.all.append(v)
   if v['kind']==kind and (id is None or v.get('command_id')==id):return v
  raise AssertionError((kind,id,self.all))
 def drain(self,seconds=.15):
  end=time.monotonic()+seconds
  while time.monotonic()<end:
   try:self.all.append(self.q.get(timeout=max(.01,end-time.monotonic())))
   except queue.Empty:break
 def close(self):
  self.p.stdin.close()
  try:self.p.wait(timeout=5)
  except subprocess.TimeoutExpired:self.p.kill();self.p.wait()
  assert self.p.returncode==0,(self.all,self.p.stderr.read())
def callback(url,path=None,**params):
 values=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query);redirect=urllib.parse.urlsplit(values['redirect_uri'][0]);params.setdefault('state',values['state'][0]);params.setdefault('code','fixture-code-SECRET')
 conn=http.client.HTTPConnection(redirect.hostname,redirect.port,timeout=2);conn.request('GET',(path or redirect.path)+'?'+urllib.parse.urlencode(params));res=conn.getresponse();code=res.status;res.read();conn.close();return code
{body}
release.set();server.shutdown()
"#);
        let output=crate::test_command("python3").args(["-c",&script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(output.status.success(),"{}\n{}",String::from_utf8_lossy(&output.stdout),String::from_utf8_lossy(&output.stderr));
    }
    #[test]
    fn e2e_anthropic_interrupt_requires_unique_nonempty_command_id(){check(r#"
config('anthropic','anthropic-messages');auth({'anthropic':{'type':'api_key','key':'fixture-key'}});stall=True;response={'content':[{'type':'text','text':'fixture-answer'}]}
c=Client();c.send(id='work',kind='python',source="agent.llm('hello',model='anthropic/fixture')");assert entered.wait(3)
c.send(id='work',kind='interrupt');assert c.until('rejected','work')['error']=='missing or duplicate command ID',c.all
c.send(kind='interrupt');assert c.until('rejected','')['error']=='missing or duplicate command ID',c.all
c.drain();assert not any(v['kind']=='completed' for v in c.all),c.all
c.send(id='cancel',kind='interrupt');assert c.until('completed','cancel')['status']=='ok';assert c.until('completed','work')['status']=='cancelled'
release.set();c.send(id='after',kind='python',source='print("still-usable")');assert c.until('completed','after')['status']=='ok';c.send(id='status',kind='status');assert c.until('status')['state']=='idle';c.close()
accepted=[v['payload']['command_id'] for v in events() if v['kind']=='accepted'];assert accepted.count('work')==1 and accepted.count('cancel')==1 and '' not in accepted,accepted
assert not any(v['kind']=='completed' and v.get('command_id')=='work' and v.get('status')=='ok' for v in c.all),c.all
"#);}
    #[test]
    fn e2e_codex_malformed_refresh_and_loaded_tokens_leave_store_unchanged(){check(r#"
config('openai-codex','openai-codex-responses')
old={'openai-codex':{'type':'oauth','access':access,'refresh':'old-refresh-SECRET','accountId':'fixture-account','expires':1},'anthropic':{'type':'api_key','key':'keep-secret'}}
for response in [dict(access_token=access+'\r\nTOKEN-SECRET',refresh_token='next-refresh',expires_in=3600),dict(access_token=access,refresh_token='bad refresh-SECRET',expires_in=3600),dict(access_token=access,refresh_token='X'*32769,expires_in=3600)]:
 auth(old);c=Client();c.send(id='bad-refresh',kind='python',source="agent.llm('hello',model='codex/fixture')");assert c.until('completed','bad-refresh')['status']=='error';c.close();assert json.load(open(home+'/auth.json'))==old,events()
for field,value in [('access',access+'\r\nTOKEN-SECRET'),('refresh','bad refresh-SECRET'),('accountId','bad\r\nACCOUNT-SECRET')]:
 loaded=json.loads(json.dumps(old));loaded['openai-codex']['expires']=int(time.time()*1000)+100000;loaded['openai-codex'][field]=value;auth(loaded);before=len(seen)
 c=Client();c.send(id='bad-loaded',kind='python',source="agent.llm('hello',model='codex/fixture')");assert c.until('completed','bad-loaded')['status']=='error';c.close();assert len(seen)==before,(field,seen);assert json.load(open(home+'/auth.json'))==loaded
history=''.join(json.dumps(v) for v in events())
for secret in ['TOKEN-SECRET','refresh-SECRET','ACCOUNT-SECRET']:assert secret not in history,history
"#);}
    #[test]
    fn e2e_browser_commit_lock_inherits_timeout_and_cancel(){check(r#"
old={'anthropic':{'type':'api_key','key':'keep-secret'}}
for mode in ('timeout','cancel'):
 auth(old);c=Client();c.send(id='browser-'+mode,kind='login',provider='codex',method='browser');url=c.until('login_prompt')['url']
 lock=open(home+'/auth.lock','a+');fcntl.flock(lock,fcntl.LOCK_EX);before=len(seen);assert callback(url)==200
 end=time.monotonic()+3
 while len(seen)==before:
  assert time.monotonic()<end,seen;time.sleep(.01)
 if mode=='cancel':c.send(id='cancel',kind='interrupt');assert c.until('completed','cancel')['status']=='ok'
 error=c.until('error','browser-'+mode,timeout=3);assert ('timed out' if mode=='timeout' else 'cancelled') in error['error'],error
 assert 'SECRET' not in json.dumps(error) and json.load(open(home+'/auth.json'))==old,error
 fcntl.flock(lock,fcntl.LOCK_UN);lock.close();c.send(id='after',kind='python',source='pass');assert c.until('completed','after')['status']=='ok';c.send(id='status',kind='status');assert c.until('status')['state']=='idle';c.close();assert json.load(open(home+'/auth.json'))==old
"#);}
    #[test]
    fn e2e_browser_wrong_path_denial_does_not_consume_valid_callback(){check(r#"
c=Client();c.send(id='browser',kind='login',provider='codex',method='browser');url=c.until('login_prompt')['url'];assert callback(url,path='/wrong',error='access_denied')==404
assert callback(url)==200;assert c.until('completed','browser')['status']=='ok';c.close();assert len(seen)==1 and json.load(open(home+'/auth.json'))['openai-codex']['accountId']=='fixture-account',seen
"#);}
}

#[cfg(test)]
mod styled_e2e{
    fn check(body:&str){
        let script=format!(r#"
import os,sys,subprocess,tempfile,pty,fcntl,termios,struct,select,time,json,re,glob,threading,http.server
home=tempfile.mkdtemp(prefix='py-style-');env=dict(os.environ,PY_HOME=home,TERM='xterm-256color',COLUMNS='48')
env.pop('NO_COLOR',None)
def plain(text):return re.sub(r'\x1b\[[0-9;]*m','',text)
def setup():
 master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',24,48,0,0));return master,slave
def controlling():os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
def drain(p,master,until=None):
 buf=bytearray();end=time.monotonic()+7
 while time.monotonic()<end:
  if select.select([master],[],[],.04)[0]:
   try:buf.extend(os.read(master,65536))
   except OSError:break
   if until and until in buf:break
  elif p.poll() is not None:break
 else:raise AssertionError(('timeout',buf.decode(errors='replace')))
 return buf.decode(errors='replace')
def human(source,extraenv=None):
 master,slave=setup();p=subprocess.Popen([sys.argv[1],'--json-input','--no-model'],stdin=subprocess.PIPE,stdout=slave,stderr=subprocess.PIPE,env=dict(env,**(extraenv or {{}})));os.close(slave)
 p.stdin.write((json.dumps(dict(id='style',kind='python',source=source))+'\n').encode());p.stdin.close();out=drain(p,master);assert p.wait(timeout=3)==0,p.stderr.read();os.close(master);return out
{body}
"#);
        let out=crate::test_command("python3").args(["-c",&script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(out.status.success(),"{}\n{}",String::from_utf8_lossy(&out.stdout),String::from_utf8_lossy(&out.stderr));
    }
    #[test]
    fn e2e_styled_markdown_cells_status_and_plain_fallback(){check(r#"
text='# Styled heading\n\n```python\nprint(42)\n```\n\n| A | B |\n| --- | --- |\n| 界 é | alpha beta gamma delta epsilon |\n\ncontrol \x1b]52;c;DANGER\x07'
source='agent.say('+repr(text)+')'
colored=human(source);unstyled=human(source,{'NO_COLOR':''});dumb=human(source,{'TERM':'dumb'})
assert '\x1b[1;36m' in colored and '\x1b[32m' in colored,colored
assert '\x1b' not in unstyled and '\x1b' not in dumb,(unstyled,dumb)
visible=plain(colored)
assert visible.count('── cell 1 · python')==1 and '── cell style' not in visible,visible
assert 'cell 1 · ok · elapsed' in visible and 'H.stdout[0]' in visible and 'H.stderr[0]' in visible,visible
assert '\x1b]52;' not in colored and '\x07' not in colored,colored
assert all(len(re.sub('[界]','xx',line))<=48 for line in visible.splitlines()),visible
session=glob.glob(home+'/sessions/*.jsonl')[0];events=[json.loads(l) for l in open(session)]
assert next(v for v in events if v['kind']=='say')['payload']['text']==text,events
p=subprocess.run([sys.argv[1],'--json-input','--no-model'],input=json.dumps(dict(id='plain',kind='python',source='pass'))+'\n',text=True,capture_output=True,env=env,timeout=7)
assert p.returncode==0 and '\x1b' not in p.stdout,p
"#);}
    #[test]
    fn e2e_styled_minimal_prompt_and_aligned_continuation(){check(r#"
master,slave=setup();p=subprocess.Popen([sys.argv[1],'--no-model'],stdin=slave,stdout=slave,stderr=slave,env=env,preexec_fn=controlling);os.close(slave)
try:
 first=drain(p,master,b'\x1b[?2004h');assert '\x1b[1;36m> \x1b[0m' in first and 'gpt-4.1' not in first and 'ctx=' not in first,first
 os.write(master,b'@x=1\x1b[13;2ux+=1');edited=drain(p,master,b'x+=1');assert '\r\n  x+=1' in edited,edited
 os.write(master,b'\r');done=drain(p,master,b'\x1b[?2004h');assert 'cell 1' in plain(done) and '\x1b[32m' in done,done
 os.write(master,b'/mo\t');menu=drain(p,master,b'\x1b[1;7m');assert '\x1b[1;36mFuzzy select' in menu and '\x1b[1;7m' in menu,menu
 os.write(master,b'\x1b');drain(p,master,b'\x1b[>1u');os.write(master,b'\x15/quit\r');drain(p,master);assert p.wait(timeout=3)==0
finally:
 if p.poll() is None:p.kill();p.wait()
 os.close(master)
"#);}
    #[test]
    fn e2e_json_output_tty_editor_and_slash_commands_never_mix_ui(){check(r#"
master,slave=setup();p=subprocess.Popen([sys.argv[1],'--json','--no-model'],stdin=slave,stdout=subprocess.PIPE,stderr=slave,env=env,preexec_fn=controlling);os.close(slave)
try:
 first=drain(p,master,b'\x1b[?2004h');assert '> ' in plain(first),first
 for line in (b'/model\r',b'/help\r',b'/login\r',b'/models haiku\r',b'@print("json-safe")\r'):
  os.write(master,line);drain(p,master,b'\x1b[?2004h')
 fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',24,24,0,0));os.write(master,b'@value="alpha beta gamma delta epsilon zeta"')
 wrapped=drain(p,master,b'zeta');assert '\r\n  ' in wrapped,wrapped
 os.write(master,b'\r');drain(p,master,b'\x1b[?2004h');os.write(master,b'/quit\r');drain(p,master);assert p.wait(timeout=3)==0
 raw=p.stdout.read();assert b'\x1b' not in raw,raw
 events=[json.loads(l) for l in raw.splitlines()];assert any(v['kind']=='completed' for v in events),events
 assert any(v['kind']=='notice' and 'Login:' in v.get('text','') for v in events),events
 assert any(v['kind']=='say' and 'Models' in v.get('text','') for v in events),events
 assert any(v['kind']=='state' and v['state']=='idle' for v in events),events
finally:
 if p.poll() is None:p.kill();p.wait()
 os.close(master)
"#);}
    #[test]
    fn e2e_login_unknown_method_never_echoes_or_journals_accidental_secret(){check(r#"
secret='accidental-fixture-secret-in-method'
p=subprocess.run([sys.argv[1],'--no-model'],input='/login openai '+secret+'\n/quit\n',text=True,capture_output=True,env=env,timeout=7)
assert p.returncode==0 and secret not in p.stdout+p.stderr,(p.stdout,p.stderr)
for queued in (False,True):
 commands=([dict(id='hold',kind='python',source='import time;time.sleep(.1)')] if queued else [])+[dict(id='login',kind='login',provider='openai',method=secret,pasted_code='code-SECRET'),dict(id='after',kind='python',source='pass')]
 p=subprocess.run([sys.argv[1],'--json','--json-input','--no-model'],input=''.join(json.dumps(c)+'\n' for c in commands),text=True,capture_output=True,env=env,timeout=7)
 assert p.returncode==0 and secret not in p.stdout+p.stderr,(p.stdout,p.stderr)
 assert any(v['kind']=='completed' and v.get('command_id')=='after' for v in map(json.loads,p.stdout.splitlines())),p.stdout
history=''.join(open(f).read() for f in glob.glob(home+'/sessions/*.jsonl'))
assert secret not in history and 'code-SECRET' not in history,history
assert secret not in open(home+'/editor-history').read()
"#);}
    #[test]
    fn e2e_json_tty_input_without_ui_terminal_has_no_generated_escapes(){check(r#"
master,slave=setup();p=subprocess.Popen([sys.argv[1],'--json','--no-model'],stdin=slave,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,preexec_fn=controlling)
try:
 end=time.monotonic()+4
 while termios.tcgetattr(slave)[3]&(termios.ECHO|termios.ICANON):
  assert time.monotonic()<end,'JSON editor never entered non-echoed input mode';time.sleep(.01)
 os.write(master,b'@print("quiet-json")\r/quit\r');out,err=p.communicate(timeout=7);assert p.returncode==0,(out,err)
 assert err==b'' and b'\x1b' not in out,(out,err)
 events=[json.loads(l) for l in out.splitlines()];assert any(v['kind']=='completed' and v['status']=='ok' for v in events),events
 assert termios.tcgetattr(slave)[3]&(termios.ECHO|termios.ICANON)==(termios.ECHO|termios.ICANON),'termios not restored'
finally:
 if p.poll() is None:p.kill();p.wait()
 os.close(master);os.close(slave)
"#);}
    #[test]
    fn e2e_styled_thinking_shows_model_only_in_thinking_status(){check(r#"
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_POST(self):
  self.rfile.read(int(self.headers.get('Content-Length','0')));time.sleep(.05);data=json.dumps({'output':[{'type':'message','content':[{'type':'output_text','text':'fixture-answer'}]}]}).encode();self.send_response(200);self.end_headers();self.wfile.write(data)
srv=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=srv.serve_forever,daemon=True).start()
open(home+'/config.json','w').write(json.dumps({'providers':{'openai':{'base_url':'http://127.0.0.1:'+str(srv.server_port)+'/v1','models':[{'id':'fixture','api':'openai-responses'}]}}}));env['OPENAI_API_KEY']='fixture-only'
out=human("agent.llm('hello',model='openai/fixture')");visible=plain(out)
assert '\x1b[35m' in out and '· thinking · openai/fixture [medium]' in visible,visible
assert all('openai/fixture' not in line for line in visible.splitlines() if line.startswith('·') and 'thinking' not in line),visible
assert visible.rstrip().endswith('· idle'),visible
srv.shutdown()
"#);}
}

#[cfg(test)]
mod inline_picker_e2e {
    fn fixture(body:&str){
        let setup=r#"
import os,sys,pty,fcntl,termios,struct,subprocess,select,time,json,tempfile,re,glob,unicodedata,codecs
home=tempfile.mkdtemp(prefix='py-inline-picker-');env=dict(os.environ,PY_HOME=home,TERM='xterm-256color',NO_COLOR='1')
class Screen:
 def __init__(self,rows=24,cols=80):
  self.rows=rows;self.cols=cols;self.r=0;self.c=0;self.grid=[[' ']*cols for _ in range(rows)];self.pending='';self.decoder=codecs.getincrementaldecoder('utf8')('replace')
 def resize(self,rows,cols):
  self.grid=[(line+[' ']*cols)[:cols] for line in self.grid[:rows]]+[ [' ']*cols for _ in range(max(0,rows-len(self.grid))) ];self.rows=rows;self.cols=cols;self.r=min(self.r,rows-1);self.c=min(self.c,cols-1)
 def lf(self):
  self.r+=1
  if self.r==self.rows:self.grid.pop(0);self.grid.append([' ']*self.cols);self.r-=1
 def feed(self,data):
  self.pending+=self.decoder.decode(data)
  while self.pending:
   s=self.pending
   if s[0]=='\x1b':
    if len(s)<2:break
    if s[1]=='[':
     match=re.match(r'\x1b\[([0-9;:?<>=]*)([ -/]*)([@-~])',s)
     if match is None:break
     raw,_,cmd=match.groups();self.pending=s[match.end():]
     if raw.startswith(('?','>','<','=')):continue
     values=[int(x or 0) for x in raw.split(';')] if ':' not in raw else [0];n=values[0] or 1
     if cmd=='A':self.r=max(0,self.r-n)
     elif cmd=='B':self.r=min(self.rows-1,self.r+n)
     elif cmd=='C':self.c=min(self.cols-1,self.c+n)
     elif cmd=='D':self.c=max(0,self.c-n)
     elif cmd=='G':self.c=min(self.cols-1,n-1)
     elif cmd in ('H','f'):self.r=min(self.rows-1,n-1);self.c=min(self.cols-1,(values[1] or 1)-1 if len(values)>1 else 0)
     elif cmd=='K':
      mode=values[0];start,end=(0,self.cols) if mode==2 else (0,self.c+1) if mode==1 else (self.c,self.cols)
      self.grid[self.r][start:end]=[' ']*(end-start)
     elif cmd=='J':
      mode=values[0]
      if mode==2:self.grid=[[' ']*self.cols for _ in range(self.rows)]
      elif mode==0:
       self.grid[self.r][self.c:]=[' ']*(self.cols-self.c)
       for row in range(self.r+1,self.rows):self.grid[row]=[' ']*self.cols
     continue
    self.pending=s[2:];continue
   self.pending=s[1:];c=s[0]
   if c=='\r':self.c=0
   elif c=='\n':self.lf()
   elif c=='\b':self.c=max(0,self.c-1)
   elif c>=' ':
    width=0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W','F') else 1
    if width==0:continue
    if self.c+width>self.cols:self.c=0;self.lf()
    self.grid[self.r][self.c]=c
    if width==2 and self.c+1<self.cols:self.grid[self.r][self.c+1]=''
    self.c+=width
 def text(self):return '\n'.join(''.join(row).rstrip() for row in self.grid)
screen=Screen();m,s=pty.openpty();fcntl.ioctl(s,termios.TIOCSWINSZ,struct.pack('HHHH',24,80,0,0))
def controlling():os.setsid();fcntl.ioctl(0,termios.TIOCSCTTY,0)
json_mode=os.environ.get('INLINE_JSON_FIXTURE')=='1'
p=subprocess.Popen([sys.argv[1],'--no-model',*(['--json'] if json_mode else [])],stdin=s,stdout=subprocess.PIPE if json_mode else s,stderr=s,env=env,preexec_fn=controlling);os.close(s);data=bytearray()
def pump():
 if select.select([m],[],[],.03)[0]:
  try:chunk=os.read(m,65536);data.extend(chunk);screen.feed(chunk)
  except OSError:pass
 return screen.text()
def wait(predicate,label):
 end=time.monotonic()+6
 while time.monotonic()<end:
  pump()
  if predicate():return
  if p.poll() is not None:break
 raise AssertionError((label,screen.text(),bytes(data[-7000:])))
def send(value):os.write(m,value.encode() if isinstance(value,str) else value)
def command(value):
 count=data.count(b'\x1b[?2004h');send(value+'\r');wait(lambda:data.count(b'\x1b[?2004h')>count,'next editor')
def records():return [json.loads(l) for f in glob.glob(home+'/sessions/*.jsonl') for l in open(f) if l.endswith('\n')]
def sources():return [v['payload']['source'] for v in records() if v['kind']=='code']
def menu(value):
 start=len(data);send(value+'\t');wait(lambda:'Fuzzy select' in screen.text(),'menu visible')
 assert b'\x1b[?1049' not in data[start:] and b'\x1b[2J' not in data[start:] and b'\x1b[H' not in data[start:],bytes(data[start:])
 return start
def close_picker(value):
 start=len(data);send(value)
 def repainted():
  segment=bytes(data[start:]).rsplit(b'\x1b[0J',1)
  return len(segment)==2 and b'\x1b[K\x1b[J' in segment[1] and re.search(rb'\x1b\[[0-9]+C(?:\x1b\[\?25h)?$',segment[1]) and 'Fuzzy select' not in screen.text()
 wait(repainted,'picker closed and source repainted')
wait(lambda:b'\x1b[?2004h' in data,'ready')
try:
"#;
        let end=r#"
 send('/quit\r');p.wait(timeout=4)
 assert p.returncode==0,bytes(data)
 if json_mode:
  output=p.stdout.read();assert b'\x1b' not in output,output
  events=[json.loads(line) for line in output.splitlines()];assert any(v['kind']=='say' for v in events),events
finally:
 if p.poll() is None:p.kill();p.wait()
 os.close(m)
"#;
        let mut command=crate::test_command("python3");
        let json_mode=body.contains("# JSON");
        if json_mode{command.env("INLINE_JSON_FIXTURE","1");}else{command.env_remove("INLINE_JSON_FIXTURE");}
        let script=format!("{setup}{}{end}",body.lines().map(|l|format!(" {l}\n")).collect::<String>());
        let out=command.args(["-c",&script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(out.status.success(),"{}\n{}",String::from_utf8_lossy(&out.stdout),String::from_utf8_lossy(&out.stderr));
    }
    #[test]
    fn e2e_inline_prompt_output_and_selection_cancel_coexist(){fixture(r#"
command('@print("inline_keep")')
menu('/mod');visible=screen.text();assert 'inline_keep' in visible,visible
assert visible.index('> /mod')<visible.index('Fuzzy select')<visible.index('Find:'),visible
count=len(sources());close_picker('el\r');assert len(sources())==count and '> /model' in screen.text(),(sources(),screen.text())
assert '/models' not in screen.text(),screen.text()
send('\x15');menu('/mod');close_picker('discard\x1b');assert '> /mod' in screen.text(),screen.text()
command('els');assert len(sources())==count,sources()
"#);}
    #[test]
    fn e2e_inline_multiline_middle_caret_select_and_cancel(){fixture(r#"
command('@inline_alpha=11;inline_beta=22')
send('\x1b[200~@answer=(\ninline_)\nprint(answer)\x1b[201~');send('\x01\x1b[A\x1b[F\x1b[D')
menu('');visible=screen.text();assert visible.index('> @answer=(')<visible.index('  inline_)')<visible.index('  print(answer)')<visible.index('Fuzzy select'),visible
close_picker('bt\r');assert '  inline_beta)' in screen.text(),screen.text()
assert ''.join(screen.grid[screen.r]).startswith('  inline_beta)') and screen.c==13,(screen.r,screen.c,screen.text())
command('.real');assert 'answer=(\ninline_beta.real)\nprint(answer)' in sources(),sources()
send('\x1b[200~@answer=(\ninline_)\nprint(answer)\x1b[201~');send('\x01\x1b[A\x1b[F\x1b[D');menu('')
close_picker('discard\x1b');assert ''.join(screen.grid[screen.r]).startswith('  inline_)') and screen.c==9,(screen.r,screen.c,screen.text())
command('alpha');assert 'answer=(\ninline_alpha)\nprint(answer)' in sources() and all('discard' not in s for s in sources()),sources()
stdout=''.join(v['payload']['text'] for v in records() if v['kind']=='stream' and v['payload']['collection']=='stdout');assert stdout=='22\n11\n',stdout
"#);}
    #[test]
    fn e2e_inline_bottom_narrow_resize_and_repeated_close(){fixture(r#"
fcntl.ioctl(m,termios.TIOCSWINSZ,struct.pack('HHHH',6,32,0,0));screen.resize(6,32)
command('@print("bottom-edge")')
menu('/');assert '> /' in screen.text() and '· idle' in screen.text(),screen.text()
fcntl.ioctl(m,termios.TIOCSWINSZ,struct.pack('HHHH',8,22,0,0));screen.resize(8,22)
send('model');wait(lambda:'Find: /model' in screen.text(),'resize/filter');assert '> /' in screen.text(),screen.text()
close_picker('\r');assert '> /model' in screen.text(),screen.text();send('\x15')
for _ in range(4):
 menu('/');close_picker('\x03');assert '> /' in screen.text(),screen.text();send('\x15')
command('@print("after-inline")');assert any(v['kind']=='stream' and 'after-inline' in v['payload']['text'] for v in records()),records()
"#);}
    #[test]
    fn e2e_inline_tiny_terminal_declines_without_losing_input(){fixture(r#"
fcntl.ioctl(m,termios.TIOCSWINSZ,struct.pack('HHHH',2,32,0,0));screen.resize(2,32)
start=len(data);send('/mod\t');wait(lambda:b'\x1b[?25h' in data[start:],'completion handled')
assert '> /mod' in screen.text() and 'Fuzzy select' not in screen.text(),screen.text()
assert b'\x1b[?1049' not in data[start:] and b'\x1b[2J' not in data[start:],bytes(data[start:])
command('els')
"#);}
    #[test]
    fn e2e_inline_json_menu_uses_ui_terminal_not_stdout(){fixture(r#"
# JSON
menu('/mo');assert '> /mo' in screen.text(),screen.text();close_picker('dels\r');assert '> /models' in screen.text(),screen.text()
command('');assert not any(v['kind']=='code' for v in records()),records()
"#);}
}


// Session-owned isolated processes. All capture/journal mutation remains on Host's thread.
struct BgTask {
    child:Option<Child>, stdout:Option<Capture>, stderr:Option<Capture>, dir:PathBuf,
    metadata:Value, deadline:Option<std::time::Instant>,
    cancel_at:Option<std::time::Instant>, terminal_status:Option<String>,
    reaped:Option<std::process::ExitStatus>,
}
impl Drop for BgTask {
    fn drop(&mut self){
        if let Some(mut child)=self.child.take(){
            if self.reaped.is_none(){
                unsafe{libc::kill(-(child.id() as i32),libc::SIGKILL);}
                let _=child.kill();let _=child.wait();
            }
        }
        if !self.dir.as_os_str().is_empty(){let _=fs::remove_dir_all(&self.dir);}
    }
}
struct Wakeup { metadata:Value, deadline:Option<std::time::Instant> }
impl Host {
    fn commit_stop(&mut self)->Result<()> {
        if let Some((seconds,reason))=self.stop_wakeup.take(){
            // One durable event commits both settled stop and its timer.
            self.schedule_wakeup(seconds,&reason,None)?;
        }
        Ok(())
    }
    fn background_control(&mut self,v:&Value)->Result<bool>{
        if !matches!(v["kind"].as_str(),Some("task_list"|"task_get"|"task_logs"|"task_kill"|"wakeup_list"|"wakeup_cancel"|"wakeup_run")){return Ok(false);}
        if !self.accept_input_control(v)?{return Ok(true);}
        if let Err(e)=self.finish_background_control(v){self.event("error",json!({"command_id":v["id"],"error":e.to_string()}));}
        Ok(true)
    }
    fn finish_background_control(&mut self,v:&Value)->Result<()> {
        let result=match v["kind"].as_str().unwrap_or(""){
            "task_list"=>self.bg_list(v.get("state").filter(|x|!x.is_null()).map(|x|x.as_str().ok_or("state must be a string")).transpose()?)?,
            "task_get"|"task_logs"=>{
                let metadata=self.bg_get(v["task_id"].as_str().ok_or("task_id required")?)?;
                if v["kind"]=="task_logs"{
                    let stream=v.get("stream").map(|x|x.as_str().ok_or("stream must be a string")).transpose()?.unwrap_or("both");
                    if !["both","stdout","stderr"].contains(&stream){return Err("stream must be stdout, stderr or both".into());}
                    let mut logs=json!({"task":metadata});
                    for name in ["stdout","stderr"]{if stream=="both"||stream==name{
                        logs[name]=json!({"ref":logs["task"][name]["ref"],"preview":self.ui_stream_preview(name,logs["task"][name]["index"].as_u64().ok_or("missing stream index")? as usize)?});
                    }}
                    logs
                }else{metadata}
            },
            "task_kill"=>self.bg_kill(v["task_id"].as_str().ok_or("task_id required")?,v.get("force").map(|x|x.as_bool().ok_or("force must be boolean")).transpose()?.unwrap_or(false))?,
            "wakeup_list"=>self.wakeup_list()?,
            "wakeup_cancel"=>self.wakeup_cancel(v["wakeup_id"].as_str().ok_or("wakeup_id required")?)?,
            "wakeup_run"=>self.wakeup_run(v["wakeup_id"].as_str().ok_or("wakeup_id required")?)?,
            _=>return Err("unknown background control".into())
        };
        self.event("completed",json!({"command_id":v["id"],"status":"ok","result":result}));Ok(())
    }
}
fn bg_text<'a>(v:&'a Value,key:&str,max:usize)->Result<Option<&'a str>>{
    match v.get(key){
        None|Some(Value::Null)=>Ok(None),
        Some(Value::String(s)) if !s.trim().is_empty()&&s.chars().count()<=max=>Ok(Some(s)),
        _=>Err(format!("{key} must be a nonempty string of at most {max} characters").into())
    }
}
fn bg_seconds(v:&Value,max:f64)->Result<Option<f64>>{
    if v.is_null(){return Ok(None);}
    let seconds=v.as_f64().ok_or("duration must be a number")?;
    if !seconds.is_finite()||seconds<=0.0||seconds>max{return Err(format!("duration must be finite, positive and at most {max} seconds").into());}
    Ok(Some(seconds))
}
impl Host {
    fn bg_save_task(&mut self,metadata:&Value)->Result<()>{
        self.journal.append("task_state",metadata.clone())?;
        self.event("task_state",metadata.clone());Ok(())
    }
    fn bg_save_wakeup(&mut self,metadata:&Value)->Result<()>{
        self.journal.append("wakeup_state",metadata.clone())?;
        self.event("wakeup_state",metadata.clone());Ok(())
    }
    fn bg_run(&mut self,v:&Value)->Result<Value>{
        self.service_background()?;
        if self.bg_tasks.values().filter(|t|t.child.is_some()).count()>=4{return Err("at most four active background jobs".into());}
        let source=v["source"].as_str().ok_or("background source required")?;
        if source.trim().is_empty(){return Err("background source must not be empty".into());}
        let kind=match v.get("task_kind").or_else(||v.get("kind")){None=>"shell",Some(value)=>value.as_str().ok_or("background kind must be a string")?};
        if !["shell","python"].contains(&kind){return Err("background kind must be shell or python".into());}
        let options=v.get("options").filter(|p|!p.is_null()).unwrap_or(v);
        if !options.is_object(){return Err("background options must be an object".into());}
        let name=bg_text(options,"name",128)?.map(str::to_owned);
        let wakeup_reason=bg_text(options,"wakeup_reason",512)?.map(str::to_owned);
        let timeout=bg_seconds(&options["timeout"],604800.0)?;
        let cwd=match options.get("cwd"){
            None|Some(Value::Null)=>None,
            Some(Value::String(s))=>Some(s.clone()),
            _=>return Err("cwd must be a string".into())
        };
        let mut env=Vec::new();
        if let Some(value)=options.get("env").filter(|p|!p.is_null()){
            for (key,value) in value.as_object().ok_or("env must be an object")?{
                if key.is_empty()||key.contains(['=','\0']){return Err("invalid environment key".into());}
                let value=value.as_str().ok_or("environment values must be strings")?;
                if value.contains('\0'){return Err("invalid environment value".into());}
                env.push((key.clone(),value.to_owned()));
            }
        }
        let id=format!("task{}",self.journal.seq);
        let dir=self.home.join(format!("background-{}-{}",std::process::id(),unique_id()));
        fs::create_dir(&dir)?;
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&dir,fs::Permissions::from_mode(0o700))?;
        let out=dir.join("stdout");let err=dir.join("stderr");
        let outfile=OpenOptions::new().write(true).create_new(true).mode(0o600).open(&out)?;
        let errfile=OpenOptions::new().write(true).create_new(true).mode(0o600).open(&err)?;
        let mut stdout=Capture::open(&out,"stdout",&id)?;
        let mut stderr=Capture::open(&err,"stderr",&id)?;
        // Commit empty chunks now so all running jobs have stable, distinct refs.
        stdout.commit(self,&[])?;stderr.commit(self,&[])?;
        let mut metadata=json!({"id":id,"kind":kind,"name":name,"status":"starting",
            "created_ms":now_ms(),"wakeup_reason":wakeup_reason,
            "stdout":{"ref":format!("H.stdout[{}]",stdout.index.unwrap()),"index":stdout.index,"bytes":0,"complete":false},
            "stderr":{"ref":format!("H.stderr[{}]",stderr.index.unwrap()),"index":stderr.index,"bytes":0,"complete":false}});
        self.bg_save_task(&metadata)?;
        self.journal.append("intent",json!({"operation":id,"type":"background","kind":kind,"source":source,"cwd":cwd}))?;
        let mut task=BgTask{child:None,stdout:Some(stdout),stderr:Some(stderr),dir,
            metadata:metadata.clone(),deadline:timeout.map(|s|std::time::Instant::now()+std::time::Duration::from_secs_f64(s)),
            cancel_at:None,terminal_status:None,reaped:None};
        let mut command=if kind=="shell"{let mut c=Command::new("/bin/sh");c.args(["-c",source]);c}
            else{let mut c=Command::new(std::env::var("PY_PYTHON").unwrap_or_else(|_|"python3".into()));c.args(["-u","-c",source]);c};
        if let Some(cwd)=cwd{command.current_dir(cwd);}
        command.envs(env).stdin(std::process::Stdio::null()).stdout(outfile).stderr(errfile);
        unsafe{command.pre_exec(||{if libc::setsid()<0{return Err(io::Error::last_os_error());}Ok(())});}
        match command.spawn(){
            Ok(child)=>{metadata["status"]=json!("running");metadata["pid"]=json!(child.id());task.child=Some(child);},
            Err(error)=>{
                metadata["status"]=json!("failed");
                task.stderr.as_mut().unwrap().commit(self,format!("Background launch failed: {error}\n").as_bytes())?;
                metadata["stdout"]=task.stdout.take().unwrap().finish(self,true)?;
                metadata["stderr"]=task.stderr.take().unwrap().finish(self,true)?;
                for key in ["stdout","stderr"]{metadata[key].as_object_mut().unwrap().remove("preview");}
                metadata["finished_ms"]=json!(now_ms());
            }
        }
        task.metadata=metadata.clone();self.bg_tasks.insert(id.clone(),task);
        if metadata["status"]=="failed"{self.bg_settle(&metadata)?;}else{self.bg_save_task(&metadata)?;}
        Ok(metadata)
    }
    fn bg_list(&mut self,state:Option<&str>)->Result<Value>{
        self.service_background()?;
        if state.is_some_and(|s|!["all","starting","running","cancelling","finished","succeeded","failed","cancelled","killed","timed_out","outcome_unknown"].contains(&s)){return Err("unknown task state filter".into());}
        let mut tasks:Vec<_>=self.bg_tasks.values().map(|t|t.metadata.clone())
            .filter(|m|state.is_none_or(|s|s=="all"||m["status"]==s||s=="finished"&&!matches!(m["status"].as_str(),Some("starting"|"running"|"cancelling")))) .collect();
        tasks.sort_by_key(|m|m["created_ms"].as_u64().unwrap_or(0));Ok(json!(tasks))
    }
    fn bg_get(&mut self,id:&str)->Result<Value>{
        self.service_background()?;Ok(self.bg_tasks.get(id).ok_or("unknown task ID")?.metadata.clone())
    }
    fn bg_kill(&mut self,id:&str,force:bool)->Result<Value>{
        self.service_background()?;
        let task=self.bg_tasks.get_mut(id).ok_or("unknown task ID")?;
        if let Some(child)=task.child.as_ref().filter(|_|task.reaped.is_none()){
            let signal=if force{libc::SIGKILL}else{libc::SIGTERM};
            if unsafe{libc::kill(-(child.id() as i32),signal)}<0&&io::Error::last_os_error().raw_os_error()!=Some(libc::ESRCH){return Err(io::Error::last_os_error().into());}
            if task.cancel_at.is_none(){task.cancel_at=Some(std::time::Instant::now());task.terminal_status=Some(if force{"killed"}else{"cancelled"}.into());}
            else if force&&task.terminal_status.as_deref()!=Some("timed_out"){task.terminal_status=Some("killed".into());}
            task.metadata["status"]=json!("cancelling");
            let metadata=task.metadata.clone();self.bg_save_task(&metadata)?;
        }
        Ok(self.bg_tasks.get(id).unwrap().metadata.clone())
    }
    fn bg_settle(&mut self,metadata:&Value)->Result<()>{
        let wakeup=metadata["wakeup_reason"].as_str().map(|reason|json!({
            "id":format!("wake{}",self.journal.seq),"reason":reason,"due_ms":now_ms(),
            "state":"ready","task":metadata}));
        // Terminal status and opt-in wakeup creation are one durable transition.
        self.journal.append("task_settled",json!({"operation":metadata["id"],"status":metadata["status"],"task":metadata,"wakeup":wakeup}))?;
        self.event("task_state",metadata.clone());
        if let Some(w)=wakeup{
            self.event("wakeup_state",w.clone());
            self.wakeups.insert(w["id"].as_str().unwrap().into(),Wakeup{metadata:w,deadline:None});
        }
        Ok(())
    }
    fn service_background(&mut self)->Result<()>{
        if self.journal.failed{
            self.bg_abort();return Err("session journal failed; owned background jobs terminated, capture may be partial".into());
        }
        if self.servicing{return Ok(());}
        self.servicing=true;
        let result=self.service_background_inner();self.servicing=false;
        if result.is_err(){self.bg_abort();}
        result
    }
    fn bg_abort(&mut self){
        // No more journal writes after a persistence failure, not even cleanup.
        for task in self.bg_tasks.values_mut(){
            if let Some(mut child)=task.child.take(){
                if task.reaped.is_none(){
                    unsafe{libc::kill(-(child.id() as i32),libc::SIGKILL);}
                    let _=child.kill();let _=child.wait();
                }
                task.metadata["status"]=json!("outcome_unknown");
                task.metadata.as_object_mut().unwrap().remove("pid");
                for name in ["stdout","stderr"]{task.metadata[name]["complete"]=json!(false);}
            }
        }
    }
    fn service_background_inner(&mut self)->Result<()>{
        let ids:Vec<_>=self.bg_tasks.iter().filter_map(|(id,t)|t.child.as_ref().map(|_|id.clone())).collect();
        for id in ids{
            let mut task=self.bg_tasks.remove(&id).unwrap();
            let result=(||->Result<bool>{
                let more_out=if let Some(out)=task.stdout.as_mut(){let more=out.drain(self)?;task.metadata["stdout"]["bytes"]=json!(out.bytes);more}else{false};
                let more_err=if let Some(err)=task.stderr.as_mut(){let more=err.drain(self)?;task.metadata["stderr"]["bytes"]=json!(err.bytes);more}else{false};
                let now=std::time::Instant::now();
                let child=task.child.as_mut().unwrap();
                let mut exit=if let Some(exit)=task.reaped{Some(exit)}else{child.try_wait()?};
                if exit.is_none(){
                    if task.cancel_at.is_none()&&task.deadline.is_some_and(|d|now>=d){
                        unsafe{libc::kill(-(child.id() as i32),libc::SIGTERM);}
                        task.cancel_at=Some(now);task.terminal_status=Some("timed_out".into());task.metadata["status"]=json!("cancelling");
                        self.bg_save_task(&task.metadata)?;
                    }
                    if task.cancel_at.is_some_and(|t|now.duration_since(t)>=std::time::Duration::from_millis(500)){
                        unsafe{libc::kill(-(child.id() as i32),libc::SIGKILL);}
                        exit=child.try_wait()?;
                    }
                }
                if let Some(exit)=exit{
                    // Reap once, then finish the backlog across bounded pump ticks.
                    if task.reaped.is_none(){
                        unsafe{libc::kill(-(child.id() as i32),libc::SIGKILL);}
                        task.reaped=Some(exit);
                        return Ok(false);
                    }
                    if more_out||more_err{return Ok(false);}
                    task.child.take();
                    task.metadata["stdout"]=task.stdout.take().unwrap().finish_metadata(self,true)?;
                    task.metadata["stderr"]=task.stderr.take().unwrap().finish_metadata(self,true)?;
                    for key in ["stdout","stderr"]{task.metadata[key].as_object_mut().unwrap().remove("preview");}
                    task.metadata["status"]=json!(task.terminal_status.clone().unwrap_or_else(||if exit.success(){"succeeded"}else{"failed"}.into()));
                    task.metadata["exit_code"]=json!(exit.code());task.metadata["finished_ms"]=json!(now_ms());
                    self.bg_settle(&task.metadata)?;
                    return Ok(true);
                }
                Ok(false)
            })();
            self.bg_tasks.insert(id,task);
            result?;
        }
        let mut ready=Vec::new();
        for wakeup in self.wakeups.values_mut(){
            if wakeup.metadata["state"]=="scheduled"&&wakeup.deadline.is_some_and(|due|due<=std::time::Instant::now()){
                wakeup.metadata["state"]=json!("ready");ready.push(wakeup.metadata.clone());
            }
        }
        for metadata in ready{self.bg_save_wakeup(&metadata)?;}
        Ok(())
    }
    fn schedule_wakeup(&mut self,seconds:f64,reason:&str,task:Option<Value>)->Result<Value>{
        bg_seconds(&json!(seconds),604800.0)?;
        bg_text(&json!({"reason":reason}),"reason",512)?;
        let metadata=json!({"id":format!("wake{}",self.journal.seq),"reason":reason,
            "due_ms":now_ms()+(seconds*1000.0).ceil() as u128,"state":"scheduled","task":task,"stop_settled":task.is_none()});
        self.bg_save_wakeup(&metadata)?;
        self.wakeups.insert(metadata["id"].as_str().unwrap().into(),Wakeup{metadata:metadata.clone(),
            deadline:Some(std::time::Instant::now()+std::time::Duration::from_secs_f64(seconds))});Ok(metadata)
    }
    fn wakeup_list(&mut self)->Result<Value>{
        self.service_background()?;let mut entries:Vec<_>=self.wakeups.values().map(|w|w.metadata.clone()).collect();
        entries.sort_by_key(|m|m["due_ms"].as_u64().unwrap_or(0));Ok(json!(entries))
    }
    fn wakeup_cancel(&mut self,id:&str)->Result<Value>{
        let w=self.wakeups.get_mut(id).ok_or("unknown wakeup ID")?;
        if w.metadata["state"]=="cancelled"{return Ok(w.metadata.clone());}
        if !matches!(w.metadata["state"].as_str(),Some("scheduled"|"ready"|"pending_confirmation")){return Err("wakeup is already settled".into());}
        w.metadata["state"]=json!("cancelled");let metadata=w.metadata.clone();self.bg_save_wakeup(&metadata)?;Ok(metadata)
    }
    fn wakeup_run(&mut self,id:&str)->Result<Value>{
        let w=self.wakeups.get_mut(id).ok_or("unknown wakeup ID")?;
        if !matches!(w.metadata["state"].as_str(),Some("scheduled"|"ready"|"pending_confirmation")){return Err("wakeup is already settled".into());}
        w.metadata["state"]=json!("ready");let metadata=w.metadata.clone();self.bg_save_wakeup(&metadata)?;Ok(metadata)
    }
    // Called only at an idle/safe boundary, never from servicing a busy operation.
    fn dispatch_wakeups(&mut self)->Result<bool>{
        self.service_background()?;
        if !self.pending.is_empty(){return Ok(false);}
        if let Some(command)=self.incoming.as_ref().and_then(|r|r.try_recv().ok()){
            if !self.background_control(&command)?{self.queue_arrival(command)?;}
            return Ok(false);
        }
        let mut ids:Vec<_>=self.wakeups.iter().filter_map(|(id,w)|(w.metadata["state"]=="ready").then_some(id.clone())).collect();
        ids.sort();if ids.is_empty(){return Ok(false);}
        let mut batch=Vec::new();
        for id in &ids{
            let w=self.wakeups.get_mut(id).unwrap();w.metadata["state"]=json!("dispatching");
            let metadata=w.metadata.clone();self.bg_save_wakeup(&metadata)?;batch.push(metadata);
        }
        let notice=json!({"wakeups":batch});
        if self.no_model{self.event("notice",json!({"text":format!("Wakeup: {notice}")}));}
        else{
            self.add_context("user",format!("Scheduled continuation (metadata only): {notice}"),false,vec![])?;
        }
        // A crash while dispatched remains outcome_unknown on resume, never replayed.
        let result=if self.no_model{Ok(())}else{self.run_agent()};
        for id in ids{
            let w=self.wakeups.get_mut(&id).unwrap();w.metadata["state"]=json!("consumed");
            let metadata=w.metadata.clone();self.bg_save_wakeup(&metadata)?;
        }
        result?;Ok(true)
    }
    fn bg_restore(&mut self,kind:&str,p:&Value)->Result<()>{
        if kind=="task_settled"{
            self.bg_restore("task_state",&p["task"])?;
            if !p["wakeup"].is_null(){self.bg_restore("wakeup_state",&p["wakeup"])?;}
            return Ok(());
        }
        let id=p["id"].as_str().ok_or("invalid background journal ID")?.to_owned();
        match kind{
            "task_state"=>{self.bg_tasks.insert(id,BgTask{child:None,stdout:None,stderr:None,dir:PathBuf::new(),
                metadata:p.clone(),deadline:None,cancel_at:None,terminal_status:None,reaped:None});},
            "wakeup_state"=>{self.wakeups.insert(id,Wakeup{metadata:p.clone(),deadline:None});},_=>{}
        }
        Ok(())
    }
    fn bg_recover(&mut self)->Result<()>{
        let mut tasks=Vec::new();let mut wakeups=Vec::new();
        for task in self.bg_tasks.values_mut(){
            if matches!(task.metadata["status"].as_str(),Some("starting"|"running"|"cancelling")){
                task.metadata["status"]=json!("outcome_unknown");task.metadata["replay_allowed"]=json!(false);
                task.metadata.as_object_mut().unwrap().remove("pid");
                for name in ["stdout","stderr"]{
                    let mut bytes=0usize;
                    if let Some(index)=task.metadata[name]["index"].as_u64(){
                        if let Some(chunks)=self.history[name].get(index as usize).and_then(|v|v["$chunks"].as_array()){
                            for seq in chunks{
                                let event=self.journal.event(seq.as_u64().ok_or("invalid stream chunk")? as usize)?;
                                let data=event["payload"]["base64"].as_str().ok_or("invalid stream bytes")?;
                                bytes+=data.len()/4*3-data.bytes().rev().take_while(|b|*b==b'=').count();
                            }
                        }
                    }
                    task.metadata[name]["bytes"]=json!(bytes);task.metadata[name]["complete"]=json!(false);
                }
                tasks.push(task.metadata.clone());
            }
        }
        for w in self.wakeups.values_mut(){
            if matches!(w.metadata["state"].as_str(),Some("scheduled"|"ready"|"dispatching")){
                w.metadata["state"]=json!(if w.metadata["state"]=="dispatching"{"outcome_unknown"}else{"pending_confirmation"});wakeups.push(w.metadata.clone());
            }
        }
        for metadata in tasks{self.bg_save_task(&metadata)?;}
        for metadata in wakeups{self.bg_save_wakeup(&metadata)?;}
        Ok(())
    }
    fn bg_guard(&mut self,cancel:bool)->Result<()>{
        self.service_background()?;
        if self.bg_tasks.values().any(|t|t.child.is_some()){
            if !cancel{return Err("background jobs are running; kill them first or request --cancel-tasks".into());}
            self.bg_shutdown()?;
        }
        Ok(())
    }
    fn bg_shutdown(&mut self)->Result<()>{
        let ids:Vec<_>=self.bg_tasks.iter().filter_map(|(id,t)|t.child.as_ref().map(|_|id.clone())).collect();
        for id in &ids{self.bg_kill(id,false)?;}
        let deadline=std::time::Instant::now()+std::time::Duration::from_millis(750);
        while self.bg_tasks.values().any(|t|t.child.is_some())&&std::time::Instant::now()<deadline{
            self.service_background()?;std::thread::sleep(std::time::Duration::from_millis(10));
        }
        for id in &ids{
            if self.bg_tasks.get(id).is_some_and(|t|t.child.is_some()){
                self.bg_kill(id,true)?;
            }
        }
        let deadline=std::time::Instant::now()+std::time::Duration::from_millis(250);
        while self.bg_tasks.values().any(|t|t.child.is_some())&&std::time::Instant::now()<deadline{
            self.service_background()?;std::thread::sleep(std::time::Duration::from_millis(10));
        }
        // A large backlog or escaped writer must not make shutdown unbounded.
        // Preserve captured bytes explicitly as partial rather than claiming EOF.
        for id in ids{
            let mut task=self.bg_tasks.remove(&id).unwrap();
            let result=(||->Result<()>{
                if let Some(mut child)=task.child.take(){
                    if task.reaped.is_none(){child.wait()?;}
                    task.metadata["stdout"]=task.stdout.take().unwrap().finish_metadata(self,false)?;
                    task.metadata["stderr"]=task.stderr.take().unwrap().finish_metadata(self,false)?;
                    for name in ["stdout","stderr"]{task.metadata[name].as_object_mut().unwrap().remove("preview");}
                    task.metadata["status"]=json!(task.terminal_status.clone().unwrap_or_else(||"cancelled".into()));
                    task.metadata["finished_ms"]=json!(now_ms());self.bg_settle(&task.metadata)?;
                }
                Ok(())
            })();
            self.bg_tasks.insert(id,task);result?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod background_e2e {
    fn check(body:&str){
        let prelude=r#"
import os,sys,tempfile,subprocess,json,glob,time,queue,threading,signal,http.server
home=tempfile.mkdtemp(prefix='py-bgtasks-');env=dict(os.environ,PY_HOME=home)
for k in ('PY_MODEL','PY_CONTEXT_LIMIT'):env.pop(k,None)
p=None;seen=[];counter=0
q=queue.Queue()
def records():
 return [json.loads(l) for f in glob.glob(home+'/sessions/*.jsonl') for l in open(f)]
def start(extra=(),model=False,preexec=None):
 global p,q,seen
 q=queue.Queue();seen=[]
 p=subprocess.Popen([sys.argv[1],'--json','--json-input']+([] if model else ['--no-model'])+list(extra),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env,preexec_fn=preexec)
 def read():
  for line in p.stdout:q.put(json.loads(line))
 threading.Thread(target=read,daemon=True).start()
 wait(lambda v:v['kind']=='ready')
def wait(predicate,seconds=6):
 end=time.monotonic()+seconds
 while time.monotonic()<end:
  try:v=q.get(timeout=max(.001,end-time.monotonic()))
  except queue.Empty:break
  seen.append(v)
  if predicate(v):return v
 raise AssertionError(('missing event',seen[-12:],records()[-12:]))
def wait_record(predicate):
 end=time.monotonic()+6
 while time.monotonic()<end:
  found=next((v for v in records() if predicate(v)),None)
  if found:return found
  time.sleep(.01)
 raise AssertionError(('missing journal record',records()[-12:]))
def send(kind,id=None,**kwargs):
 global counter
 counter+=1;id=id or 'cmd'+str(counter)
 p.stdin.write(json.dumps(dict(id=id,kind=kind,**kwargs))+'\n');p.stdin.flush();return id
def call(kind,id=None,**kwargs):
 id=send(kind,id,**kwargs)
 return wait(lambda v:v.get('command_id')==id and v['kind'] in ('completed','error','rejected'))
def terminal(task,status=None):
 predicate=lambda v:v['kind']=='task_state' and v.get('id')==task and v.get('status') in ([status] if status else ['succeeded','failed','cancelled','killed','timed_out'])
 return next((v for v in reversed(seen) if predicate(v)),None) or wait(predicate)
def finish():
 send('quit',cancel_tasks=True);p.wait(timeout=5);assert p.returncode==0,p.stderr.read()
def launches():return [v for v in records() if v['kind']=='intent' and v['payload'].get('type')=='background']
def model_server(codes):
 requests=[]
 class Handler(http.server.BaseHTTPRequestHandler):
  def log_message(self,*args):pass
  def do_POST(self):
   requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
   code=codes.pop(0) if codes else 'agent.loop.stop()'
   if isinstance(code,tuple):delay,code=code;time.sleep(delay)
   self.send_response(200);self.end_headers()
   try:self.wfile.write(json.dumps({'choices':[{'message':{'content':code}}]}).encode())
   except BrokenPipeError:pass
 server=http.server.HTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
 json.dump({'model':'local/m','providers':{'local':{'base_url':'http://127.0.0.1:'+str(server.server_port),'models':[{'id':'m','api':'openai-completions'}]}}},open(home+'/config.json','w'))
 return server,requests
"#;
        let script=format!("{prelude}\ntry:\n{}\nfinally:\n if p is not None and p.poll() is None:p.kill();p.wait()\n",body.lines().map(|l|format!(" {l}")).collect::<Vec<_>>().join("\n"));
        let out=crate::test_command("python3").args(["-c",&script,&std::env::var("PY_HARNESS_BIN").unwrap()]).output().unwrap();
        assert!(out.status.success(),"{}\n{}",String::from_utf8_lossy(&out.stdout),String::from_utf8_lossy(&out.stderr));
    }
    #[test]
    fn e2e_bgtasks_shell_python_isolation_and_limit(){check(r#"
start()
result=call('python',source="secret_main=42\nt=agent.bgtasks.run(\"import sys,os;print('secret_main' in globals());print('agent' in sys.modules);print(os.environ['BG_KEY']);print(repr(sys.stdin.read()))\",kind='python',env={'BG_KEY':'isolated'})\nprint(t['id'])")
assert result['status']=='ok',result
task=next(v['payload']['id'] for v in records() if v['kind']=='task_state');terminal(task,'succeeded')
m=call('task_get',task_id=task)['result'];assert m['stdout']['bytes']>0 and m['stdout']['complete'],m
assert call('task_logs',task_id=task,stream='stdout')['result']['stdout']['preview']=='False\nFalse\nisolated\n\'\'',records()
for _ in range(4):assert call('bg_run',source='sleep 20')['kind']=='completed'
assert call('bg_run',source='echo too-many')['kind']=='error'
call('task_kill',task_id=call('task_list',state='running')['result'][0]['id'],force=True)
time.sleep(.1)
for options in ({'timeout':True},{'timeout':0},{'timeout':-1},{'env':{'BAD':1}},{'name':''},{'wakeup_reason':''}):
 assert call('bg_run',source='true',options=options)['kind']=='error',options
assert call('python',source="for x in (float('nan'),float('inf'),True,0,-1):\n try:agent.bgtasks.run('true',timeout=x)\n except ValueError:pass\n else:raise AssertionError('invalid timeout accepted')\nassert secret_main==42")['status']=='ok'
assert len(launches())==5,launches()
assert call('task_list',state='typo')['kind']=='error'
finish()
"#);}
    #[test]
    fn e2e_bgtasks_stream_refs_and_context_privacy(){check(r#"
start()
a=call('bg_run',source="printf alpha-private; printf alpha-error >&2")['result'];b=call('bg_run',source="printf beta-private; printf beta-error >&2")['result']
assert a['stdout']['ref']!=b['stdout']['ref'] and a['stderr']['ref']!=b['stderr']['ref']
call('python',source="print('foreground-private')")
# Queries service the pump too; inspect final metadata without relying on earlier event ordering.
for task in (a,b):
 end=time.monotonic()+3
 while True:
  m=call('task_get',task_id=task['id'])['result']
  if m['status']=='succeeded':break
  assert time.monotonic()<end,m
  time.sleep(.02)
 assert m['stdout']['complete'] and m['stderr']['complete'],m
 logs=call('task_logs',task_id=task['id'])['result'];prefix='alpha' if task is a else 'beta'
 assert logs['stdout']['preview']==prefix+'-private' and logs['stderr']['preview']==prefix+'-error',logs
 assert m['stdout']['bytes']==len(prefix+'-private'),m
r=records();assert not any('-private' in v['payload'].get('text','') or '-error' in v['payload'].get('text','') for v in r if v['kind']=='context_add'),r
assert not any(v['kind']=='wakeup_state' for v in r),r
assert len([v for v in r if v['kind']=='task_settled'])==2,r
large=call('bg_run',task_kind='python',source="import os;os.write(1,b'x'*2000000);os.write(2,b'y'*1500000)")['result']
call('python',source="print('responsive-during-large-capture')")
terminal(large['id'],'succeeded');m=call('task_get',task_id=large['id'])['result']
assert m['stdout']['bytes']==2000000 and m['stderr']['bytes']==1500000 and m['stdout']['complete'],m
assert call('python',source="assert len(H.stdout[%d])==2000000;assert len(H.stderr[%d])==1500000"%(m['stdout']['index'],m['stderr']['index']))['status']=='ok'
finish()
"#);}
    #[test]
    fn e2e_bgtasks_busy_kill_timeout_and_idempotence(){check(r#"
start()
t=call('bg_run',source="trap '' TERM; printf partial; while :; do sleep .05; done",options={'timeout':.12})['result']
busy=send('python',source="import time;time.sleep(1.2);print('foreground-done')")
terminal(t['id'],'timed_out')
assert not any(v.get('command_id')==busy and v['kind']=='completed' for v in seen),seen
m=call('task_get',task_id=t['id'])['result'];assert m['stdout']['bytes']==7 and m['stdout']['complete'],m
assert call('task_kill',task_id=t['id'])['result']['status']=='timed_out'
wait(lambda v:v.get('command_id')==busy and v['kind']=='completed')
t=call('bg_run',source="trap '' TERM; printf killed-partial; sleep 20")['result']
busy=send('shell',command='sleep 1.2');began=time.monotonic()
assert call('task_kill',task_id=t['id'])['result']['status']=='cancelling'
terminal(t['id'],'cancelled');assert time.monotonic()-began<1,seen
assert call('task_kill',task_id=t['id'],force=True)['result']['status']=='cancelled'
wait(lambda v:v.get('command_id')==busy and v['kind']=='completed')
finish()
"#);}
    #[test]
    fn background_completion_catalog_covers_ids_and_flags(){
        let catalog=crate::CompletionCatalog{names:vec![],models:serde_json::json!([]),home:std::env::temp_dir(),
            python_cwd:std::env::temp_dir(),current_model:String::new(),configured_providers:vec![],
            task_ids:vec!["task7".into()],wakeup_ids:vec!["wake9".into()]};
        for (prefix,wanted) in [("/b","/bg"),("/task t","task7"),("/task logs t","task7"),
            ("/task logs task7 ","stdout"),("/task kill task7 ","--force"),
            ("/wakeup run w","wake9"),("/tasks out","outcome_unknown"),("/bg p","python")]{
            let (_,items)=catalog.candidates(prefix,prefix.len(),false).unwrap();
            assert!(items.iter().any(|i|i.replacement==wanted),"{prefix}: missing {wanted}");
        }
    }
    #[test]
    fn e2e_bgtasks_slash_controls_and_user_only_logs(){check(r#"
p=subprocess.Popen([sys.argv[1],'--json','--no-model'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
def read():
 for line in p.stdout:q.put(json.loads(line))
threading.Thread(target=read,daemon=True).start();wait(lambda v:v['kind']=='ready')
def line(text):p.stdin.write(text+'\n');p.stdin.flush()
line('/bg shell printf slash-private');m=wait(lambda v:v['kind']=='completed')['result']
line('@import time;time.sleep(.08)');wait(lambda v:v['kind']=='completed')
line('/task logs '+m['id']);logs=wait(lambda v:v['kind']=='completed')['result'];assert logs['stdout']['preview']=='slash-private',logs
line('/tasks');assert len(wait(lambda v:v['kind']=='info')['value'])==1
line('/task kill '+m['id']+' --force');assert wait(lambda v:v['kind']=='info')['value']['status']=='succeeded'
line('@agent.loop.stop(wakeup=(20,"ui-later"))');wait(lambda v:v['kind']=='completed')
line('/wakeups');w=wait(lambda v:v['kind']=='info')['value'];assert len(w)==1,w
line('/wakeup cancel '+w[0]['id']);assert wait(lambda v:v['kind']=='info')['value'][0]['state']=='cancelled'
line('/help');assert '/bg shell|python' in wait(lambda v:v['kind']=='notice')['text']
assert not any(v['kind']=='context_add' and 'slash-private' in v['payload']['text'] for v in records())
line('/quit');p.wait(timeout=3);assert p.returncode==0,p.stderr.read()
"#);}
    #[test]
    fn e2e_bgtasks_controls_and_command_deduplication(){check(r#"
start()
a=call('bg_run',id='launch',source='sleep 20')['result'];assert call('bg_run',id='launch',source='echo duplicate')['kind']=='rejected'
assert len(launches())==1
assert call('task_get',task_id='missing')['kind']=='error'
assert call('task_kill',task_id=a['id'],force='yes')['kind']=='error'
assert call('quit')['kind']=='error' and p.poll() is None
assert call('new')['kind']=='error'
v=call('python',source="agent.loop.stop(wakeup=(20,'later'))");assert v['status']=='ok'
w=call('wakeup_list')['result'][0];assert w['state']=='scheduled',w
assert call('wakeup_cancel',wakeup_id=w['id'])['result']['state']=='cancelled'
assert call('wakeup_cancel',wakeup_id=w['id'])['result']['state']=='cancelled'
assert call('wakeup_run',wakeup_id=w['id'])['kind']=='error'
assert call('new',cancel_tasks=True)['kind']=='completed'
assert call('task_list')['result']==[]
assert call('wakeup_list')['result']==[]
finish()
"#);}
    #[test]
    fn e2e_stop_wakeup_settlement_and_order(){check(r#"
start()
assert call('python',source="agent.loop.stop(wakeup=(20,'failed'));raise Exception('after-stop')")['status']=='error'
assert call('wakeup_list')['result']==[]
assert call('python',source="for x in (float('nan'),float('inf'),True,0,-1):\n try:agent.loop.stop(wakeup=(x,'invalid'))\n except ValueError:pass\n else:raise AssertionError('invalid wakeup accepted')")['status']=='ok'
busy=send('python',source="import time;agent.loop.stop(wakeup=(20,'cancelled'));print('staged');time.sleep(20)")
wait_record(lambda v:v['kind']=='stream' and 'staged' in v['payload'].get('text',''))
call('interrupt');wait(lambda v:v.get('command_id')==busy and v['kind']=='completed')
assert call('wakeup_list')['result']==[]
v=call('python',source="agent.loop.stop(wakeup=(20,'replaced'));agent.loop.stop(wakeup=(20,'retained'));\ntry:agent.loop.stop(wakeup=(True,'invalid'))\nexcept ValueError:pass\nprint('following-statements')")
assert v['status']=='ok'
w=call('wakeup_list')['result'];assert len(w)==1 and w[0]['reason']=='retained',w
call('python',source='agent.loop.stop()');assert len(call('wakeup_list')['result'])==1
call('wakeup_run',wakeup_id=w[0]['id']);wait(lambda v:v['kind']=='notice' and 'Wakeup:' in v.get('text',''))
assert call('wakeup_list')['result'][0]['state']=='consumed'
finish()
# Model steering wins even after a successful stop request in the current cell.
server,requests=model_server(["import time;agent.loop.stop(wakeup=(20,'discarded'));print('stage-ready');time.sleep(.3)","agent.loop.stop()"])
start(model=True)
first=send('submit',text='initial')
wait_record(lambda v:v['kind']=='stream' and 'stage-ready' in v['payload'].get('text',''))
steer=send('submit',text='steer-now',mode='steering')
wait(lambda v:v.get('command_id')==first and v['kind']=='completed')
assert len(requests)==2 and 'steer-now' in json.dumps(requests[1]),requests
assert call('wakeup_list')['result']==[]
finish();server.shutdown()
"#);}
    #[test]
    fn e2e_bgtasks_wakeup_safe_boundaries_and_privacy(){check(r#"
server,requests=model_server(["import time;agent.loop.stop(wakeup=(.04,'first'));time.sleep(.1)","agent.loop.stop()"])
start(model=True)
call('submit',text='initial')
wait(lambda v:v['kind']=='wakeup_state' and v.get('state')=='consumed')
assert len(requests)==2 and 'Scheduled continuation' in json.dumps(requests[1]),requests
finish();server.shutdown()
# Capture and immediate controls continue while the provider is blocked;
# simultaneously ready completion wakeups coalesce into just one continuation.
server,requests=model_server([(.5,'agent.loop.stop()'),'agent.loop.stop()'])
start(model=True)
a=call('bg_run',source='sleep .06;printf provider-private-a',options={'wakeup_reason':'review-a'})['result']
b=call('bg_run',source='sleep .06;printf provider-private-b',options={'wakeup_reason':'review-b'})['result']
fg=send('submit',text='provider-busy')
terminal(a['id'],'succeeded');terminal(b['id'],'succeeded')
assert len(requests)==1,requests
assert call('task_get',task_id=a['id'])['result']['status']=='succeeded'
assert not any(v['kind']=='completed' and v.get('command_id')==fg for v in seen),seen
wait(lambda v:v['kind']=='wakeup_state' and v.get('state')=='consumed')
w=call('wakeup_list')['result'];assert sum(v['state']=='consumed' for v in w)==2,w
assert len(requests)==2 and 'review-a' in json.dumps(requests[1]) and 'review-b' in json.dumps(requests[1]),requests
assert 'provider-private' not in json.dumps(requests),requests
finish();server.shutdown()
start()
a=call('bg_run',source='sleep .08;printf optin-private',options={'wakeup_reason':'review'})['result']
b=call('bg_run',source='sleep .08;printf silent-private')['result']
fg=send('python',source="input('hold boundary: ')")
prompt=wait(lambda v:v['kind']=='input_prompt')
terminal(a['id'],'succeeded');time.sleep(.1)
assert not any(v['kind']=='notice' and 'Wakeup:' in v.get('text','') for v in seen),seen
call('stdin_reply',prompt_id=prompt['prompt_id'],operation=prompt['operation'],worker_generation=prompt['worker_generation'],value='release')
wait(lambda v:v['kind']=='notice' and 'Wakeup:' in v.get('text',''))
r=records();settled=[v['payload'] for v in r if v['kind']=='task_settled' and v['payload']['task']['id']==a['id'] and v['payload']['task']['created_ms']==a['created_ms']]
assert len(settled)==1 and settled[0]['wakeup']['task']['stdout']['bytes']==13,settled
assert 'optin-private' not in json.dumps(settled) and 'silent-private' not in json.dumps(settled),settled
finish()
"#);}
    #[test]
    fn e2e_bgtasks_wakeup_crash_resume_without_replay(){check(r#"
start()
t=call('bg_run',source='sleep 20; touch '+home+'/must-not-replay')['result']
pid=t['pid'];call('python',source="agent.loop.stop(wakeup=(.2,'resume-needs-confirmation'))")
p.kill();p.wait()
path=glob.glob(home+'/sessions/*.jsonl')[0]
# Explicitly simulate stale/reused saved PID: restore must not ever signal it.
r=records();states=[v for v in r if v['kind']=='task_state' and v['payload']['id']==t['id']]
last=states[-1];last['payload']['pid']=os.getpid()
lines=open(path).readlines();lines[last['seq']]=json.dumps(last)+'\n';open(path,'w').writelines(lines)
start(['--resume',path]);time.sleep(.25)
m=call('task_get',task_id=t['id'])['result'];assert m['status']=='outcome_unknown' and 'pid' not in m,m
assert call('task_kill',task_id=t['id'])['result']['status']=='outcome_unknown'
w=call('wakeup_list')['result'];assert w[-1]['state']=='pending_confirmation',w
assert not any(v['kind']=='notice' and 'Wakeup:' in v.get('text','') for v in seen),seen
call('wakeup_run',wakeup_id=w[-1]['id']);wait(lambda v:v['kind']=='notice' and 'Wakeup:' in v.get('text',''))
assert not os.path.exists(home+'/must-not-replay') and len(launches())==1
finish()
try:os.killpg(pid,signal.SIGKILL)
except ProcessLookupError:pass
# A crash during a dispatched provider request is uncertain, never replayed.
server,requests=model_server(["agent.loop.stop(wakeup=(.02,'uncertain'))",(.8,'agent.loop.stop()')])
start(model=True);call('submit',text='schedule interrupted dispatch')
end=time.monotonic()+3
while len(requests)<2:
 assert time.monotonic()<end,requests
 time.sleep(.01)
p.kill();p.wait();path=max(glob.glob(home+'/sessions/*.jsonl'),key=os.path.getmtime)
start(['--resume',path],model=True);time.sleep(.1)
w=call('wakeup_list')['result'];assert any(v['state']=='outcome_unknown' for v in w),w
unknown=next(v for v in w if v['state']=='outcome_unknown')
assert call('wakeup_run',wakeup_id=unknown['id'])['kind']=='error'
assert len(requests)==2,requests
finish();server.shutdown()
"#);}
    #[test]
    fn e2e_bgtasks_session_shutdown_and_failure_cleanup(){check(r#"
start()
t=call('bg_run',source="printf eof-partial; trap '' TERM; sleep 20")['result'];time.sleep(.05)
p.stdin.close();p.wait(timeout=4);assert p.returncode==0,p.stderr.read()
try:os.kill(t['pid'],0);raise AssertionError('child not reaped')
except ProcessLookupError:pass
r=records();m=next(v['payload']['task'] for v in r if v['kind']=='task_settled' and v['payload']['task']['id']==t['id'])
assert m['status']=='cancelled' and m['stdout']['bytes']==11,m
start()
m=call('bg_run',source='true',options={'cwd':home+'/missing'})['result'];assert m['status']=='failed' and m['stderr']['bytes']>0,m
assert call('task_get',task_id=m['id'])['result']['status']=='failed'
finish()
# A real journal I/O failure must not leave an owned process running.
def limit_file_size():
 import resource
 signal.signal(signal.SIGXFSZ,signal.SIG_IGN)
 resource.setrlimit(resource.RLIMIT_FSIZE,(65536,65536))
start(preexec=limit_file_size)
t=call('bg_run',source="printf before-disk-failure; sleep .1; python3 -c \"print('x'*50000)\"; sleep 20")['result']
p.wait(timeout=4);assert p.returncode!=0
try:os.kill(t['pid'],0);raise AssertionError('journal failure left owned child')
except ProcessLookupError:pass
path=max(glob.glob(home+'/sessions/*.jsonl'),key=os.path.getmtime)
r=[json.loads(l) for l in open(path) if l.endswith('\n')]
assert not any(v['kind']=='task_settled' for v in r),r[-3:]
"#);}
}

// Durable entries remain ordinary files; only initialization interprets them.
// Pure, bounded catalog normalization. Remote instructions and tool metadata
// are intentionally never copied into normalized descriptors.
fn catalog_safe_id(id:&str)->bool{
    !id.is_empty()&&id.len()<=128&&id.bytes().all(|c|c.is_ascii_alphanumeric()||matches!(c,b'.'|b'_'|b'-'))
}
// OpenAI fine-tuned model IDs contain colons; Codex native slugs do not.
fn catalog_api_id(id:&str)->bool{
    !id.is_empty()&&id.len()<=128&&id.bytes().all(|c|c.is_ascii_alphanumeric()||matches!(c,b'.'|b'_'|b'-'|b':'))
}
fn catalog_safe_label(text:&str,max:usize)->bool{
    !text.trim().is_empty()&&text.chars().count()<=max
        &&!text.chars().any(|c|c.is_control()||matches!(c,'\u{2028}'|'\u{2029}'))
}
fn catalog_optional_limit(item:&Value,key:&str)->Result<Option<u64>>{
    match item.get(key){
        None|Some(Value::Null)=>Ok(None),
        Some(value)=>{
            let n=value.as_u64().filter(|n|(1..=16777216).contains(n))
                .ok_or_else(||format!("Invalid model catalog {key}: expected an integer in 1..=16777216"))?;
            Ok(Some(n))
        }
    }
}
fn catalog_effort(effort:&str)->Option<&str>{
    match effort{
        "none"=>Some("off"),
        "off"|"minimal"|"low"|"medium"|"high"|"xhigh"|"max"=>Some(effort),
        _=>None,
    }
}
fn catalog_aliases(slug:&str,base:Option<&Value>)->Vec<String>{
    let mut aliases=base.and_then(|m|m["aliases"].as_array()).map(|values|values.iter()
        .filter_map(Value::as_str).filter(|s|catalog_safe_id(s)).map(str::to_string).collect::<Vec<_>>())
        .unwrap_or_default();
    if let Some((version,kind))=slug.strip_prefix("gpt-").and_then(|s|s.rsplit_once('-')){
        if ["sol","astra","luna","terra"].contains(&kind)&&!version.is_empty()
            &&!version.starts_with('.')&&!version.ends_with('.')
            &&version.bytes().all(|c|c.is_ascii_digit()||c==b'.'){
            let alias=format!("{kind}{}",version.replace('.',""));
            if catalog_safe_id(&alias)&&!aliases.contains(&alias){aliases.push(alias);}
        }
    }
    aliases
}
fn normalize_codex_catalog(body:&Value,base:&[Value])->Result<Vec<Value>>{
    let entries=body["models"].as_array().ok_or("Codex model catalog requires a models array")?;
    if entries.len()>2048{return Err("Codex model catalog exceeds 2048 entries".into());}
    let mut seen=std::collections::HashSet::new();
    let mut normalized=Vec::new();
    for item in entries{
        let slug=item["slug"].as_str().filter(|id|catalog_safe_id(id)).ok_or("Invalid Codex catalog model slug")?;
        if !seen.insert(slug){return Err(format!("Duplicate Codex catalog model slug: {slug}").into());}
        let name=item["display_name"].as_str().filter(|s|catalog_safe_label(s,256))
            .ok_or("Invalid Codex catalog model display_name")?;
        let visibility=item["visibility"].as_str().filter(|s|matches!(*s,"list"|"hide"|"none"))
            .ok_or("Invalid Codex catalog model visibility")?;
        let context=catalog_optional_limit(item,"context_window")?;
        let maximum=catalog_optional_limit(item,"max_context_window")?;
        let context=context.or(maximum);
        let percent=match item.get("effective_context_window_percent"){
            None=>95,
            Some(value)=>value.as_u64().filter(|n|(1..=100).contains(n))
                .ok_or("Invalid Codex effective_context_window_percent")?,
        };
        let input=context.map(|n|n*percent/100);
        if input==Some(0){return Err("Codex effective input limit must be positive".into());}
        let priority=match item.get("priority"){
            None=>i32::MAX as i64,
            Some(value)=>value.as_i64().filter(|n|(i32::MIN as i64..=i32::MAX as i64).contains(n))
                .ok_or("Invalid Codex catalog priority")?,
        };
        let mut levels=Vec::new();
        match item.get("supported_reasoning_levels"){
            None=>{},
            Some(value)=>{
                for level in value.as_array().filter(|a|a.len()<=32).ok_or("Invalid Codex supported_reasoning_levels")?{
                    let effort=level["effort"].as_str().filter(|s|catalog_safe_id(s)&&s.len()<=32)
                        .ok_or("Invalid Codex reasoning effort preset")?;
                    if let Some(effort)=catalog_effort(effort){
                        if !levels.contains(&effort){levels.push(effort);}
                    }
                }
            }
        }
        let preferred=match item.get("default_reasoning_level"){
            None|Some(Value::Null)=>None,
            Some(value)=>Some(value.as_str().filter(|s|catalog_safe_id(s)&&s.len()<=32)
                .ok_or("Invalid Codex default_reasoning_level")?),
        };
        let default=preferred.and_then(catalog_effort).filter(|s|levels.contains(s))
            .or_else(||if levels.contains(&"medium"){Some("medium")}else{levels.first().copied()});
        let image_input=match item.get("input_modalities"){
            None=>true, // The native schema's documented default.
            Some(value)=>{
                let modalities=value.as_array().filter(|a|a.len()<=32).ok_or("Invalid Codex input_modalities")?;
                for modality in modalities{
                    if !modality.as_str().is_some_and(|s|catalog_safe_id(s)&&s.len()<=32){
                        return Err("Invalid Codex input modality".into());
                    }
                }
                modalities.iter().any(|v|v=="image")
            }
        };
        // Validate hidden descriptors too: never accept a partial malformed catalog.
        if visibility!="list"{continue;}
        let id=format!("openai-codex/{slug}");
        let known=base.iter().find(|m|m["id"]==id);
        normalized.push(json!({"id":id,"name":name,"provider":"openai-codex",
            "api":"openai-codex-responses","aliases":catalog_aliases(slug,known),
            "context_limit":context,"max_input_tokens":input,"reasoning":!levels.is_empty(),
            "reasoning_efforts":levels,"default_effort":default,"image_input":image_input,
            "image_output":false,"deprecated":false,"priority":priority,
            "metadata_complete":context.is_some()&&!levels.is_empty(),
            "available":true,"source":"provider-discovery"}));
    }
    normalized.sort_by(|a,b|a["priority"].as_i64().cmp(&b["priority"].as_i64())
        .then_with(||a["id"].as_str().cmp(&b["id"].as_str())));
    Ok(normalized)
}
fn normalize_openai_catalog(body:&Value,base:&[Value],provider:&str)->Result<Vec<Value>>{
    if !catalog_safe_id(provider){return Err("Invalid model catalog provider ID".into());}
    let entries=body["data"].as_array().ok_or("API model catalog requires a data array")?;
    if entries.len()>2048{return Err("API model catalog exceeds 2048 entries".into());}
    let mut seen=std::collections::HashSet::new();
    let mut normalized=Vec::new();
    for entry in entries{
        let slug=entry["id"].as_str().filter(|id|catalog_api_id(id)).ok_or("Invalid API catalog model ID")?;
        if !seen.insert(slug){return Err(format!("Duplicate API catalog model ID: {slug}").into());}
        let id=format!("{provider}/{slug}");
        let known=base.iter().find(|m|m["id"]==id);
        let mut model=json!({"id":id,"name":slug,"provider":provider,"context_limit":null,
            "metadata_complete":false,"available":true,"source":"provider-discovery"});
        if let Some(known)=known{
            // /models proves availability only, not protocol or capabilities.
            for key in ["name","api","context_limit","max_input_tokens","max_tokens","image_input",
                "image_output","reasoning","reasoning_efforts","default_effort","aliases","deprecated"]{
                if let Some(value)=known.get(key){model[key]=value.clone();}
            }
            model["metadata_complete"]=json!(known["metadata_complete"]!=false
                &&known["context_limit"].as_u64().is_some_and(|n|(1..=16777216).contains(&n))
                &&known["api"].as_str().is_some_and(|s|!s.is_empty()));
        }
        normalized.push(model);
    }
    normalized.sort_by(|a,b|a["id"].as_str().cmp(&b["id"].as_str()));
    Ok(normalized)
}

const MODEL_CATALOG_MAX_BYTES:u64=8*1024*1024;
// Native catalog compatibility revision, not this harness's package version.
// Current upstream metadata requires 0.153/0.155 for the GPT-6 family.
fn model_catalog_defaults()->Value{json!({"auto_refresh":true,"refresh_interval_seconds":3600,"retry_interval_seconds":300,"codex_client_version":"0.155.0"})}
fn model_catalog_options(config:&Value)->Result<Value>{
    let mut options=model_catalog_defaults();
    if let Some(value)=config.get("model_catalog"){
        for (key,value) in value.as_object().ok_or("model_catalog must be an object")?{
            if options.get(key).is_none(){return Err(format!("unknown model_catalog option {key}").into());}
            if key=="auto_refresh"{if !value.is_boolean(){return Err("model_catalog.auto_refresh must be boolean".into());}}
            else if key=="codex_client_version"{
                if !value.as_str().is_some_and(|s|s.len()<=32&&s.split('.').count()==3&&s.split('.').all(|p|!p.is_empty()&&p.bytes().all(|b|b.is_ascii_digit())&&p.parse::<u32>().is_ok())){return Err("model_catalog.codex_client_version must be a numeric major.minor.patch string".into());}
            }
            else if !value.as_u64().is_some_and(|n|(1..=604800).contains(&n)){return Err(format!("model_catalog.{key} must be an integer in 1..=604800").into());}
            options[key]=value.clone();
        }
    }
    Ok(options)
}
fn catalog_contains_secret(value:&Value,secrets:&[String])->bool{
    match value{
        Value::String(text)=>secrets.iter().any(|s|!s.is_empty()&&text.contains(s)),
        Value::Array(values)=>values.iter().any(|v|catalog_contains_secret(v,secrets)),
        Value::Object(values)=>values.values().any(|v|catalog_contains_secret(v,secrets)),_=>false
    }
}
fn empty_model_catalog()->Value{json!({"version":1,"providers":{}})}
fn load_model_catalog(path:&Path)->Result<Value>{
    let file=match OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW|libc::O_NONBLOCK).open(path){
        Ok(file)=>file,Err(error)if error.kind()==io::ErrorKind::NotFound=>return Ok(empty_model_catalog()),Err(error)=>return Err(error.into())
    };
    if !file.metadata()?.is_file()||file.metadata()?.len()>MODEL_CATALOG_MAX_BYTES{return Err("model catalog cache must be a regular file of at most 8 MiB".into());}
    let mut cache:Value=serde_json::from_reader(file)?;
    if cache["version"]!=1||!cache["providers"].is_object(){return Err("invalid model catalog cache".into());}
    for (provider,record) in cache["providers"].as_object_mut().unwrap(){
        if !["openai","openai-codex"].contains(&provider.as_str())||!record["identity"].as_str().is_some_and(|s|s.len()==64&&s.bytes().all(|c|c.is_ascii_hexdigit())){
            return Err("invalid model catalog cache identity".into());
        }
        for key in ["fetched_at_ms","attempted_at_ms"]{if record.get(key).is_some_and(|v|!v.is_u64()){return Err("invalid model catalog timestamp".into());}}
        if let Some(models)=record.get_mut("models"){
            let models=models.as_array_mut().filter(|m|m.len()<=2048).ok_or("invalid model catalog cache entries")?;
            let mut seen=HashSet::new();
            for model in models{
                let id=model["id"].as_str().ok_or("invalid cached model ID")?;
                let (p,slug)=id.split_once('/').ok_or("invalid cached model ID")?;
                let valid_slug=if provider=="openai-codex"{catalog_safe_id(slug)}else{catalog_api_id(slug)};
                if p!=provider.as_str()||!valid_slug||!seen.insert(id.to_string()){return Err("invalid cached model identity".into());}
                if !model["name"].as_str().is_some_and(|s|catalog_safe_label(s,256))||!model["metadata_complete"].is_boolean(){return Err("invalid cached model metadata".into());}
                for key in ["context_limit","max_input_tokens","max_tokens"]{catalog_optional_limit(model,key)?;}
                if let Some(levels)=model.get("reasoning_efforts"){
                    if !levels.as_array().is_some_and(|a|a.len()<=7&&a.iter().all(|v|v.as_str().is_some_and(|s|catalog_effort(s)==Some(s)))){return Err("invalid cached model effort metadata".into());}
                }
                for key in ["reasoning","image_input","image_output","deprecated"]{if model.get(key).is_some_and(|v|!v.is_boolean()){return Err("invalid cached model capability".into());}}
                if model["metadata_complete"]==true&&(!model["context_limit"].is_u64()
                    ||!model["api"].as_str().is_some_and(|s|["openai-codex-responses","openai-responses","openai-completions","image-generation"].contains(&s))
                    ||(provider=="openai-codex"&&(!model["reasoning_efforts"].as_array().is_some_and(|a|!a.is_empty())||model["api"]!="openai-codex-responses"))){return Err("inconsistent cached model capabilities".into());}
                if let Some(aliases)=model.get("aliases"){
                    if !aliases.as_array().is_some_and(|a|a.len()<=32&&a.iter().all(|v|v.as_str().is_some_and(catalog_safe_id))){return Err("invalid cached model aliases".into());}
                }
                let mut safe=json!({});
                for key in ["id","name","api","context_limit","max_input_tokens","max_tokens","reasoning","reasoning_efforts","default_effort","image_input","image_output","deprecated","priority","aliases","metadata_complete"]{
                    if let Some(value)=model.get(key){safe[key]=value.clone();}
                }
                safe["provider"]=json!(provider);safe["source"]=json!("provider-discovery");safe["available"]=json!(true);*model=safe;
            }
        }
    }
    Ok(cache)
}
impl Host{
    fn catalog_endpoint(&self,provider:&str)->Result<(String,String)>{
        let provider=provider_alias(provider);
        if !["openai","openai-codex"].contains(&provider){return Err("catalog refresh currently supports codex and openai; other providers retain offline/configured inventory".into());}
        let (base,key,_)=self.provider_config(&format!("{provider}/catalog-discovery"))?;
        let endpoint=if provider=="openai-codex"{
            let base=base.strip_suffix("/codex/responses").map(|s|format!("{s}/codex")).unwrap_or(base);
            if base.ends_with("/codex"){format!("{base}/models")}else{format!("{base}/codex/models")}
        }else{format!("{}/models",base.trim_end_matches('/'))};
        Ok((endpoint,key))
    }
    fn catalog_identity(&self,provider:&str)->Option<String>{
        let provider=provider_alias(provider);
        let (endpoint,key)=self.catalog_endpoint(provider).ok()?;
        // Credential/account/endpoint-bound; no plaintext identity or token is cached.
        let principal=if provider=="openai-codex"{
            key.split('.').nth(1).and_then(|part|base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(part.trim_end_matches('=')).ok())
                .and_then(|bytes|serde_json::from_slice::<Value>(&bytes).ok())
                .filter(|claims|claims["https://api.openai.com/auth"]["chatgpt_account_id"]==self.auth[provider]["accountId"])
                .map(|claims|json!({"auth":claims["https://api.openai.com/auth"],"profile":claims["https://api.openai.com/profile"],"subject":claims["sub"],"email":claims["email"]}).to_string()).unwrap_or(key)
        }else{key};
        let revision=if provider=="openai-codex"{model_catalog_options(&self.config_defaults).ok()?["codex_client_version"].as_str()?.to_string()}else{String::new()};
        let scope=format!("{provider}\0{endpoint}\0{principal}\0{revision}\0{}",self.auth[provider]["accountId"].as_str().unwrap_or(""));
        Some(ring::digest::digest(&ring::digest::SHA256,scope.as_bytes()).as_ref().iter().map(|b|format!("{b:02x}")).collect())
    }
    fn catalog_auto_enabled(&self)->bool{
        match std::env::var("PY_MODEL_CATALOG_AUTO_REFRESH").ok().as_deref(){Some("0"|"false")=>false,Some("1"|"true")=>true,_=>model_catalog_options(&self.config_defaults).map_or(false,|v|v["auto_refresh"]==true)}
    }
    fn maybe_refresh_model_catalog(&mut self,provider:&str){
        if !self.catalog_auto_enabled()||self.catalog_identity(provider).is_none(){return;}
        if let Err(error)=self.refresh_model_catalog(provider,false){
            self.event("notice",json!({"text":format!("Model catalog refresh failed: {error}. Keeping cached/offline inventory; /model list refresh retries explicitly.")}));
        }
    }
    fn refresh_model_catalog(&mut self,provider:&str,force:bool)->Result<bool>{
        let provider=provider_alias(provider).to_string();
        self.reload_auth()?;
        self.catalog_endpoint(&provider)?;
        let before=self.catalog_identity(&provider).ok_or("catalog refresh requires credentials; /login the provider first")?;
        let record=&self.catalog["providers"][&provider];let now=now_ms() as u64;
        let options=model_catalog_options(&self.config_defaults)?;
        if !force&&record["identity"]==before{
            let recent=|key:&str,seconds:u64|record[key].as_u64().is_some_and(|t|t<=now&&now-t<seconds*1000);
            if recent("fetched_at_ms",options["refresh_interval_seconds"].as_u64().unwrap())||recent("attempted_at_ms",options["retry_interval_seconds"].as_u64().unwrap()){return Ok(false);}
        }
        self.event("notice",json!({"text":format!("Refreshing {provider} model catalog…")}));
        let cancel_revision=self.cancel_revision;
        let result=self.with_state(UiState::Running,None,|host|{
            if provider=="openai-codex"{host.refresh_codex()?;}
            let (endpoint,key)=host.catalog_endpoint(&provider)?;
            let client=reqwest::Client::builder().timeout(std::time::Duration::from_secs(15)).redirect(reqwest::redirect::Policy::none()).build()?;
            let mut request=client.get(endpoint).bearer_auth(&key).header("Accept","application/json").header("originator","py").header("User-Agent","py-rust/0.1.0");
            if provider=="openai-codex"{
                request=request.query(&[("client_version",options["codex_client_version"].as_str().unwrap())]).header("chatgpt-account-id",host.auth["openai-codex"]["accountId"].as_str().ok_or("Codex account ID missing")?);
            }
            let mut secrets=Vec::new();credential_secret_values(&host.auth,&mut secrets);secrets.push(key);
            let body=host.codex_wait(async{
                let mut response=request.send().await.map_err(|_|"model catalog network error")?;let status=response.status();let mut bytes=Vec::new();
                while let Some(chunk)=response.chunk().await.map_err(|_|"model catalog response disconnected")?{
                    if bytes.len() as u64+chunk.len() as u64>MODEL_CATALOG_MAX_BYTES{return Err("model catalog response exceeds 8 MiB".into());}bytes.extend_from_slice(&chunk);
                }
                if !status.is_success(){return Err(format!("model catalog HTTP {status}{}",codex_error_detail(&bytes,&secrets)).into());}
                Ok(serde_json::from_slice::<Value>(&bytes).map_err(|_|"model catalog returned invalid JSON")?)
            },"model_catalog")?;
            let base=host.models();
            if provider=="openai-codex"{normalize_codex_catalog(&body,base.as_array().unwrap())}else{normalize_openai_catalog(&body,base.as_array().unwrap(),&provider)}
        });
        // Metadata/validation failures may echo a credential in an ID or label too.
        let mut secrets=Vec::new();credential_secret_values(&self.auth,&mut secrets);
        if let Ok((_,key))=self.catalog_endpoint(&provider){secrets.push(key);}
        let mut result:Result<Vec<Value>>=result.map_err(|error|format!("model catalog refresh{}",codex_error_detail(error.to_string().as_bytes(),&secrets)).into());
        if result.as_ref().is_ok_and(|models|catalog_contains_secret(&json!(models),&secrets)){
            result=Err("model catalog contained reflected credential data; update rejected".into());
        }
        // Tokens can rotate during refresh; bind the response to the actual principal.
        let identity=self.catalog_identity(&provider).unwrap_or(before);
        let mut record=if self.catalog["providers"][&provider]["identity"]==identity{self.catalog["providers"][&provider].clone()}else{json!({"identity":identity})};
        record["attempted_at_ms"]=json!(now_ms() as u64);
        if self.cancel_revision!=cancel_revision{return Err("model catalog refresh cancelled; previous cache retained".into());}
        if let Ok(models)=&result{record["models"]=json!(models);record["fetched_at_ms"]=json!(now_ms() as u64);}
        self.commit_model_catalog(&provider,record)?;
        self.context_limit=self.model_limit(&self.model)?;
        match result{
            Ok(models)=>{
                self.journal.append("model_catalog_refresh",json!({"provider":provider,"count":models.len(),"fetched_at_ms":now_ms() as u64}))?;
                self.event("notice",json!({"text":format!("Updated {provider} model catalog: {} entries. Active model and session prompt unchanged.",models.len())}));Ok(true)
            },Err(error)=>Err(error)
        }
    }
    fn commit_model_catalog(&mut self,provider:&str,mut record:Value)->Result<()>{
        let lock=OpenOptions::new().create(true).read(true).write(true).truncate(false).mode(0o600).custom_flags(libc::O_NOFOLLOW).open(self.home.join("models.lock"))?;
        let deadline=std::time::Instant::now()+std::time::Duration::from_secs(5);
        loop{
            if unsafe{libc::flock(lock.as_raw_fd(),libc::LOCK_EX|libc::LOCK_NB)}==0{break;}
            let error=io::Error::last_os_error();if error.raw_os_error()!=Some(libc::EWOULDBLOCK){return Err(error.into());}
            if std::time::Instant::now()>=deadline||self.codex_cancel("model_catalog")?{return Err("model catalog cache busy/cancelled; previous cache retained".into());}
            std::thread::sleep(std::time::Duration::from_millis(15));
        }
        let path=self.home.join("models.json");
        let mut cache=load_model_catalog(&path).unwrap_or_else(|_|empty_model_catalog());
        let latest=&cache["providers"][provider];
        // A failed/older concurrent request must not replace a newer good snapshot.
        if latest["identity"]==record["identity"]&&latest["models"].is_array()
            &&latest["fetched_at_ms"].as_u64().unwrap_or(0)>record["fetched_at_ms"].as_u64().unwrap_or(0){
            record["models"]=latest["models"].clone();record["fetched_at_ms"]=latest["fetched_at_ms"].clone();
        }
        record["attempted_at_ms"]=json!(record["attempted_at_ms"].as_u64().unwrap_or(0).max(latest["attempted_at_ms"].as_u64().unwrap_or(0)));
        cache["providers"][provider]=record;
        if serde_json::to_vec(&cache)?.len() as u64>MODEL_CATALOG_MAX_BYTES{return Err("normalized model cache exceeds 8 MiB".into());}
        write_private_json(&path,&cache)?;self.catalog=cache;Ok(())
    }
    fn model_list_command(&mut self,args:&str)->Result<()>{
        let (verb,rest)=args.split_once(char::is_whitespace).map(|(a,b)|(a,b.trim())).unwrap_or((args,""));
        let provider=self.model.split_once('/').map(|p|provider_alias(p.0)).unwrap_or("openai").to_string();
        if verb=="refresh"{
            let target=if rest.is_empty(){provider}else{self.resolve_provider(rest)?};
            let result=self.refresh_model_catalog(&target,true);
            self.list_models(&format!("{target}/"));result.map(|_|())
        }else{self.maybe_refresh_model_catalog(&provider);self.list_models(args);Ok(())}
    }
}

#[cfg(test)]
mod catalog_normalization_tests{
    use super::*;
    use std::os::unix::fs::PermissionsExt;
    fn native(slug:&str)->Value{json!({"slug":slug,"display_name":"A native model","visibility":"list",
        "context_window":272000,"max_context_window":872000,
        "supported_reasoning_levels":[{"effort":"low"},{"effort":"medium"},{"effort":"max"}],
        "default_reasoning_level":"low","input_modalities":["text","image"],"priority":1})}
    #[test]
    fn catalog_native_limits_fields_aliases_and_efforts(){
        let mut item=native("gpt-6.2-sol");
        item["supported_reasoning_levels"].as_array_mut().unwrap().push(json!({"effort":"ultra"}));
        item["base_instructions"]=json!("REMOTE_INSTRUCTIONS_MUST_NOT_SURVIVE");
        item["model_messages"]=json!(["untrusted instructions"]);item["tools"]=json!([{"type":"shell"}]);item["supported_in_api"]=json!(false);
        let base=vec![json!({"id":"openai-codex/gpt-6.2-sol","aliases":["sol6.2"],"context_limit":1050000,"max_input_tokens":922000,"max_tokens":128000})];
        let models=normalize_codex_catalog(&json!({"models":[item]}),&base).unwrap();let model=&models[0];
        assert_eq!(model["context_limit"],272000);assert_eq!(model["max_input_tokens"],258400);
        assert_eq!(model["aliases"],json!(["sol6.2","sol62"]));assert_eq!(model["reasoning_efforts"],json!(["low","medium","max"]));
        assert_eq!(model["default_effort"],"low");assert_eq!(model["image_input"],true);assert_eq!(model["metadata_complete"],true);assert!(model.get("max_tokens").is_none());
        for key in ["base_instructions","model_messages","tools","supported_in_api"]{assert!(model.get(key).is_none());}
        assert!(!model.to_string().contains("REMOTE_INSTRUCTIONS_MUST_NOT_SURVIVE"));
    }
    #[test]
    fn catalog_native_visibility_sorting_and_missing_metadata(){
        let mut first=native("first");first["priority"]=json!(2);first["input_modalities"]=json!(["text"]);
        first["supported_reasoning_levels"]=json!([{"effort":"none"},{"effort":"low"}]);first["default_reasoning_level"]=json!("none");
        let mut second=native("second");second["priority"]=json!(1);second["context_window"]=Value::Null;second["effective_context_window_percent"]=json!(90);
        let mut hidden=native("hidden");hidden["visibility"]=json!("hide");let mut unavailable=native("unavailable");unavailable["visibility"]=json!("none");
        let incomplete=json!({"slug":"incomplete","display_name":"Incomplete","visibility":"list"});
        let models=normalize_codex_catalog(&json!({"models":[first,hidden,incomplete,second,unavailable]}),&[]).unwrap();
        assert_eq!(models.len(),3);assert_eq!(models[0]["id"],"openai-codex/second");assert_eq!(models[0]["context_limit"],872000);assert_eq!(models[0]["max_input_tokens"],784800);
        assert_eq!(models[1]["image_input"],false);assert_eq!(models[1]["default_effort"],"off");assert_eq!(models[2]["metadata_complete"],false);
        assert_eq!(models[2]["context_limit"],Value::Null);assert_eq!(models[2]["reasoning"],false);assert_eq!(models[2]["image_input"],true);
    }
    #[test]
    fn catalog_native_rejects_malformed_response_atomically(){
        for field in ["slug","display_name","visibility"]{let mut bad=native("bad");bad[field]=Value::Null;assert!(normalize_codex_catalog(&json!({"models":[native("good"),bad]}),&[]).is_err(),"{field}");}
        for (field,value) in [("slug",json!("bad/id")),("slug",json!("bad\nID")),("slug",json!("bad:ID")),
            ("display_name",json!("bad\u{2028}name")),("display_name",json!("x".repeat(257))),
            ("context_window",json!(true)),("context_window",json!(-1)),("max_context_window",json!(16777217)),
            ("effective_context_window_percent",json!(0)),("effective_context_window_percent",json!(101)),
            ("supported_reasoning_levels",json!([{"effort":false}])),("default_reasoning_level",json!(false)),
            ("input_modalities",json!([true])),("priority",json!(1.5)),("visibility",json!("unknown"))]{
            let mut bad=native("bad");bad[field]=value;assert!(normalize_codex_catalog(&json!({"models":[native("good"),bad]}),&[]).is_err(),"{field}");
        }
        assert!(normalize_codex_catalog(&json!({"models":[native("same"),native("same")]}),&[]).is_err());assert!(normalize_codex_catalog(&json!({"models":vec![native("x");2049]}),&[]).is_err());
        assert!(normalize_codex_catalog(&json!({"models":{}}),&[]).is_err());let mut hidden=native("bad");hidden["visibility"]=json!("hide");hidden["context_window"]=json!(false);
        assert!(normalize_codex_catalog(&json!({"models":[native("good"),hidden]}),&[]).is_err());
    }
    #[test]
    fn catalog_api_known_metadata_unknown_ids_and_safe_field_selection(){
        let base=vec![json!({"id":"openai/known","name":"Known model","api":"openai-responses","context_limit":32000,"max_tokens":4096,"reasoning":true,"reasoning_efforts":["low","high"],"aliases":["known-alias"],"tools":["base tool metadata not copied"],"base_instructions":"not copied"})];
        let models=normalize_openai_catalog(&json!({"data":[{"id":"unknown","context_limit":999999,"api":"untrusted","base_instructions":"untrusted"},{"id":"known","reasoning":false},{"id":"ft:gpt-4.1:org:custom:abc"}]}),&base,"openai").unwrap();
        let known=models.iter().find(|m|m["id"]=="openai/known").unwrap();let unknown=models.iter().find(|m|m["id"]=="openai/unknown").unwrap();
        assert_eq!(known["context_limit"],32000);assert_eq!(known["reasoning"],true);assert_eq!(known["metadata_complete"],true);
        assert!(known.get("tools").is_none());assert!(known.get("base_instructions").is_none());assert_eq!(unknown["metadata_complete"],false);assert_eq!(unknown["context_limit"],Value::Null);
        assert!(unknown.get("api").is_none());assert!(unknown.get("reasoning").is_none());assert!(unknown.get("base_instructions").is_none());
        assert!(models.iter().any(|m|m["id"]=="openai/ft:gpt-4.1:org:custom:abc"&&m["metadata_complete"]==false));
    }
    #[test]
    fn catalog_api_rejects_malformed_duplicate_or_oversized_ids(){
        for body in [json!({}),json!({"data":{}}),json!({"data":[{}]}),json!({"data":[{"id":false}]}),json!({"data":[{"id":""}]}),json!({"data":[{"id":"bad/id"}]}),json!({"data":[{"id":"x".repeat(129)}]}),json!({"data":[{"id":"same"},{"id":"same"}]}),json!({"data":vec![json!({"id":"x"});2049]})]{assert!(normalize_openai_catalog(&body,&[],"openai").is_err());}
        assert!(normalize_openai_catalog(&json!({"data":[]}),&[],"bad/provider").is_err());assert_eq!(normalize_openai_catalog(&json!({"data":[]}),&[],"openai").unwrap(),Vec::<Value>::new());
    }
    #[test]
    fn catalog_config_defaults_overrides_and_invalid_values(){
        assert_eq!(model_catalog_options(&json!({})).unwrap(),model_catalog_defaults());
        let options=model_catalog_options(&json!({"model_catalog":{"auto_refresh":false,"refresh_interval_seconds":604800,"retry_interval_seconds":1}})).unwrap();
        assert_eq!(options["auto_refresh"],false);assert_eq!(options["refresh_interval_seconds"],604800);assert_eq!(options["retry_interval_seconds"],1);
        for value in [json!(false),json!({"unknown":1}),json!({"auto_refresh":1}),json!({"refresh_interval_seconds":0}),json!({"retry_interval_seconds":604801}),json!({"retry_interval_seconds":true})]{assert!(model_catalog_options(&json!({"model_catalog":value})).is_err(),"{value}");}
    }
    #[test]
    fn catalog_cache_validation_strips_instructions_and_rejects_unsafe_files(){
        let root=std::env::temp_dir().join(format!("py-catalog-cache-{}-{}",std::process::id(),now_ms()));fs::create_dir(&root).unwrap();fs::set_permissions(&root,fs::Permissions::from_mode(0o700)).unwrap();
        let path=root.join("models.json");assert_eq!(load_model_catalog(&path).unwrap(),empty_model_catalog());
        let mut model=normalize_codex_catalog(&json!({"models":[native("gpt-6.2-sol")]}),&[]).unwrap().remove(0);model["base_instructions"]=json!("MUST_NOT_LOAD");
        let mut cache=json!({"version":1,"providers":{"openai-codex":{"identity":"a".repeat(64),"attempted_at_ms":1,"fetched_at_ms":1,"models":[model]}}});
        write_private_json(&path,&cache).unwrap();let safe=load_model_catalog(&path).unwrap();assert!(!safe.to_string().contains("MUST_NOT_LOAD"));assert_eq!(safe["providers"]["openai-codex"]["models"][0]["context_limit"],272000);
        cache["providers"]["openai-codex"]["models"][0]["id"]=json!("openai/wrong-provider");write_private_json(&path,&cache).unwrap();assert!(load_model_catalog(&path).is_err());
        fs::remove_file(&path).unwrap();std::os::unix::fs::symlink(root.join("missing.json"),&path).unwrap();assert!(load_model_catalog(&path).is_err());fs::remove_file(&path).unwrap();fs::create_dir(&path).unwrap();assert!(load_model_catalog(&path).is_err());
        fs::remove_dir_all(root).unwrap();
    }
}

#[cfg(test)]
mod catalog_refresh_e2e {
    fn fixture(body: &str) {
        let prefix = r#"
import os,sys,json,tempfile,threading,subprocess,time,base64,glob,pathlib,urllib.parse,select
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
home=tempfile.mkdtemp(prefix='py-catalog-e2e-')
requests=[];responses=[];hold_refresh=False;release_refresh=threading.Event()
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def do_GET(self):
  requests.append({'method':'GET','path':self.path,'headers':{k.lower():v for k,v in self.headers.items()}})
  if hold_refresh:release_refresh.wait(timeout=5)
  status,data,kind=responses.pop(0) if responses else (500,b'unexpected model catalog request','text/plain')
  if isinstance(data,dict):data=json.dumps(data).encode()
  elif isinstance(data,str):data=data.encode()
  self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)));self.end_headers()
  try:self.wfile.write(data)
  except (BrokenPipeError,ConnectionResetError):pass
 def do_POST(self):
  raw=self.rfile.read(int(self.headers.get('Content-Length',0)))
  requests.append({'method':'POST','path':self.path,'headers':{k.lower():v for k,v in self.headers.items()},'body':raw.decode()})
  if not responses or responses[0][2]!='text/event-stream':self.send_error(500,'unexpected provider invocation');return
  status,data,kind=responses.pop(0)
  self.send_response(status);self.send_header('Content-Type',kind);self.end_headers();self.wfile.write(data.encode())
server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
base='http://127.0.0.1:'+str(server.server_port)
def jwt(account='account-SECRET',subject='subject-SECRET',plan='plus',issued=1):
 claims={'https://api.openai.com/auth':{'chatgpt_account_id':account,'chatgpt_plan_type':plan},'sub':subject,'iat':issued,'exp':issued+3600}
 return 'header.'+base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')+'.signature'
def auth(account='account-SECRET',subject='subject-SECRET',plan='plus',issued=1):
 value={'openai-codex':{'type':'oauth','access':jwt(account,subject,plan,issued),'refresh':'refresh-SECRET','expires':int((time.time()+3600)*1000),'accountId':account}}
 pathlib.Path(home+'/auth.json').write_text(json.dumps(value));os.chmod(home+'/auth.json',0o600)
config={'model':'openai-codex/gpt-6.1-sol','effort':'high','providers':{'openai-codex':{'base_url':base}},'model_catalog':{'auto_refresh':False}}
def save_config():pathlib.Path(home+'/config.json').write_text(json.dumps(config))
save_config();auth()
env=dict(os.environ,PY_HOME=home,PY_MODEL_CATALOG_AUTO_REFRESH='0')
def native(slug='gpt-6.2-sol',context=64000):
 return {'slug':slug,'display_name':'Fixture '+slug,'visibility':'list','priority':1,'context_window':context,'max_context_window':872000,'effective_context_window_percent':95,'supported_reasoning_levels':[{'effort':'low'},{'effort':'medium'},{'effort':'high'},{'effort':'max'},{'effort':'ultra'}],'default_reasoning_level':'low','input_modalities':['text','image'],'base_instructions':'REMOTE_INSTRUCTIONS_NOT_ADOPTED','model_messages':['REMOTE_MESSAGES_NOT_ADOPTED'],'tools':[{'type':'shell'}]}
def reply(data,status=200,kind='application/json'):responses.append((status,data,kind))
def catalog(*models):return {'models':list(models) if models else [native()]}
def stop_reply():reply('data: '+json.dumps({'type':'response.completed','response':{'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'agent.loop.stop()'}]}],'usage':{'input_tokens':20,'output_tokens':5}}})+'\n\n',kind='text/event-stream')
def refresh(id='refresh',provider=None):
 v={'id':id,'kind':'models','refresh':True}
 if provider is not None:v['provider']=provider
 return v
def models(id='models'):return {'id':id,'kind':'models'}
def py(source,id='python'):return {'id':id,'kind':'python','source':source}
def run(commands=None,lines=None,auto=False,args=None,model=False):
 flags=['--json',*([] if model else ['--no-model']),*(args or [])]
 if lines is None:
  flags.append('--json-input');text=''.join(json.dumps(c)+'\n' for c in (commands or []))
 else:text=lines
 p=subprocess.run([sys.argv[1],*flags],input=text,capture_output=True,text=True,env=dict(env,PY_MODEL_CATALOG_AUTO_REFRESH='1' if auto else '0'),timeout=20)
 assert p.returncode==0,(p.stdout,p.stderr)
 return [json.loads(line) for line in p.stdout.splitlines()]
def run_steps(commands):
 p=subprocess.Popen([sys.argv[1],'--json','--json-input'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,bufsize=0)
 events=[]
 def wait(kind,id=None):
  deadline=time.monotonic()+10
  while time.monotonic()<deadline:
   if not select.select([p.stdout],[],[],.1)[0]:continue
   line=p.stdout.readline();assert line,(kind,id,events,p.stderr.read())
   value=json.loads(line);events.append(value)
   assert not(value['kind']=='error' and (id is None or value.get('command_id')==id)),value
   if value['kind']==kind and (id is None or value.get('command_id')==id):return
  raise AssertionError((kind,id,events))
 wait('ready')
 for command in commands:
  p.stdin.write((json.dumps(command)+'\n').encode());p.stdin.flush()
  kind={'models':'models','status':'status'}.get(command['kind'],'completed')
  wait(kind,None if kind=='status' else command['id'])
 p.stdin.close();p.stdin=None
 out,err=p.communicate(timeout=10);assert p.returncode==0,(out,err,events)
 events.extend(json.loads(line) for line in out.splitlines())
 return events
def event(events,kind,id=None):
 matches=[v for v in events if v['kind']==kind and (id is None or v.get('command_id')==id)]
 assert matches,(kind,id,events)
 return matches[0]
def model_map(events,id='models'):return {m['id']:m for m in event(events,'models',id)['models']}
def failed(events,id):return event(events,'error',id)
def cache():return json.loads(pathlib.Path(home+'/models.json').read_text())
def record():return cache()['providers']['openai-codex']
def journal_records():return [json.loads(line) for file in glob.glob(home+'/sessions/*.jsonl') for line in pathlib.Path(file).read_text().splitlines()]
def journals():return ''.join(pathlib.Path(file).read_text() for file in glob.glob(home+'/sessions/*.jsonl'))
def expire():
 value=cache();r=value['providers']['openai-codex'];r['fetched_at_ms']=r['attempted_at_ms']=int(time.time()*1000)-7200000
 pathlib.Path(home+'/models.json').write_text(json.dumps(value))
def no_secrets():
 text=pathlib.Path(home+'/models.json').read_text()+journals()
 for secret in [jwt(),'account-SECRET','subject-SECRET','refresh-SECRET','REMOTE_INSTRUCTIONS_NOT_ADOPTED','REMOTE_MESSAGES_NOT_ADOPTED']:
  assert secret not in text,(secret,text[:1000])
"#;
        let script = format!("{prefix}\n{body}");
        let output = crate::test_command("python3")
            .args(["-c", &script, &std::env::var("PY_HARNESS_BIN").expect("build CLI and set PY_HARNESS_BIN"), env!("CARGO_PKG_VERSION")])
            .output()
            .unwrap();
        assert!(output.status.success(), "{}\n{}", String::from_utf8_lossy(&output.stdout), String::from_utf8_lossy(&output.stderr));
    }

    #[test]
    fn e2e_catalog_interrupt_is_operation_local_and_retains_exact_cache() {
        fixture(r#"
reply(catalog());run([refresh()]);original=pathlib.Path(home+'/models.json').read_bytes()
hold_refresh=True;reply(catalog(native(context=128000)))
p=subprocess.Popen([sys.argv[1],'--json','--json-input','--no-model'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env,bufsize=0)
seen=[]
def wait_event(kind,id=None):
 deadline=time.monotonic()+5
 while time.monotonic()<deadline:
  if not select.select([p.stdout],[],[],.05)[0]:continue
  line=p.stdout.readline();assert line,(kind,id,seen)
  v=json.loads(line);seen.append(v)
  if v['kind']==kind and (id is None or v.get('command_id')==id):return v
 raise AssertionError((kind,id,seen))
def send(v):p.stdin.write((json.dumps(v)+'\n').encode());p.stdin.flush()
try:
 wait_event('ready');send(refresh('slow'))
 deadline=time.monotonic()+5
 while len(requests)<2:
  assert time.monotonic()<deadline,requests;time.sleep(.01)
 send({'id':'interrupt','kind':'interrupt'})
 wait_event('completed','interrupt');error=wait_event('error','slow')
 assert 'cancelled' in error['error'],seen
 assert pathlib.Path(home+'/models.json').read_bytes()==original
 send(models('after'));assert 'openai-codex/gpt-6.2-sol' in {m['id'] for m in wait_event('models','after')['models']}
 send(py('assert 1+1==2','alive'));assert wait_event('completed','alive')['status']=='ok',seen
 p.stdin.close();p.stdin=None;out,err=p.communicate(timeout=5);assert p.returncode==0,(out,err,seen)
finally:
 release_refresh.set()
 if p.poll() is None:p.kill();p.wait()
no_secrets()
"#);
    }

    #[test]
    fn e2e_catalog_force_refresh_native_metadata_namespace_prompt_and_private_cache() {
        fixture(r#"
os.makedirs(home+'/skills')
pathlib.Path(home+'/skills/helper.md').write_text('---\nkind: python\ncreated: 2026-10-10T09:00:00Z\nupdated: 2026-10-10T09:00:00Z\norigin: user\ndescription: Frozen helper.\ncore: true\n---\n```python\ndef helper(x):\n    return x+7\n```\n')
stop_reply();reply(catalog(native('gpt-6.1-sol',32000),native('gpt-6.2-sol',64000)));stop_reply()
ev=run_steps([py('persistent=42; assert helper(1)==8','before'),{'id':'outer-before','kind':'submit','text':'first turn'},refresh(),{'id':'outer-after','kind':'submit','text':'second turn'},py('assert persistent==42 and helper(1)==8','after'),{'id':'status','kind':'status'}])
assert event(ev,'completed','before')['status']=='ok' and event(ev,'completed','after')['status']=='ok',ev
status=event(ev,'status');assert status['model']=='openai-codex/gpt-6.1-sol' and status['worker_generation']==0,status
assert status['context_usage']['model_context_limit']==32000 and status['context_usage']['input_token_budget']==27904,status
snapshot=[r for r in journal_records() if r['kind']=='skills_snapshot'];assert len(snapshot)==1,snapshot
assert 'def helper' in snapshot[0]['payload']['system'] and 'REMOTE_' not in snapshot[0]['payload']['system'],snapshot
assert len([r for r in journal_records() if r['kind']=='startup_begin'])==1,journal_records()
gets=[r for r in requests if r['method']=='GET'];posts=[r for r in requests if r['method']=='POST']
assert len(gets)==1 and len(posts)==2,requests
assert event(ev,'completed','outer-before')['status']=='ok' and event(ev,'completed','outer-after')['status']=='ok',ev
wire_systems=[json.loads(r['body'])['instructions'] for r in posts]
assert wire_systems[0]==wire_systems[1]==snapshot[0]['payload']['system'],wire_systems
url=urllib.parse.urlsplit(gets[0]['path']);assert url.path=='/codex/models' and urllib.parse.parse_qs(url.query)['client_version']==['0.155.0'],requests
assert gets[0]['headers']['authorization']=='Bearer '+jwt() and gets[0]['headers']['chatgpt-account-id']=='account-SECRET',requests
learned=next(m for m in record()['models'] if m['id']=='openai-codex/gpt-6.2-sol')
assert learned['context_limit']==64000 and learned['max_input_tokens']==60800 and learned['metadata_complete'] is True,learned
assert 'sol62' in learned['aliases'] and 'ultra' not in learned['reasoning_efforts'],learned
assert os.stat(home+'/models.json').st_mode&0o777==0o600
assert os.stat(home+'/models.lock').st_mode&0o777==0o600
no_secrets()
# Offline, cross-process reuse and fuzzy alias selection require no new request.
ev=run([models()]);assert model_map(ev)['openai-codex/gpt-6.2-sol']['ready'] is True,ev
assert len(requests)==3,requests
ev=run(lines='/model codex/sol62\n/status\n')
status=event(ev,'info')['value'];assert status['model']=='openai-codex/gpt-6.2-sol' and status['context_usage']['model_context_limit']==64000,status
assert len(requests)==3,requests
"#);
    }

    #[test]
    fn e2e_catalog_automatic_ttl_force_fresh_bypass_and_expiry() {
        fixture(r#"
config['model_catalog']={'auto_refresh':True,'refresh_interval_seconds':3600,'retry_interval_seconds':300};save_config()
reply(catalog(native('gpt-6.1-sol',32000)))
reply(catalog(native('gpt-6.1-sol',64000)))
ev=run(lines='/model list\n/models\n/model list refresh\n',auto=True)
assert len(requests)==2,requests
assert len([v for v in ev if v['kind']=='notice' and 'Updated openai-codex model catalog' in v.get('text','')])==2,ev
assert record()['models'][0]['context_limit']==64000,record()
# A fresh process/list still uses the persisted fresh catalog.
run(lines='/model list\n',auto=True);assert len(requests)==2,requests
expire();reply(catalog(native('gpt-6.1-sol',128000)))
run(lines='/model list\n',auto=True)
assert len(requests)==3 and record()['models'][0]['context_limit']==128000,(requests,record())
"#);
    }

    #[test]
    fn e2e_catalog_failure_backoff_preserves_stale_cache_and_manual_retry() {
        fixture(r#"
reply(catalog());run([refresh()]);original=record()['models'];expire()
reply({'error':{'message':'service temporarily unavailable'}},503)
ev=run(lines='/model list\n/model list\n',auto=True)
assert len(requests)==2,requests
assert any(v['kind']=='notice' and 'Keeping cached/offline inventory' in v.get('text','') for v in ev),ev
assert record()['models']==original,record()
assert record()['fetched_at_ms']<int(time.time()*1000)-3600000,record()
reply(catalog(native('gpt-6.2-sol',128000)))
ev=run([refresh('retry')]);assert event(ev,'models','retry')['models'],ev
assert len(requests)==3 and record()['models'][0]['context_limit']==128000,(requests,record())
"#);
    }

    #[test]
    fn e2e_catalog_rejections_are_atomic_bounded_redacted_and_instruction_free() {
        fixture(r#"
reply(catalog());run([refresh()]);original=record()['models']
cases=[(200,b'{not json','application/json'),(200,catalog(dict(native(),context_window=True)),'application/json'),(400,{'error':{'message':'rejected '+jwt()+' refresh-SECRET account-SECRET\n\x1b[31m'}},'application/json'),(200,b'x'*(8*1024*1024+1),'application/json'),(200,catalog(dict(native(),display_name='Reflected refresh-SECRET')),'application/json')]
for i,(status,data,kind) in enumerate(cases):
 responses.append((status,data,kind));identifier='bad-'+str(i)
 ev=run([refresh(identifier),models('fallback')]);error=failed(ev,identifier)
 assert record()['models']==original,(i,record())
 assert 'openai-codex/gpt-6.2-sol' in model_map(ev,'fallback'),ev
 assert not any(v['kind']=='models' and v.get('command_id')==identifier for v in ev),ev
 if i==2:
  assert '[redacted]' in error['error'] and jwt() not in json.dumps(ev) and 'refresh-SECRET' not in json.dumps(ev),ev
 if i==3:assert 'exceeds 8 MiB' in error['error'],error
assert len(requests)==len(cases)+1,requests
no_secrets()
"#);
    }

    #[test]
    fn e2e_catalog_principal_endpoint_rotation_and_logout_scope() {
        fixture(r#"
reply(catalog());run([refresh()]);identity=record()['identity']
# Token timestamps/rotation alone do not invalidate same-principal metadata.
auth(issued=20)
ev=run([models()]);assert 'openai-codex/gpt-6.2-sol' in model_map(ev),ev
assert record()['identity']==identity and len(requests)==1,(record(),requests)
# Account, subject, plan and endpoint each scope the cache independently.
for account,subject,plan in [('different-account','subject-SECRET','plus'),('account-SECRET','different-subject','plus'),('account-SECRET','subject-SECRET','pro')]:
 auth(account,subject,plan,issued=20)
 ev=run([models()]);assert 'openai-codex/gpt-6.2-sol' not in model_map(ev),ev
 assert len(requests)==1,requests
 # Original account cache remains on disk; it is simply not adopted.
 assert record()['identity']==identity,record()
auth(issued=20)
config['providers']['openai-codex']['base_url']=base+'/different';save_config()
ev=run([models()]);assert 'openai-codex/gpt-6.2-sol' not in model_map(ev),ev
config['providers']['openai-codex']['base_url']=base;save_config()
ev=run([{'id':'logout','kind':'logout','provider':'codex'},models()])
assert event(ev,'completed','logout')['status']=='ok',ev
assert 'openai-codex/gpt-6.2-sol' not in model_map(ev),ev
assert len(requests)==1,requests
"#);
    }

    #[test]
    fn e2e_catalog_openai_unknown_ids_need_declared_capabilities_and_key_scope() {
        fixture(r#"
config={'model':'openai/gpt-4.1','effort':'medium','providers':{'openai':{'base_url':base+'/v1'}},'model_catalog':{'auto_refresh':False}}
save_config();pathlib.Path(home+'/auth.json').write_text(json.dumps({'openai':{'type':'api_key','key':'api-key-SECRET'}}))
unknown='ft:gpt-4.1:fixture:custom'
reply({'data':[{'id':'gpt-4.1'},{'id':unknown,'context_limit':999999,'base_instructions':'REMOTE_INSTRUCTIONS_NOT_ADOPTED'}]})
ev=run([refresh(provider='openai'),models()]);m=model_map(ev)
assert requests[0]['path']=='/v1/models' and requests[0]['headers']['authorization']=='Bearer api-key-SECRET',requests
assert m['openai/gpt-4.1']['ready'] is True and m['openai/gpt-4.1']['metadata_complete'] is True,m
assert m['openai/'+unknown]['metadata_complete'] is False and m['openai/'+unknown]['ready'] is False,m
assert m['openai/'+unknown]['context_limit'] is None,m
# Both explicit selection and invocation are gated before HTTP.
ev=run(lines='/model openai/'+unknown+'\n@agent.llm("hello",model="openai/'+unknown+'")\n')
assert any(v['kind']=='error' and 'capability metadata' in v.get('error','') for v in ev),ev
assert event(ev,'completed')['status']=='error',ev
assert len(requests)==1,requests
# Explicit user descriptors override incomplete discovered metadata.
config['providers']['openai']['models']=[{'id':unknown,'api':'openai-responses','context_limit':32000,'reasoning':False,'image_input':False,'image_output':False}];save_config()
ev=run([models()]);assert model_map(ev)['openai/'+unknown]['user_declared'] is True and model_map(ev)['openai/'+unknown]['ready'] is True,ev
ev=run(lines='/model openai/'+unknown+'\n/status\n');status=event(ev,'info')['value']
assert status['model']=='openai/'+unknown and status['context_usage']['model_context_limit']==32000,status
# Another API key cannot inherit this key's discovered availability/IDs.
config['providers']['openai'].pop('models');save_config()
pathlib.Path(home+'/auth.json').write_text(json.dumps({'openai':{'type':'api_key','key':'different-api-key-SECRET'}}))
ev=run([models()]);assert 'openai/'+unknown not in model_map(ev),ev
text=pathlib.Path(home+'/models.json').read_text()+journals()
assert 'api-key-SECRET' not in text and 'different-api-key-SECRET' not in text and 'REMOTE_INSTRUCTIONS_NOT_ADOPTED' not in text,text[:1000]
assert len(requests)==1,requests
"#);
    }

    #[test]
    fn e2e_catalog_auto_disabled_manual_refresh_and_prefix_endpoint_forms() {
        fixture(r#"
run(lines='/model list\n/models\n/model codex/sol61\n');assert not requests,requests
# Auto disabling never disables an explicit refresh.
reply(catalog(native('gpt-6.1-sol')))
ev=run(lines='/model list refresh codex\n');assert len(requests)==1,requests
assert any(v['kind']=='notice' and 'Updated openai-codex' in v.get('text','') for v in ev),ev
for suffix in ['/codex','/codex/responses']:
 config['providers']['openai-codex']['base_url']=base+suffix;save_config()
 reply(catalog(native('gpt-6.1-sol')))
 run([refresh(provider='codex')])
 assert urllib.parse.urlsplit(requests[-1]['path']).path=='/codex/models',requests
assert len(requests)==3,requests
"#);
    }

    #[test]
    fn e2e_catalog_missing_credentials_unsupported_provider_and_bad_refresh_flag_do_not_fetch() {
        fixture(r#"
os.unlink(home+'/auth.json')
ev=run([refresh('missing'),{'id':'invalid','kind':'models','refresh':'yes'},models()])
assert 'login' in failed(ev,'missing')['error'].lower(),ev
assert 'refresh must be boolean' in failed(ev,'invalid')['error'],ev
assert event(ev,'models','models')['models'],ev
assert not requests,requests
ev=run(lines='/model list refresh anthropic\n/model list refresh nonexistent-provider\n')
assert any(v['kind']=='error' and 'currently supports codex and openai' in v.get('error','') for v in ev),ev
assert any(v['kind']=='error' and 'Unknown provider' in v.get('error','') for v in ev),ev
assert not requests,requests
"#);
    }
}


fn skills_defaults()->Value{json!({"enabled":true,"max_system_tokens":8000,"max_core_entry_tokens":2000,
    "max_inventory_entry_tokens":128,"max_entries":128,"max_file_bytes":65536})}
fn skills_options(config:&Value)->Result<Value>{
    if !config.is_object(){return Err("config.json must contain an object".into());}
    let mut result=skills_defaults();
    if let Some(options)=config.get("skills"){
        for (key,value) in options.as_object().ok_or("skills config must be an object")?{
            if result.get(key).is_none(){return Err(format!("unknown skills setting: {key}").into());}
            if key=="enabled"{if !value.is_boolean(){return Err("skills.enabled must be boolean".into());}}
            else if !value.as_u64().is_some_and(|n|n>0&&n<=16_777_216){return Err(format!("skills.{key} must be an integer in 1..=16777216").into());}
            result[key]=value.clone();
        }
    }
    Ok(result)
}
fn config_secret_key(key:&str)->bool{
    matches!(key.to_ascii_lowercase().replace('-',"_").as_str(),"auth"|"credentials"|"key"|"api_key"|"apikey"|"access"|"refresh"|"token"|"password"|"access_token"|"refresh_token"|"secret"|"client_secret"|"authorization")
}
fn config_has_secrets(value:&Value)->bool{
    match value{
        Value::Object(object)=>object.iter().any(|(key,value)|config_secret_key(key)||config_has_secrets(value)),
        Value::Array(values)=>values.iter().any(config_has_secrets),_=>false
    }
}
fn validate_config(config:&Value)->Result<()>{
    if config_has_secrets(config){return Err("credentials belong in auth.json and /login, not config.json".into());}
    skills_options(config)?;model_catalog_options(config)?;
    for key in ["model","effort"]{if let Some(value)=config.get(key){
        if !value.as_str().is_some_and(|s|!s.trim().is_empty()){return Err(format!("{key} must be a nonempty string").into());}
    }}
    if let Some(effort)=config["effort"].as_str(){if !["off","minimal","low","medium","high","xhigh","max"].contains(&effort){return Err("invalid configured effort".into());}}
    if let Some(providers)=config.get("providers"){if !providers.is_object(){return Err("providers must be an object".into());}}
    Ok(())
}
fn effective_config(config:&Value)->Result<Value>{let mut value=config.clone();value["skills"]=skills_options(config)?;value["model_catalog"]=model_catalog_options(config)?;Ok(value)}
fn skills_tokens(text:&str)->usize{text.chars().count().div_ceil(3)}
fn valid_skill_date(date:&str)->bool{
    if date.len()!=20||!date.is_ascii(){return false;}
    let b=date.as_bytes();if b[4]!=b'-'||b[7]!=b'-'||b[10]!=b'T'||b[13]!=b':'||b[16]!=b':'||b[19]!=b'Z'{return false;}
    if b.iter().enumerate().any(|(i,c)|![4,7,10,13,16,19].contains(&i)&&!c.is_ascii_digit()){return false;}
    let number=|a,b|date[a..b].parse::<u32>().ok();
    let (Some(y),Some(m),Some(d),Some(h),Some(min),Some(s))=(number(0,4),number(5,7),number(8,10),number(11,13),number(14,16),number(17,19)) else{return false};
    let days=match m{1|3|5|7|8|10|12=>31,4|6|9|11=>30,2=>if y%4==0&&(y%100!=0||y%400==0){29}else{28},_=>0};
    y>0&&d>0&&d<=days&&h<24&&min<60&&s<60
}
fn parse_skill(name:&str,text:&str)->Result<Value>{
    let mut lines=text.split_inclusive('\n');
    if lines.next().map(str::trim_end)!=Some("---"){return Err("missing --- front matter".into());}
    let mut metadata=json!({});let mut closed=false;
    for line in lines.by_ref(){
        let line=line.trim_end();if line=="---"{closed=true;break;}
        if line.trim().is_empty(){continue;}
        let (key,raw)=line.split_once(':').ok_or("expected key: value front matter")?;
        if !["kind","created","updated","origin","description","core"].contains(&key){return Err(format!("unknown metadata field {key}").into());}
        if metadata.get(key).is_some(){return Err(format!("duplicate metadata field {key}").into());}
        let raw=raw.trim();
        metadata[key]=if key=="core"{match raw{"true"=>json!(true),"false"=>json!(false),_=>return Err("core must be true or false".into())}}
            else if raw.starts_with('"'){let s:String=serde_json::from_str(raw)?;json!(s)}else{
                if raw.is_empty()||raw.starts_with(['\'','|','>','[','{','&','*','!','#']){return Err("use a plain single-line or JSON-quoted string".into());}json!(raw)
            };
    }
    if !closed{return Err("unterminated front matter".into());}
    for key in ["kind","created","updated","origin","description","core"]{if metadata.get(key).is_none(){return Err(format!("missing metadata field {key}").into());}}
    if !["memory","skill","python"].contains(&metadata["kind"].as_str().unwrap_or("")){return Err("kind must be memory, skill or python".into());}
    if !["agent","user"].contains(&metadata["origin"].as_str().unwrap_or("")){return Err("origin must be agent or user".into());}
    for key in ["created","updated"]{if !valid_skill_date(metadata[key].as_str().unwrap_or("")){return Err(format!("{key} must be a valid YYYY-MM-DDTHH:MM:SSZ timestamp").into());}}
    if metadata["updated"].as_str()<metadata["created"].as_str(){return Err("updated precedes created".into());}
    let description=metadata["description"].as_str().unwrap();
    if description.trim().is_empty()||description.chars().any(|c|c.is_control()||c=='\u{2028}'||c=='\u{2029}'){
        return Err("description must be nonempty, single-line and control-free".into());
    }
    let body=lines.collect::<String>();let mut python=String::new();
    if metadata["kind"]=="python"{
        let mut fence:Option<String>=None;let mut executable=false;let mut count=0;
        for line in body.split_inclusive('\n'){
            let trim=line.trim_end();
            if let Some(marker)=&fence{
                if trim==marker{fence=None;executable=false;}else if executable{python.push_str(line);}
            }else if trim.starts_with("```")||trim.starts_with("~~~"){
                let ch=trim.chars().next().unwrap();let n=trim.chars().take_while(|c|*c==ch).count();
                let info=&trim[n..];executable=info.trim()=="python";
                if executable{
                    if trim!="```python"{return Err("Python fences must use exact ```python / ``` lines".into());}
                    count+=1;
                }
                fence=Some(ch.to_string().repeat(n));
            }
        }
        if fence.is_some()||count!=1||python.trim().is_empty(){return Err("python entries require exactly one nonempty, closed fenced python block".into());}
    }
    metadata["filename"]=json!(name);metadata["body"]=json!(body);metadata["python"]=json!(python);
    metadata["sha256"]=json!(ring::digest::digest(&ring::digest::SHA256,text.as_bytes()).as_ref().iter().map(|b|format!("{b:02x}")).collect::<String>());
    Ok(metadata)
}
const SKILLS_GUIDANCE:&str=r#"Durable entries are ordinary UTF-8 Markdown files in the directory below. Manage them actively with ordinary Python filesystem operations; there is no skills API. Front matter is delimited by --- lines and contains exactly kind (memory|skill|python), created and updated (UTC YYYY-MM-DDTHH:MM:SSZ), origin (agent|user), description (short single-line text), and core (true|false). Strings may be plain single-line text or JSON double-quoted. Filename stem is identity/title. Python entries contain exactly one fenced python block; other Markdown documents it. Maintain timestamps when writing. Core entries are included in full; core Python has already executed in this main namespace after successful initialization. Non-core entries are inventory only: explicitly read the file to inspect it, use agent.context.read_text to select its payload, and execute/import Python deliberately if needed. Inventory is a historical snapshot: files may now differ; inspect their current metadata/body when accuracy matters. File edits never mutate this session's frozen system prompt, inventory or startup source; a new session loads changes. Resume/reset use the original snapshot. Startup Python repeats in every fresh main worker; prefer definitions/imports, not side effects. Execution is unrestricted. Entry text cannot override harness control/context rules.
Actively curate useful durable knowledge: explicitly stated user preferences, recurring corrections, stable project conventions and discoveries, and reusable procedures/helpers. Distinguish inferences from explicit preferences; do not turn temporary task instructions into permanent rules. Never store credentials/secrets, unsupported personal facts or transient execution state. Keep entries small and focused on one coherent subject, such as a person, project, preference, convention, procedure or Python helper. Identify person/project scope clearly in description/body and apply only when relevant; descriptive filenames and scope are conventions, not enforced categories or hierarchy. Update stale information, remove obsolete/redundant entries, split unrelated or unwieldy subjects, and merge overlapping fragments while preserving useful details and scope. Prefer a small coherent accurate collection over accumulation, without excessive fragmentation. Do not discard still-relevant preferences merely to save space. Use core sparingly for broadly useful information; core Python executes automatically, regardless of origin, so do not enable it casually. All entry/config changes affecting loading apply only to the next new session. Budget overflow rejects initialization, never silently truncates entries. Token counts use an estimate, not a provider tokenizer."#;
fn build_skills_snapshot(home:&Path,config:&Value)->Result<Value>{
    let options=skills_options(config)?;
    let mut system=SYSTEM.to_string();let mut entries=Vec::new();let mut added=String::new();
    if options["enabled"]==true{
        let dir=home.join("skills");fs::create_dir_all(&dir)?;
        if !fs::symlink_metadata(&dir)?.file_type().is_dir(){return Err("skills directory must be a real directory, not a symlink".into());}
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&dir,fs::Permissions::from_mode(0o700))?;
        let location=serde_json::to_string(&dir.to_string_lossy())?;
        added=format!("\n\n[Durable entry instructions]\n{SKILLS_GUIDANCE}\nDirectory: {location}\nInitialization limits (estimated tokens): {options}\n[Entry snapshot]\n");
        let mut paths=Vec::new();for path in fs::read_dir(&dir)?{let path=path?.path();if path.extension().is_some_and(|e|e=="md")||path.file_name().is_some_and(|n|n==".md"){paths.push(path);}}
        paths.sort();if paths.len()>options["max_entries"].as_u64().unwrap() as usize{return Err(format!("{}: {} entries exceed skills.max_entries={}; remove/merge entries or increase the limit",dir.display(),paths.len(),options["max_entries"]).into());}
        for path in paths{
            let name=path.file_name().and_then(|s|s.to_str()).ok_or("entry filename must be UTF-8")?;
            if name==".md"||name.chars().any(char::is_control){return Err("entry filename must have a nonempty, printable stem".into());}
            let meta=fs::symlink_metadata(&path)?;if !meta.file_type().is_file(){return Err(format!("{} must be a regular file, not a symlink/directory",path.display()).into());}
            let max=options["max_file_bytes"].as_u64().unwrap() as usize;
            if meta.len()>max as u64{return Err(format!("{}: {} bytes exceed skills.max_file_bytes={max}; shorten/split the entry or increase the limit",path.display(),meta.len()).into());}
            // Bound reads even if another writer grows a discovered file.
            let file=OpenOptions::new().read(true).custom_flags(libc::O_NOFOLLOW|libc::O_NONBLOCK).open(&path)?;
            if !file.metadata()?.file_type().is_file(){return Err(format!("{name} must be a regular file").into());}
            let mut bytes=Vec::new();std::io::Read::read_to_end(&mut std::io::Read::take(file,max as u64+1),&mut bytes)?;
            if bytes.len()>max{return Err(format!("{} exceeds skills.max_file_bytes={max}; shorten/split the entry or increase the limit",path.display()).into());}
            let text=std::str::from_utf8(&bytes)?;
            let mut entry=parse_skill(name,text).map_err(|e|format!("{}: {e}",path.display()))?;
            entry["path"]=json!(path);let core=entry["core"]==true;
            let inventory=json!({"filename":name,"kind":entry["kind"],"description":entry["description"],"core":core,"path":path});
            let rendered=if core{format!("\n[Core entry {}]\n{}\n{}\n[End core entry]\n",serde_json::to_string(name)?,inventory,entry["body"].as_str().unwrap())}
                else{format!("\n[Available entry] {inventory}\n")};
            let key=if core{"max_core_entry_tokens"}else{"max_inventory_entry_tokens"};
            if skills_tokens(&rendered)>options[key].as_u64().unwrap() as usize{return Err(format!("{}: {} estimated tokens exceed skills.{key}={}; shorten/split the entry or increase the limit",path.display(),skills_tokens(&rendered),options[key]).into());}
            added.push_str(&rendered);
            if !core{entry.as_object_mut().unwrap().remove("body");entry.as_object_mut().unwrap().remove("python");}
            entries.push(entry);
        }
        if skills_tokens(&added)>options["max_system_tokens"].as_u64().unwrap() as usize{return Err(format!("{}: {} estimated tokens including guidance exceed skills.max_system_tokens={}; shorten/remove entries or increase the limit",dir.display(),skills_tokens(&added),options["max_system_tokens"]).into());}
        system.push_str(&added);
    }
    Ok(json!({"version":1,"system":system,"entries":entries,"options":options,
        "estimated_added_tokens":skills_tokens(&added),"estimator":"unicode-chars/3-v1 (estimate, not a guarantee)"}))
}
impl Host{
    fn system_prompt(&self)->&str{self.skills["system"].as_str().unwrap_or(SYSTEM)}
    fn check_system_budget(&self,limit:usize)->Result<()>{self.check_model_system_budget(&self.model,limit)}
    fn check_model_system_budget(&self,model:&str,limit:usize)->Result<()>{
        let protected=(self.system_prompt().chars().count()+512).div_ceil(3);
        let available=model_input_budget(limit,&self.reasoning_metadata(model),4096);
        if protected+1024>=available{return Err(format!("Unsatisfiable protected system-prefix budget: {protected} estimated input tokens plus 1024 continuation reserve do not fit input budget {available} (context limit {limit}, 4096 output reserve); choose a larger-context model or start a new session with fewer core entries").into());}
        Ok(())
    }
    fn initialize_skills(&mut self)->Result<()>{
        self.startup_ready=false;
        self.check_system_budget(self.context_limit)?;
        let entries=self.skills["entries"].as_array().ok_or("invalid skills snapshot entries")?.iter()
            .filter(|e|e["core"]==true&&e["kind"]=="python").cloned().collect::<Vec<_>>();
        if entries.is_empty(){self.startup_ready=true;return Ok(());}
        self.journal.append("startup_begin",json!({"generation":self.generation,"entries":entries.iter().map(|e|&e["filename"]).collect::<Vec<_>>()}))?;
        self.initializing=true;
        let outcome=(||->Result<()>{
            let mut validation=String::new();
            for entry in &entries{validation.push_str(&format!("compile({}, {}, 'exec')\n",serde_json::to_string(&entry["python"])?,serde_json::to_string(&entry["path"])?));}
            let id=format!("startup-validate-{}",self.journal.seq);
            let result=self.execute(&id,&validation,false)?;
            if result["status"]!="ok"{return Err(format!("startup precompilation failed; diagnostics in {}",result["stderr"]["ref"]).into());}
            for entry in entries{
                let id=format!("startup-{}",self.journal.seq);
                self.journal.append("startup_entry",json!({"operation":id,"generation":self.generation,"filename":entry["filename"],"sha256":entry["sha256"],"source":entry["python"]}))?;
                let result=self.execute(&id,entry["python"].as_str().ok_or("missing startup source")?,false)?;
                if result["status"]!="ok"{return Err(format!("startup {} failed; diagnostics in {}; earlier side effects are not rolled back",entry["filename"],result["stderr"]["ref"]).into());}
            }
            Ok(())
        })();
        self.initializing=false;
        self.journal.append("startup_end",json!({"generation":self.generation,"status":if outcome.is_ok(){"ok"}else{"error"}}))?;
        self.startup_ready=outcome.is_ok();outcome
    }
    fn config_command(&mut self,args:&str)->Result<()>{
        let (verb,rest)=args.split_once(char::is_whitespace).map(|(a,b)|(a,b.trim())).unwrap_or((args,""));
        if verb.is_empty(){self.ui_json("Configuration (loading changes apply on /new)",&json!({"path":self.home.join("config.json"),"effective":effective_config(&self.config_defaults)?,"session_skills_options":self.skills["options"],"skills_changes_pending":skills_options(&self.config_defaults)?!=self.skills["options"],"config_changes_pending":self.config_defaults!=self.config}));return Ok(());}
        if verb=="reload"{
            if !rest.is_empty(){return Err("usage: /config reload".into());}
            let next=load_json(&self.home.join("config.json"))?;validate_config(&next)?;self.config_defaults=next;
            self.ui_text("Configuration reloaded. Session prompt/startup snapshot unchanged; catalog refresh preferences apply immediately, other loading defaults apply on /new.");return Ok(());
        }
        let (key,value)=rest.split_once(char::is_whitespace).map(|(a,b)|(a,b.trim())).unwrap_or((rest,""));
        let parts=key.split('.').collect::<Vec<_>>();
        if key.is_empty()||parts.iter().any(|p|p.is_empty()||!p.chars().all(|c|c.is_ascii_alphanumeric()||c=='_'||c=='-')){return Err("use a dotted configuration key".into());}
        if parts.iter().any(|p|config_secret_key(p)){return Err("credentials belong in auth.json and /login, not /config".into());}
        if verb=="get"{
            if !value.is_empty(){return Err("usage: /config get <key>".into());}
            let effective=effective_config(&self.config_defaults)?;let mut found=&effective;
            for part in &parts{found=found.get(*part).ok_or("unknown configuration key")?;}
            self.ui_json(key,found);return Ok(());
        }
        if verb!="set"&&verb!="unset"{return Err("usage: /config [get <key> | set <key> <JSON-value> | unset <key> | reload]".into());}
        if verb=="unset"&&!value.is_empty(){return Err("usage: /config unset <key>".into());}
        let mut next=self.config_defaults.clone();let mut target=&mut next;
        for part in &parts[..parts.len()-1]{
            let object=target.as_object_mut().ok_or("configuration key crosses a non-object")?;
            target=object.entry((*part).to_string()).or_insert_with(||json!({}));
        }
        let object=target.as_object_mut().ok_or("configuration parent must be an object")?;
        if verb=="set"{object.insert(parts.last().unwrap().to_string(),serde_json::from_str(value)?);}else{object.remove(*parts.last().unwrap());}
        validate_config(&next)?;
        let lock=OpenOptions::new().create(true).read(true).write(true).truncate(false).mode(0o600).open(self.home.join("config.lock"))?;
        if unsafe{libc::flock(lock.as_raw_fd(),libc::LOCK_EX)}!=0{return Err(io::Error::last_os_error().into());}
        let path=self.home.join("config.json");
        if load_json(&path)?!=self.config_defaults{return Err("config.json changed externally; /config reload before editing".into());}
        write_private_json(&path,&next)?;self.config_defaults=next;
        self.ui_text("Configuration saved. Current session snapshot unchanged; catalog refresh preferences apply immediately, other loading defaults apply on /new.");Ok(())
    }
}

#[cfg(test)]
mod skills_e2e {
    fn check(body: &str) {
        let script = format!(
            r#"
import os,sys,tempfile,subprocess,json,glob,pathlib,select,time
root=tempfile.mkdtemp(prefix='py-skills-');home=os.path.join(root,'home')
os.makedirs(home+'/skills')
env=dict(os.environ,PY_HOME=home)
for k in ('PY_MODEL','PY_CONTEXT_LIMIT'):env.pop(k,None)
def entry(name,kind='memory',core=False,description='Focused entry.',body='Useful knowledge.'):
 text='---\nkind: '+kind+'\ncreated: 2026-10-10T09:00:00Z\nupdated: 2026-10-10T09:00:00Z\norigin: user\ndescription: '+description+'\ncore: '+str(core).lower()+'\n---\n\n'+body+'\n'
 pathlib.Path(home+'/skills/'+name+'.md').write_text(text)
def run(lines='',args=None):
 return subprocess.run([sys.argv[1],'--json','--no-model',*(args or [])],input=lines,text=True,capture_output=True,timeout=10,env=env)
def events(p):
 assert p.returncode==0,(p.stdout,p.stderr)
 return [json.loads(l) for l in p.stdout.splitlines()]
def records(path=None):
 return [json.loads(l) for f in ([path] if path else glob.glob(home+'/sessions/*.jsonl')) for l in open(f)]
def snapshot(path=None):
 snapshots=[v['payload'] for v in records(path) if v['kind']=='skills_snapshot']
 assert len(snapshots)==1,snapshots
 return snapshots[0]
def passed(ev):
 done=[v for v in ev if v['kind']=='completed']
 assert done,ev
 assert all(v['status']=='ok' for v in done),ev
{body}
"#
        );
        let p = crate::test_command("python3")
            .args(["-c", &script, &std::env::var("PY_HARNESS_BIN").unwrap()])
            .output()
            .unwrap();
        assert!(
            p.status.success(),
            "{}\n{}",
            String::from_utf8_lossy(&p.stdout),
            String::from_utf8_lossy(&p.stderr)
        );
    }

    #[test]
    fn e2e_skills_full_core_and_inventory_only_noncore() {
        check(
            r#"
entry('preferences',core=True,body='CORE_BODY_UNIQUE: prefer concise answers.')
entry('project-testing',kind='skill',description='Project testing conventions.',body='NONCORE_BODY_MUST_NOT_APPEAR')
entry('python-util',kind='python',description='Reusable helper.',body='```python\nraise RuntimeError("NONCORE_PYTHON_MUST_NOT_RUN")\n```')
events(run())
s=snapshot()['system']
assert 'CORE_BODY_UNIQUE' in s,s
assert 'project-testing' in s and 'Project testing conventions.' in s,s
assert 'python-util' in s and 'Reusable helper.' in s,s
assert 'NONCORE_BODY_MUST_NOT_APPEAR' not in s,s
assert 'NONCORE_PYTHON_MUST_NOT_RUN' not in s,s
"#,
        );
    }

    #[test]
    fn e2e_skills_core_python_order_global_namespace_and_reset() {
        check(
            r#"
entry('a-helpers',kind='python',core=True,body='```python\nseed=40\ndef add_two(value):\n    return value+2\n```')
entry('b-dependent',kind='python',core=True,body='```python\nassert agent is not None and len(H.code)>0\nanswer=add_two(seed)\n```')
ev=events(run('@assert answer==42 and add_two(3)==5\n@answer=0\n/reset\n@assert answer==42\n'))
passed(ev)
assert 'def add_two' in snapshot()['system']
r=records()
assert len([v for v in r if v['kind']=='startup_begin'])==2,r
assert len([v for v in r if v['kind']=='startup_end' and v['payload']['status']=='ok'])==2,r
initializers=[v['payload'] for v in r if v['kind']=='startup_entry']
assert [v['filename'] for v in initializers]==['a-helpers.md','b-dependent.md']*2,initializers
assert all(v['source'] and len(v['sha256'])==64 for v in initializers),initializers
assert not any('def add_two' in v['payload'].get('text','') for v in r if v['kind']=='context_add'),r
"#,
        );
    }

    #[test]
    fn e2e_skills_resume_deleted_files_uses_original_snapshot() {
        check(
            r#"
entry('helper',kind='python',core=True,body='```python\nanswer=42\n```')
entry('preferences',core=True,body='ORIGINAL_SESSION_PREFERENCE')
passed(events(run('@assert answer==42\n')))
path=glob.glob(home+'/sessions/*.jsonl')[0]
original=snapshot(path)
for f in glob.glob(home+'/skills/*.md'):os.unlink(f)
entry('other',core=True,body='NEW_FILE_MUST_NOT_CHANGE_RESUME')
passed(events(run('@assert answer==42\n',args=['--session',path])))
assert snapshot(path)==original
assert 'NEW_FILE_MUST_NOT_CHANGE_RESUME' not in snapshot(path)['system']
"#,
        );
    }

    #[test]
    fn e2e_skills_file_edit_freezes_session_until_new() {
        check(
            r#"
entry('preferences',core=True,body='BEFORE_FILE_EDIT')
# Editing uses ordinary Python/filesystem operations, not a skills API.
p=home+'/skills/preferences.md'
source='from pathlib import Path; p=Path('+repr(p)+'); p.write_text(p.read_text().replace("BEFORE_FILE_EDIT","AFTER_FILE_EDIT"))'
ev=events(run('@'+source+'\n/reset\n/new\n'))
snapshots=[v['payload']['system'] for v in records() if v['kind']=='skills_snapshot']
assert len(snapshots)==2,snapshots
assert any('BEFORE_FILE_EDIT' in s and 'AFTER_FILE_EDIT' not in s for s in snapshots),snapshots
assert any('AFTER_FILE_EDIT' in s and 'BEFORE_FILE_EDIT' not in s for s in snapshots),snapshots
"#,
        );
    }

    #[test]
    fn e2e_skills_precompile_all_before_any_startup_side_effects() {
        check(
            r#"
marker=home+'/must-not-exist'
entry('a-effect',kind='python',core=True,body='```python\nopen('+repr(marker)+',"w").write("ran")\n```')
entry('z-invalid',kind='python',core=True,body='```python\ndef broken(:\n```')
p=run();assert p.returncode!=0,(p.stdout,p.stderr)
assert not os.path.exists(marker)
assert not any(v['kind']=='ready' for v in [json.loads(l) for l in p.stdout.splitlines()]),p.stdout
"#,
        );
    }

    #[test]
    fn e2e_skills_budget_rejection_precedes_python_execution() {
        check(
            r#"
marker=home+'/must-not-exist'
entry('helper',kind='python',core=True,body='```python\nopen('+repr(marker)+',"w").write("ran")\n```')
entry('large',core=True,body='budget '*500)
json.dump({'skills':{'max_core_entry_tokens':16}},open(home+'/config.json','w'))
p=run();assert p.returncode!=0,(p.stdout,p.stderr)
assert not os.path.exists(marker)
assert 'budget' in (p.stdout+p.stderr).lower() or 'tokens' in (p.stdout+p.stderr).lower(),(p.stdout,p.stderr)
"#,
        );
    }

    #[test]
    fn e2e_skills_invalid_metadata_and_python_fences_reject_initialization() {
        check(
            r#"
for bad in ['kind: unknown', 'origin: unknown', 'core: maybe', 'created: not-a-date', 'description: ']:
 entry('bad',core=True)
 pth=pathlib.Path(home+'/skills/bad.md')
 key=bad.split(':')[0]+':'
 lines=pth.read_text().splitlines()
 pth.write_text('\n'.join(bad if l.startswith(key) else l for l in lines)+'\n')
 p=run();assert p.returncode!=0,(bad,p.stdout,p.stderr)
 os.unlink(pth)
for body in ['Documentation without Python.', '```python\nx=1\n```\n```python\ny=2\n```']:
 entry('bad',kind='python',core=True,body=body)
 p=run();assert p.returncode!=0,(body,p.stdout,p.stderr)
 os.unlink(home+'/skills/bad.md')
"#,
        );
    }

    #[test]
    fn e2e_skills_disabled_ignores_entries_and_startup() {
        check(
            r#"
entry('helper',kind='python',core=True,body='```python\nraise RuntimeError("disabled Python ran")\n```')
pathlib.Path(home+'/skills/malformed.md').write_text('invalid front matter')
json.dump({'skills':{'enabled':False}},open(home+'/config.json','w'))
passed(events(run('@assert "helper" not in globals()\n')))
assert 'disabled Python ran' not in snapshot()['system']
assert 'Durable entry instructions' not in snapshot()['system']
assert snapshot()['entries']==[]
assert not any(v['kind']=='startup_begin' for v in records())
"#,
        );
    }

    #[test]
    fn e2e_skills_config_set_get_unset_persistence_and_invalid_value() {
        check(
            r#"
json.dump({'custom_key':{'preserve':True}},open(home+'/config.json','w'))
ev=events(run('/config set skills.max_core_entry_tokens 1000\n/config get skills.max_core_entry_tokens\n'))
assert json.load(open(home+'/config.json'))['skills']['max_core_entry_tokens']==1000
assert json.load(open(home+'/config.json'))['custom_key']=={'preserve':True}
assert any(v['kind']=='info' and v.get('value')==1000 for v in ev),ev
before=pathlib.Path(home+'/config.json').read_bytes()
ev=events(run('/config set skills.max_core_entry_tokens -1\n'))
assert any(v['kind']=='error' for v in ev),ev
assert pathlib.Path(home+'/config.json').read_bytes()==before
# unset restores defaults, without discarding unrelated keys.
ev=events(run('/config unset skills.max_core_entry_tokens\n/config get skills.max_core_entry_tokens\n'))
assert 'max_core_entry_tokens' not in json.load(open(home+'/config.json')).get('skills',{})
assert any(v['kind']=='info' and v.get('value')==2000 for v in ev),ev
assert json.load(open(home+'/config.json'))['custom_key']=={'preserve':True}
"#,
        );
    }

    #[test]
    fn e2e_skills_config_reload_affects_new_session_not_existing_snapshot() {
        check(
            r#"
entry('preferences',core=True,body='FROZEN_EVEN_WHEN_DISABLED')
source='import json; json.dump({"skills":{"enabled":False}},open('+repr(home+'/config.json')+',"w"))'
ev=events(run('@'+source+'\n/config reload\n/reset\n/new\n'))
snapshots=[v['payload']['system'] for v in records() if v['kind']=='skills_snapshot']
assert len(snapshots)==2,snapshots
assert sum('FROZEN_EVEN_WHEN_DISABLED' in s for s in snapshots)==1,snapshots
"#,
        );
    }

    #[test]
    fn e2e_skills_provider_wire_snapshot_edit_reset_resume_new() {
        check(r#"
import http.server,threading
requests=[]
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
  self.send_response(200);self.end_headers()
  self.wfile.write(json.dumps({'choices':[{'message':{'content':'agent.loop.stop()'}}]}).encode())
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
json.dump({'model':'local/m','providers':{'local':{'base_url':'http://127.0.0.1:'+str(server.server_port),'models':[{'id':'m','api':'openai-completions','context_limit':32000}]}}},open(home+'/config.json','w'))
entry('preferences',core=True,body='WIRE_ORIGINAL_CORE')
entry('noncore',description='Noncore inventory description.',body='WIRE_NONCORE_BODY_NOT_SELECTED')
pth=home+'/skills/preferences.md'
edit='from pathlib import Path; p=Path('+repr(pth)+'); p.write_text(p.read_text().replace("WIRE_ORIGINAL_CORE","WIRE_CHANGED_CORE"))'
p=subprocess.run([sys.argv[1],'--json'],input='first task\n@'+edit+'\n/reset\nsecond task\n/new\nthird task\n',text=True,capture_output=True,timeout=12,env=env)
ev=events(p)
assert len(requests)==3,(requests,p.stdout,p.stderr)
def system(request):
 systems=[m['content'] for m in request['messages'] if m['role']=='system']
 assert len(systems)==1,request
 return systems[0]
a,b,c=map(system,requests)
assert a==b and 'WIRE_ORIGINAL_CORE' in a and 'WIRE_CHANGED_CORE' not in a,(a,b)
assert 'WIRE_CHANGED_CORE' in c and 'WIRE_ORIGINAL_CORE' not in c,c
for request in requests:
 assert 'WIRE_NONCORE_BODY_NOT_SELECTED' not in json.dumps(request),request
 assert 'Noncore inventory description.' in system(request),request
ready=[v for v in ev if v['kind']=='ready']
assert len(ready)==2,ev
original_path=ready[0]['session']
assert a==snapshot(original_path)['system']
os.unlink(pth)
p=subprocess.run([sys.argv[1],'--json','--session',original_path],input='resumed task\n',text=True,capture_output=True,timeout=12,env=env)
events(p)
assert len(requests)==4,requests
assert system(requests[-1])==a,requests[-1]
assert 'system_chars' in ready[0]['context_usage'],ready
assert ready[0]['context_usage']['system_chars']==len(a),ready
server.shutdown()
"#);
    }

    #[test]
    fn e2e_skills_inner_and_background_do_not_inherit_entries() {
        check(r#"
import http.server,threading
requests=[]
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):
  requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
  self.send_response(200);self.end_headers()
  self.wfile.write(json.dumps({'choices':[{'message':{'content':'INNER_RESULT'}}]}).encode())
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
json.dump({'model':'local/m','providers':{'local':{'base_url':'http://127.0.0.1:'+str(server.server_port),'models':[{'id':'m','api':'openai-completions'}]}}},open(home+'/config.json','w'))
entry('python-global',kind='python',core=True,body='```python\ncore_only_global=42\n```')
entry('preference',core=True,body='INNER_MUST_NOT_INHERIT_CORE_MEMORY')
source='assert core_only_global==42; assert agent.llm("inner prompt",system="INNER_SYSTEM_ONLY")=="INNER_RESULT"; t=agent.bgtasks.run("print(\\\"core_only_global\\\" in globals())",kind="python"); print(t["id"])'
ev=events(run('@'+source+'\n@import time; time.sleep(.2)\n/tasks\n/quit --cancel-tasks\n'))
passed(ev)
assert len(requests)==1,requests
request=requests[0]
assert [m['content'] for m in request['messages'] if m['role']=='system']==['INNER_SYSTEM_ONLY'],request
assert 'INNER_MUST_NOT_INHERIT_CORE_MEMORY' not in json.dumps(request),request
assert 'core_only_global=42' not in json.dumps(request),request
r=records()
settled=[v['payload'] for v in r if v['kind']=='task_settled']
assert len(settled)==1 and settled[0]['status']=='succeeded',settled
# The isolated subprocess output must be captured, not inserted into model context.
chunks=[v['payload']['text'] for v in r if v['kind']=='stream' and v['payload'].get('collection')=='stdout']
assert any('False' in chunk for chunk in chunks),r
server.shutdown()
"#);
    }

    #[test]
    fn e2e_skills_runtime_failure_blocks_resume_and_requires_explicit_reset() {
        check(r#"
marker=home+'/startup-count'
source='from pathlib import Path\np=Path('+repr(marker)+')\np.write_text(str(int(p.read_text())+1) if p.exists() else "1")\nprint("STARTUP_PRIVATE_DIAGNOSTIC")\nraise RuntimeError("startup deliberately fails")'
entry('fails',kind='python',core=True,body='```python\n'+source+'\n```')
p=run();assert p.returncode!=0,(p.stdout,p.stderr)
assert pathlib.Path(marker).read_text()=='1'
assert not any(v['kind']=='ready' for v in map(json.loads,p.stdout.splitlines())),p.stdout
path=glob.glob(home+'/sessions/*.jsonl')[0]
ev=events(run('/status\n/recovery\n',args=['--session',path]))
assert pathlib.Path(marker).read_text()=='1',ev
assert any(v['kind']=='info' and v.get('value',{}).get('startup_ready') is False for v in ev),ev
assert not any(v['kind']=='ready' and v.get('startup_ready',True) for v in ev),ev
assert not any('STARTUP_PRIVATE_DIAGNOSTIC' in v['payload'].get('text','') for v in records(path) if v['kind']=='context_add'),records(path)
ev=events(run('/reset\n/status\n',args=['--session',path]))
assert pathlib.Path(marker).read_text()=='2',ev
assert any(v['kind']=='error' and 'startup' in v.get('error','').lower() for v in ev),ev
assert any(v['kind']=='info' and v.get('value',{}).get('startup_ready') is False for v in ev),ev
"#);
    }

    #[test]
    fn e2e_skills_unknown_startup_intent_is_not_automatically_replayed() {
        check(r#"
marker=home+'/startup-count'
source='from pathlib import Path\np=Path('+repr(marker)+')\np.write_text(str(int(p.read_text())+1) if p.exists() else "1")'
entry('initializer',kind='python',core=True,body='```python\n'+source+'\n```')
events(run())
assert pathlib.Path(marker).read_text()=='1'
path=glob.glob(home+'/sessions/*.jsonl')[0]
# Crash witness: retain a valid journal prefix through the startup intent, but no settlement.
lines=pathlib.Path(path).read_text().splitlines(keepends=True)
cut=next(i for i,line in enumerate(lines) if json.loads(line)['kind']=='startup_begin')
pathlib.Path(path).write_text(''.join(lines[:cut+1]))
ev=events(run('/status\n',args=['--session',path]))
assert pathlib.Path(marker).read_text()=='1',ev
assert any(v['kind']=='info' and v.get('value',{}).get('startup_ready') is False for v in ev),ev
ev=events(run('/reset\n/status\n@assert True\n',args=['--session',path]))
passed(ev)
assert any(v['kind']=='info' and v.get('value',{}).get('startup_ready') is True for v in ev),ev
assert pathlib.Path(marker).read_text()=='2'
"#);
    }

    #[test]
    fn e2e_skills_worker_crash_does_not_repeat_initialization_implicitly() {
        check(r#"
marker=home+'/startup-count'
source='from pathlib import Path\np=Path('+repr(marker)+')\np.write_text(str(int(p.read_text())+1) if p.exists() else "1")\nhelper_value=42'
entry('initializer',kind='python',core=True,body='```python\n'+source+'\n```')
ev=events(run('@import os; os._exit(17)\n/status\n/reset\n@assert helper_value==42\n'))
assert pathlib.Path(marker).read_text()=='2',(pathlib.Path(marker).read_text(),ev)
assert any(v['kind']=='info' and v.get('value',{}).get('startup_ready') is False for v in ev),ev
assert any(v['kind']=='completed' and v.get('status')=='worker_crashed' for v in ev),ev
assert [v for v in ev if v['kind']=='completed'][-1]['status']=='ok',ev
"#);
    }

    #[test]
    fn e2e_skills_inventory_aggregate_byte_and_count_budgets_precede_execution() {
        check(r#"
marker=home+'/must-not-exist'
entry('initializer',kind='python',core=True,body='```python\nopen('+repr(marker)+',"w").write("ran")\n```')
entry('knowledge',description='Available knowledge.',body='body remains unselected')
for options,needle in [({'max_inventory_entry_tokens':1},'max_inventory_entry_tokens'),({'max_system_tokens':1},'max_system_tokens'),({'max_file_bytes':64},'max_file_bytes'),({'max_entries':1},'max_entries')]:
 json.dump({'skills':options},open(home+'/config.json','w'))
 p=run();assert p.returncode!=0,(options,p.stdout,p.stderr)
 assert needle in p.stdout+p.stderr,(options,p.stdout,p.stderr)
 assert not os.path.exists(marker),options
 assert not any(v['kind']=='ready' for v in map(json.loads,p.stdout.splitlines())),p.stdout
"#);
    }

    #[test]
    fn e2e_skills_config_conflicts_reload_and_private_atomic_save() {
        check(r#"
# The first startup creates default configuration. Ordinary file changes then cause conflicts.
external={'custom':{'preserved':'external'},'skills':{'max_entries':7}}
source='import json; json.dump('+repr(external)+',open('+repr(home+'/config.json')+',"w"))'
ev=events(run('@'+source+'\n/config set skills.max_entries 8\n/config reload\n/config get skills.max_entries\n/config set skills.max_entries 9\n'))
assert any(v['kind']=='error' and 'changed' in v.get('error','').lower() for v in ev),ev
assert any(v['kind']=='info' and v.get('value')==7 for v in ev),ev
saved=json.load(open(home+'/config.json'))
assert saved['custom']==external['custom'] and saved['skills']['max_entries']==9,saved
assert os.stat(home+'/config.json').st_mode & 0o777==0o600
assert not glob.glob(home+'/config.tmp-*'),glob.glob(home+'/*')
"#);
    }

    #[test]
    fn e2e_skills_config_nested_credentials_refused_without_disclosure() {
        check(r#"
events(run())
before=pathlib.Path(home+'/config.json').read_bytes()
secret='CREDENTIAL_SHOULD_NOT_BE_DISPLAYED'
ev=events(run('/config set providers.local {"api_key":"'+secret+'"}\n/config set custom [{"access_token":"'+secret+'"}]\n/config\n'))
assert len([v for v in ev if v['kind']=='error'])==2,ev
assert pathlib.Path(home+'/config.json').read_bytes()==before
assert secret not in json.dumps(ev),ev
assert secret not in pathlib.Path(home+'/editor-history').read_text() if os.path.exists(home+'/editor-history') else True
# Reject externally edited sensitive fields on reload rather than displaying them.
source='import json; json.dump({"providers":{"local":{"API_KEY":'+repr(secret)+'}}},open('+repr(home+'/config.json')+',"w"))'
ev=events(run('@'+source+'\n/config reload\n/config\n'))
assert any(v['kind']=='error' and 'credential' in v.get('error','').lower() for v in ev),ev
# Ordinary Python source is previewed/journaled by design; config/error
# channels must not disclose sensitive fields loaded from disk.
assert secret not in json.dumps([v for v in ev if v['kind'] in ('info','error')]),ev
"#);
    }
    #[test]
    fn e2e_skills_protected_prefix_admission_model_override_and_accounting() {
        check(r#"
marker=home+'/initializer-ran'
entry('initializer',kind='python',core=True,body='```python\nopen('+repr(marker)+',"w").write("ran")\n```')
json.dump({'model':'local/tiny','providers':{'local':{'base_url':'http://127.0.0.1:1','api':'openai-responses','models':[{'id':'tiny','context_limit':5000},{'id':'large','context_limit':32000}]}}},open(home+'/config.json','w'))
p=run();assert p.returncode!=0,(p.stdout,p.stderr)
assert 'protected system-prefix budget' in p.stdout+p.stderr,(p.stdout,p.stderr)
assert not os.path.exists(marker)
ev=events(run('/model local/tiny\n/status\n',args=['--model','local/large']))
assert os.path.exists(marker)
assert any(v['kind']=='error' and 'protected system-prefix budget' in v.get('error','') for v in ev),ev
ready=next(v for v in ev if v['kind']=='ready')
assert ready['model']=='local/large',ready
usage=ready['context_usage'];system=snapshot(ready['session'])['system']
assert usage['system_chars']==len(system),usage
assert usage['rendered_chars']>=len(system)+512,usage
assert usage['estimated_input_tokens']>=(len(system)+512+2)//3,usage
"#);
    }

    #[test]
    fn e2e_skills_collapse_cannot_expand_history_using_system_allowance() {
        check(r#"
entry('core',core=True,body='Protected frozen knowledge.')
ev=events(run('@agent.context.read_text("a",max_chars=100)\n@agent.context.read_text("b",max_chars=100)\n'));passed(ev)
path=glob.glob(home+'/sessions/*.jsonl')[0]
ids=[v['payload']['id'] for v in records(path) if v['kind']=='context_add']
assert len(ids)==2,ids
source='agent.context.collapse('+repr(ids[0])+','+repr(ids[1])+','+repr('x'*600)+')'
ev=events(run('@'+source+'\n',args=['--resume',path]))
assert any(v['kind']=='completed' and v['status']=='error' for v in ev),ev
assert 'collapse must reduce rendered context size' in json.dumps(ev),ev
assert not any(v['kind']=='context_replace' for v in records(path)),records(path)
"#);
    }

    #[test]
    fn e2e_skills_config_changed_model_effort_defaults_apply_on_new() {
        check(r#"
ev=events(run('/config set model "openai/gpt-4o"\n/config set effort "high"\n/new\n'))
ready=[v for v in ev if v['kind']=='ready']
assert ready[0]['model']=='openai/gpt-4.1' and ready[0]['effort']=='medium',ready
assert ready[1]['model']=='openai/gpt-4o' and ready[1]['effort']=='high',ready
# Unchanged defaults preserve explicit live session settings on /new.
ev=events(run('/model openai/gpt-4.1\n/effort low\n/new\n'))
ready=[v for v in ev if v['kind']=='ready'];assert ready[-1]['model']=='openai/gpt-4.1' and ready[-1]['effort']=='low',ready
"#);
    }

    #[test]
    fn e2e_skills_defaults_privacy_help_version_and_discovery_validation() {
        check(r#"
untouched=home+'/untouched'
for flag in ['--help','--version']:
 p=subprocess.run([sys.argv[1],flag],env=dict(env,PY_HOME=untouched),capture_output=True,text=True,timeout=5)
 assert p.returncode==0,(p.stdout,p.stderr)
 assert not os.path.exists(untouched)
events(run())
config=json.load(open(home+'/config.json'));assert config['skills']['enabled'] is True and config['skills']['max_system_tokens']==8000,config
for path in [home,home+'/skills']:assert os.stat(path).st_mode & 0o777==0o700,path
assert os.stat(home+'/config.json').st_mode & 0o777==0o600
# Flat discovery ignores subdirectories and non-Markdown files.
os.makedirs(home+'/skills/project');pathlib.Path(home+'/skills/project/ignored.md').write_text('invalid')
pathlib.Path(home+'/skills/ignored.txt').write_text('invalid');events(run())
for case in ['directory','symlink','invalid-utf8','empty-stem','non-utf8-name']:
 if case=='directory':
  path=home+'/skills/bad.md';os.mkdir(path)
 elif case=='symlink':
  path=home+'/skills/bad.md';os.symlink(home+'/skills/ignored.txt',path)
 elif case=='invalid-utf8':
  path=home+'/skills/bad.md';pathlib.Path(path).write_bytes(b'\xff')
 elif case=='empty-stem':
  entry('');path=home+'/skills/.md'
 else:
  path=os.fsencode(home+'/skills/')+b'bad\xff.md';open(path,'wb').write(b'invalid')
 p=run();assert p.returncode!=0,(case,p.stdout,p.stderr)
 assert not any(v['kind']=='ready' for v in map(json.loads,p.stdout.splitlines())),(case,p.stdout)
 if case=='directory':os.rmdir(path)
 else:os.unlink(path)
# The skills directory itself must not be a symlink.
os.rename(home+'/skills',home+'/saved-skills');os.symlink(home+'/saved-skills',home+'/skills')
p=run();assert p.returncode!=0 and 'real directory' in p.stdout+p.stderr,(p.stdout,p.stderr)
"#);
    }
}

#[cfg(test)]
mod skills_unit_tests {
    use super::*;
    fn entry(kind:&str,body:&str)->String{format!("---\nkind: {kind}\ncreated: 2024-02-29T12:00:00Z\nupdated: 2024-03-01T12:00:00Z\norigin: agent\ndescription: Focused entry.\ncore: false\n---\n{body}")}
    #[test]
    fn skills_metadata_and_date_validation(){
        let text=entry("memory","Small body.");
        let parsed=parse_skill("person-preferences.md",&text).unwrap();
        assert_eq!(parsed["body"],"Small body.");assert_eq!(parsed["origin"],"agent");
        assert_eq!(parsed["sha256"].as_str().unwrap().len(),64);
        assert!(valid_skill_date("2024-02-29T00:00:00Z"));
        for date in ["2023-02-29T00:00:00Z","2024-13-01T00:00:00Z","2024-01-00T00:00:00Z","2024-01-01T24:00:00Z","2024-01-01T00:60:00Z","2024-01-01T00:00:60Z","0000-01-01T00:00:00Z","+024-01-01T00:00:00Z","2024-+1-01T00:00:00Z"]{assert!(!valid_skill_date(date),"{date}");}
        for modified in [text.replace("origin: agent","origin: agent\norigin: user"),text.replace("origin: agent","origin: agent\ntitle: unwanted"),text.replace("updated: 2024-03-01T12:00:00Z","updated: 2024-02-28T12:00:00Z"),text.replace("core: false","core: \"false\""),text.replace("description: Focused entry.","description: \"line\\nline\""),text.replace("description: Focused entry.","description: \"\\u0000\""),text.replace("description: Focused entry.","description: \"\\u2028\"")]{assert!(parse_skill("bad.md",&modified).is_err());}
    }
    #[test]
    fn skills_python_single_block(){
        let parsed=parse_skill("helpers.md",&entry("python","Explanation.\n```python\nx=42\n```\n")).unwrap();
        assert_eq!(parsed["python"],"x=42\n");
        for body in ["No block.","```python\nx=1\n","```python\nx=1\n```\n```python\ny=2\n```\n","~~~python\nx=1\n~~~\n","````python\nx=1\n````\n","``` python\nx=1\n```\n"]{assert!(parse_skill("bad.md",&entry("python",body)).is_err());}
    }
    #[test]
    fn skills_options_validate_and_preserve_defaults(){
        assert_eq!(skills_options(&json!({})).unwrap(),skills_defaults());
        let options=skills_options(&json!({"custom":42,"skills":{"max_core_entry_tokens":1000}})).unwrap();
        assert_eq!(options["max_core_entry_tokens"],1000);assert_eq!(options["enabled"],true);
        for config in [json!([]),json!({"skills":[]}),json!({"skills":{"unknown":1}}),json!({"skills":{"enabled":1}}),json!({"skills":{"max_entries":true}}),json!({"skills":{"max_entries":0}})]{assert!(skills_options(&config).is_err());}
        assert_eq!(skills_tokens("é界a"),1);assert_eq!(skills_tokens("é界ab"),2);
    }
    #[test]
    fn skills_curation_prompt_contract(){
        for fragment in ["ordinary Python filesystem operations","user preferences","small and focused","person, project","Update stale information","remove obsolete","split unrelated","merge overlapping","preserving useful details and scope","do not turn temporary task instructions","Never store credentials/secrets"]{assert!(SKILLS_GUIDANCE.contains(fragment),"{fragment}");}
    }
}

