# Implementing a pi-style `--json` mode

Status: implementation proposal; `--json` is not implemented today.

## Goal and pi precedent

Add a non-interactive frontend for scripts and editor integrations: one invocation,
one persistent worker, newline-delimited JSON (JSONL) on stdout, then exit.
Do not scrape terminal output or change the agent's Python-cell execution model.

Pi calls this `pi --mode json "prompt"`. Its JSON event stream starts with a session
header, emits lifecycle/message/tool events, and exits after the supplied prompts
finish. Its RPC mode is a separate, bidirectional, long-lived protocol.
See [pi's JSON mode reference](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/json.md)
and [RPC reference](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md).

Borrow that invocation and framing model, not pi's message/tool schema.
Py generates executable Python cells, buffers provider responses, and already has
its own execution events. Do not invent token deltas or describe a Python cell as
a pi tool call. This proposal is not wire-compatible with pi.

## What the code already provides

- `src/py_agent/cli.py`: `default_parser()` has no prompt argument;
  `main()` rejects non-TTY stdin/stdout. `_run_phase1()` builds the configured
  coordinator/journal, prints banners, and always starts `PlainTerminal`.
  Its `--stream` flag buffers a provider stream; it is not a frontend output mode.
- `src/py_agent/plain_terminal.py`: owns prompt-toolkit, interactive input,
  terminal rendering, and local commands. Its submission callback is useful as a
  reference, but a JSON frontend must not instantiate this class.
- `src/py_agent/coordinator.py` and `coordinator_runner.py`:
  `submit(frontend_id, text, allow_stdin=False, input_handler=None,
  on_progress=None)` is the frontend-neutral entry point. Submissions share
  the worker namespace and return `Submission`.
- `src/py_agent/contracts.py`: `Origin`, `OutputEvent`, `ExecutionResult`,
  and `Submission` already carry identity, output, and status.
  `OutputEvent` kinds are `stream`, `display`, `execute_result`, `update`,
  `clear`, `error`, and `progress`.
- `src/py_agent/coordinator_frontend.py`: dispatches ordered events and publishes
  completed execution output. `say()` becomes a `display` with
  `metadata.py_agent_source = "say"` and `metadata.final`.
  A staged final from a failed cell is deliberately suppressed.
- `src/py_agent/coordinator_agent.py`: emits phases including `generation_start`,
  `execution_start`, `cell_complete`, `format_retry`, and `step_limit`.
- `src/py_agent/coordinator_io.py`: can send provisional stream previews only to
  the request callback. These are not durable events; completed-cell output is
  authoritative. Sending both as ordinary streams would duplicate output.
- `src/py_agent/session_journal.py` and `journal_worker.py`: optional private
  audit persistence, not a public transport schema or restartable worker session.

## CLI contract (proposed)

```sh
py --json --model PROVIDER/MODEL "Review this repository"
printf '%s' 'Summarize the project' | py --json --model PROVIDER/MODEL
py --json --provider fake "hello" >events.jsonl
```

Add `--json` and zero or more positional prompts to `default_parser()`.

- In JSON mode, submit positional prompts sequentially in the same coordinator.
  Each argument is one submission; do not split on embedded newlines.
- With no positional prompts, read non-TTY stdin once as one UTF-8 prompt.
  This is plain prompt text, not JSON commands. Empty/whitespace-only input is a
  usage error. With TTY stdin and no prompt, fail rather than launch a composer.
- Positional prompts take precedence: do not read stdin in that case.
- Reject positional prompts without `--json`, and reject `--json` combined with
  terminal-only composer flags such as `--vi` or `--multiline`.
- Do not relax the TTY requirement for the existing terminal path.
- Preserve provider/model selection, explicit plugin configuration, executor
  selection, journal settings, context limits, and `--max-steps`.
- No frontend menus, `/login` interaction, completion, or input prompts.
  Keep ordinary coordinator routing for supplied text; do not add terminal-local
  slash commands. Submit with `allow_stdin=False` and no input handler.
  A cell calling `input()`/`getpass()` must get the existing input-unavailable
  behavior, never consume the prompt pipe or hang.
- Execution remains unrestricted by default. Send its warning and human-readable
  startup diagnostics to stderr, never stdout.

## JSONL v1 wire contract

Every stdout line is one compact UTF-8 JSON object terminated by `\n`.
Flush each record. No banners, ANSI sequences, Markdown fences, Python reprs,
blank lines, or pretty-printed JSON. Worker stdout/stderr belongs inside records;
CLI diagnostics belong on process stderr.

Use a dedicated serializer with explicit field allowlists. Do not serialize
dataclass internals or reuse the journal's private encoding. Thaw immutable
mappings recursively; encode only JSON-compatible values and reject unsupported
values explicitly. Do not use `default=str` to silently corrupt the schema.

Every record has `type`, `schema_version: 1`, `session_id`, and `seq`.
`seq` starts at zero and increases for every emitted wire record.
Preserve the coordinator's separate sequence as `event_sequence`; it may have
gaps after provisional events are suppressed. Order callback delivery serially.

| Type | Additional fields | Meaning |
| --- | --- | --- |
| `session` | `provider`, `model`, `executor` | First record, after initialization succeeds; no credentials/config dump. |
| `submission_start` | `submission_id`, `index` | Frontend-generated ID and zero-based input index, emitted before calling `submit`. |
| `output` | `submission_id`, `event_sequence`, `origin`, `kind`, `data`, `metadata`, `display_id`, `author` | One authoritative coordinator event. |
| `submission_end` | `submission_id`, `status`, `message`, `execution_count`, `error` | Exactly one terminal outcome per started submission while the sink remains writable. |
| `error` | `submission_id` (nullable), `code`, `message` | CLI/frontend/provider failure, not a duplicate execution-output error. |
| `session_end` | `status`, `exit_code`, `submissions_completed` | Last record after cleanup, when stdout remains writable. |

`origin` explicitly contains `session_id`, `request_id`, `frontend_id`,
`config_revision`, `generation_id`, and `execution_id`; optional IDs are null.
The frontend submission ID correlates lifecycle records before `submit()` has
allocated its request ID. Never fabricate coordinator IDs.

Preserve MIME bundles, display IDs, clear/update semantics, and structured error
data in `output`. Rich assets retain existing safety/capture limits; truncation
notices and archive references must remain visible. Worker `outputs[index]`
references are session-local and are not promised retrievable after process exit.
This stream is not lossless archival; see
[the unified-session-record proposal](unified-session-record.md).

`submission_end.message` is an aggregate convenience field and can repeat text
already emitted through `say()` displays. Consumers render either output events
or that summary, not both. Do not attach a second full copy of
`Submission.events` or execution stdout/stderr to the summary.

Ignore callbacks with `metadata.provisional = true` in v1. Emit other callback
events once, then reconcile `Submission.events` by `(origin, sequence)` to deliver
only missing events. Bound deduplication state to the current submission.
Provider token streaming and provisional output revisions are future extensions.

## Completion, failure, and cancellation

A successful Python cell is not necessarily a finished agent request. In
particular, a step-limit submission can contain a successful last cell while
the request is paused. JSON mode must not report that as successful completion.

Define submission statuses `completed`, `failed`, `paused`, `cancelled`, and
`uncertain`. Session status uses the same values, with the first non-completed
submission stopping the input batch.

- `completed`: a direct cell succeeded, a coordinator command completed, or the
  agent reached its successful `say(final=True)` completion boundary.
- `failed`: an unhandled request/provider/frontend failure or a failed direct
  execution. An intermediate agent cell error that the agent recovers from is
  not itself a failed submission.
- `paused`: a step cap or another boundary requires user intervention.
- `cancelled`: interruption was acknowledged.
- `uncertain`: execution may have happened but its outcome is unknown.
  Stop; never replay the cell automatically.

`Submission` currently has no explicit overall outcome. Add a typed,
frontend-neutral outcome (and reason/error where needed) in `contracts.py`,
populated by the runner/agent at **every** return boundary, including format
failure, step limit, and cancellation/uncertainty paths. Exceptions still need
frontend handling. Preserve terminal behavior and cover the new contract in
coordinator tests. Do not infer the outcome from human-readable `message`,
presence of any historical final display, or only `Submission.result.status`.

Proposed exit codes:

| Code | Meaning |
| --- | --- |
| 0 | All supplied submissions completed. |
| 1 | Failed request, setup/cleanup failure, or output transport failure. |
| 2 | Invalid arguments/input (before starting a session). |
| 3 | Paused or uncertain; requires intervention, not automatic retry. |
| 130 | SIGINT/KeyboardInterrupt cancellation. |
| 143 | SIGTERM cancellation. |

Pre-session argparse/input/setup failures go to stderr and may produce no JSON.
After the session header, report failures structurally where possible and finish
with `session_end`. A caught request failure gets one `submission_end`; do not
pretend a partially emitted stream is complete.

Use the existing coordinator interruption/shutdown machinery. Always close the
coordinator and journal, including provider failures and output-write failures.
Do not emit a successful `session_end` until cleanup has succeeded. A broken pipe
means no further writes to stdout: stop active work, clean up, exit nonzero, and
do not retry the request. For slow readers, apply bounded backpressure; do not
accumulate an unbounded queue or silently drop records. If cancellation cannot
confirm the execution outcome, retain `uncertain` in the terminal outcome while
using the signal exit code. No automatic resume, `/resume` exchange, or execution
replay belongs in this one-shot frontend.

## Implementation plan

1. **Separate startup from rendering.** In `cli.py`, share configuration,
   coordinator construction, and journal lifetime between terminal and JSON
   branches. Branch before the TTY gate and before banners/`PlainTerminal`;
   preserve the existing terminal path.
2. **Make outcomes explicit.** Extend the submission contract and populate all
   runner/agent return paths as described above. Pauses that need human input
   terminate the batch rather than leave the process awaiting a composer.
3. **Add `src/py_agent/json_frontend.py`.** Own prompt iteration, JSONL framing,
   wire sequence numbers, explicit serialization, progress callbacks, event
   deduplication, final summaries, and error/exit mapping. Reuse
   `coordinator.submit()`; do not implement a second agent loop or execute cells
   directly. Keep renderer state separate from model context and journal state.
4. **Keep stdout pure.** Audit core/plugin startup prints, provider diagnostics,
   cleanup, and worker capture. Route host diagnostics to stderr without
   redirecting captured worker streams out of the event payloads. An explicitly
   selected plugin that writes directly to process stdout can violate this
   transport; document/enforce the output discipline at the integration boundary.
5. **Test and document the shipped feature.** Add subprocess coverage in
   `tests/test_json_frontend.py`, alongside coordinator outcome tests. Update
   README usage and its “Batch/JSON frontends ... are not implemented” statement
   only when the feature actually ships.

### Acceptance tests

- Run the fake provider without a TTY or credentials. Parse every stdout line
  using `json.loads`; assert one first header, one last session end, monotonic
  wire sequence, and no terminal/banner output.
- Cover positional input, multiline piped input, missing/empty input, prompt
  precedence over stdin, and conflicting terminal flags.
- Submit two direct cells that assign/read a variable to prove sequential
  persistent-worker execution and submission correlation.
- Exercise progress, stdout/stderr, `say()`, failed staged finals, execution
  errors, display updates/clears, MIME data, and bounded/archive output.
  Assert no duplication from callback events, returned events, or provisional
  previews.
- Test successful agent completion, recoverable intermediate cell error,
  provider failure, invalid generated code, direct-cell failure, and step-cap
  pause. A successful last cell must not hide a paused outcome.
- Test `input()`/`getpass()` unavailability with timeouts; never echo passwords.
- Simulate SIGINT, SIGTERM, uncertain execution, startup and cleanup failures,
  a slow reader, and a closed pipe. Assert cleanup and no automatic replay.
- Keep existing terminal and coordinator tests passing. Validate against the
  explicit v1 schema; permit unknown future fields/types in consumer examples.

### Consumer example (proposed feature)

```sh
set -o pipefail
py --json --model PROVIDER/MODEL "Summarize this project" |
  jq -c 'select(.type == "submission_end") | {status, message}'
```

Consumers must inspect the process exit code and final `session_end`, not assume
EOF or a final-looking display means success. Ignore unknown event types/fields
within v1; incompatible semantic changes require a new schema version.

## Non-goals for v1

No bidirectional RPC, interactive JSON input replies, live provider-token deltas,
session restoration, automatic replay, or pi wire compatibility. A future RPC
frontend can share this encoder/schema while adding a separate command channel.
