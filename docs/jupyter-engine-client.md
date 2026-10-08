# Reusing the Jupyter kernel as the engine server

Status: high-level proposal, not an implemented terminal transport.

## Idea

Use the existing managed Jupyter kernel as the long-lived agent engine.
Make the terminal a client of that kernel rather than introduce another server.

Today:

- The default terminal calls a Coordinator in-process.
- The optional Jupyter kernel owns its own Coordinator and persistent executor.
- Both entry points use the same coordinator implementation, but starting them
  separately does not give them the same live session.
- The executor runs Python in a separate persistent worker process.

Proposed:

```text
Terminal client / notebook client
              |
        Jupyter protocol
              |
   Managed kernel + Coordinator
              |
        LocalExecutor
              |
    Persistent Python worker
```

This would let a terminal detach and reconnect without closing the engine.
A notebook and terminal could address the same kernel, subject to explicit
multi-client ownership rules.

## Existing foundations

- `src/py_agent/jupyter_kernel.py`: adapts execute, completion, inspection,
  stdin, interrupt, and shutdown to coordinator operations. Ordinary text is
  routed as an agent request; `@`, `!`, and `%` select direct execution.
- `src/py_agent/kernel_sessions.py`: managed process lifecycle and private
  connection/session records.
- `src/py_agent/cli.py`: kernel management commands; current `py attach`
  launches stock `jupyter_console`, not our PlainTerminal.
- `src/py_agent/plain_terminal.py`: renders coordinator results but also reads
  coordinator state, activity, recovery, and configuration directly.
- `src/py_agent/contracts.py`: frontend-neutral requests and results.
  QueueTicket includes a local awaitable, so it is not a wire contract.
- `tests/test_jupyter_kernel.py` and `tests/test_kernel_sessions.py`:
  adapter and lifecycle coverage, including progress correlation and stdin.

The adapter can publish progress supplied by the coordinator; this does not
establish continuous worker-output streaming. README still describes the
managed workflow as experimental and not verified end to end.

## Scope

Start local-only, with one actively submitting terminal client. Preserve
stock Jupyter compatibility and the existing in-process terminal path.
Do not add another HTTP server, remote authentication system, or execution
sandbox as part of this work. The kernel remains unrestricted local execution.

A live kernel can preserve a namespace across client disconnects. This is
different from recovering a namespace after the kernel or worker dies.

## Staged plan

### 1. Verify the existing server path

Run an integration smoke test with the optional Jupyter dependencies: start a
managed kernel, connect, execute direct Python and an agent request, disconnect,
and reconnect. Verify namespace persistence, parent-message correlation,
completion, stdin, interruption, and explicit shutdown. Use a fake provider
for offline tests; paid provider calls must remain opt-in.

This is the first decision gate: prove the current server works before moving
the terminal onto it.

### 2. Define a frontend-facing engine interface

Inventory PlainTerminal and its menu/completion helpers' coordinator access.
Replace internal-object access with explicit operations and status snapshots:

- Submit an action and receive a stable request ID.
- Receive correlated progress and final outcomes.
- Read session state, model/configuration metadata, activity, and capabilities.
- Complete/inspect text and answer an owned input request.
- Interrupt; distinguish client detach from engine shutdown.
- Support queueing and steering only when advertised.

Keep an in-process implementation first. Do not serialize Python objects,
callbacks, exceptions, or awaitables. Represent queue completion through IDs
and outcomes instead of sending QueueTicket's local completion handle.

### 3. Add a Jupyter-backed client

Use `jupyter_client` channels for standard execution, IOPub, stdin, completion,
inspection, and control requests. Correlate replies using parent message IDs;
IOPub is shared, so another client's output must not become this client's result.
Deduplicate progress versus final rendering, as the current adapters do.

Standard execute requests are sufficient for a first submission path, but not
necessarily for terminal feature parity. The kernel currently serializes
do_execute with an execution lock. Queued shell execution must not be mistaken
for coordinator steering during an active request.

Define a small versioned extension, using comms or dedicated messages after
a protocol spike, for capabilities, status, queue/steering actions, and request
outcomes. Test responsiveness while execution is busy before choosing that
mechanism. Unsupported features should be visibly unavailable, not silently
emulated with arbitrary execution or private-object access.

### 4. Connect PlainTerminal to the client

Add an explicit managed-session attachment mode for our terminal; keep the
existing stock-console attachment behavior available. Command spelling is
an implementation decision, not a promised API.

On attachment, fetch capabilities and a status snapshot. On terminal exit,
close client channels only. Kernel shutdown remains an explicit operation.
Keep provider credentials, configuration ownership, journal writes, and
execution policy in the engine process rather than duplicating them in clients.

### 5. Specify disconnect and multi-client semantics

Jupyter IOPub is not a durable event log. Reconnecting does not automatically
recover missed output. Choose initially between explicit missing-output notices
and a bounded replay extension with sequence IDs; do not imply full recovery.

Never resubmit an action automatically after a transport failure. A lost reply
does not prove the action did not execute. Before offering retries, add
engine-owned request identity, deduplication, and an outcome query that can
report pending, completed, or uncertain execution.

Define who may interrupt or shut down a shared session. Route stdin only to its
requesting frontend, preserve password handling, and specify what happens if
that frontend disconnects. Start with a single submitting client and other
clients treated as observers; broader concurrency is a separate milestone.

## Acceptance criteria

- In-process terminal behavior remains covered and usable without Jupyter.
- An attached terminal executes each action once and renders only correlated
  results, without duplicating progress output.
- Detach/reconnect preserves the live namespace without implying output replay.
- Input, cancellation, completion, and explicit shutdown work over real channels.
- Disconnects during provider calls and worker execution do not trigger retries.
- Multi-client tests cover output isolation, input ownership, and interruption.
- Unsupported capabilities and dead kernels produce clear user-facing errors.

Connection files contain execution-capable bearer keys. Reuse the existing
private-directory and ownership checks, never display those keys, and retain
terminal output sanitization. This is lifecycle hygiene, not a same-user sandbox.
Remote access and stronger authentication are explicitly outside this plan.

## Recommendation

Reuse and harden the existing kernel boundary before inventing a second server.
Begin with the integration smoke test and the frontend interface. Adopt the
kernel-backed terminal only after transport parity and disconnect semantics
are demonstrated; a server boundary alone will not simplify engine internals.
