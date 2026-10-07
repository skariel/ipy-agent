# Unified session record and bounded views

Status: proposal; no runtime changes implemented.

## Idea

Retain session data once, and render it selectively. The terminal transcript and
model context should be bounded views of a durable session record, rather than
the only copies of output.

Large-output reduction and context collapse become view operations: replace
visible content with excerpts, summaries, and stable references, while keeping
the retained originals inspectable.

## Current behavior

- The local worker executes cells with IPython history enabled. `In[n]` stores
  source; `Out[n]` caches expression results, not stdout, stderr, or displays.
- stdout and stderr are captured for execution results. Terminal stream labels
  are cosmetic: there are no corresponding `stdout[n]` / `stderr[n]` dictionaries.
- The terminal maintains its own cell counter, independently of IPython.
- Oversized stream output is stored in `outputs[k]`, bounded and combined as
  stdout followed by stderr. This does not preserve stream interleaving.
- Context collapse stores original conversation records separately in
  `collapsed[k]`.

Relevant starting points: `local_worker.py`, `local_executor.py`,
`plain_terminal.py`, and the context-collapse implementation under `src/py_agent`.

## Proposed model

### Canonical session record

Each executed cell has a stable session-scoped identity and records:

- Author, exact source, IPython execution count, status, and error information.
- Ordered stdout/stderr chunks and derived per-stream text.
- Expression-result events and display/update events, including MIME metadata.
- References to retained rich-display assets where supported.

Use IPython's actual execution count for user-facing `In[n]`, `Out[n]`,
`stdout[n]`, and `stderr[n]` references. Keep a separate stable record ID where
needed for executions without history, restarts, and non-cell conversation
events; do not assume a frontend counter is equivalent.

Conversation messages and collapsed ranges use the same storage/inspection
mechanism, but remain distinct record types. Preserve exact original records
and ordering rather than flattening everything into streams.

### Python inspection surface

- Keep IPython's `In` and `Out` semantics unchanged.
- Expose retained streams through `stdout[n]` and `stderr[n]`, including empty
  entries for completed cells. These may be disk-backed mapping-like objects,
  not necessarily in-memory dictionaries.
- Provide bounded range inspection for large text and structured event records.
  Avoid requiring the model to load an entire large string just to inspect a slice.
- Clearly distinguish display events from `Out[n]`: `display()` and `say()`
  do not necessarily create cached expression results.
- Document record lifetime, missing entries, truncation, and restart behavior.

The model instructions should explain these references and encourage bounded
inspection rather than printing entire archives.

### Terminal and model-context views

- Show a compact `In[n]` reference for executed cells without echoing source.
- Label stream panels with real `stdout[n]` / `stderr[n]` references.
- Use `Out[n]` only for expression results associated with that actual count.
- Keep communication readable: `say()` need not show its Python call.
- Display bounded excerpts and actionable references for long output.
- Collapse substitutes summaries and references in active context; it does not
  delete canonical records.
- Reuse existing cell records when collapsing, rather than duplicating their
  output. Store otherwise-unrecorded conversation content as its own records.

## Storage and safety

"Save everything" means all data within an explicit retention policy, not
unlimited storage or guaranteed retention of secrets.

- Write incrementally to disk-backed session storage; do not accumulate
  unbounded output in memory.
- Redact secrets before persistence and inspection. Exact retention means exact
  retained, redacted records, not necessarily raw bytes.
- Today's capture limits can already discard data. Archiving the existing
  bounded captures alone would not make retention lossless.
- Define quotas, asset limits, cleanup, and session lifetime. Explicitly record
  and surface any dropped or omitted data; never silently claim completeness.
- Preserve event ordering for stream interleaving and display updates.
- Distinguish rich assets from text fallbacks; decide which MIME types/assets
  can safely be retained.
- Handle interrupted cells, worker crashes, and incomplete records explicitly.
- Store expression-result representations/events, not arbitrary pickled live
  Python objects. `Out[n]` remains IPython's live object cache.

## High-level implementation plan

1. **Define identity and record contracts.**
   Specify session IDs, execution IDs, IPython-count mapping, ordered events,
   completion state, redaction, and omission metadata.

2. **Add the canonical store.**
   Introduce append-oriented, disk-backed records and bounded readers. Persist
   output during capture, before current presentation limits discard content.

3. **Expose inspection references.**
   Add stream-history access and bounded record readers to the worker namespace.
   Carry the actual execution count through the executor/frontend contracts.

4. **Make rendering a view.**
   Point terminal labels and model-context excerpts at stored records. Keep
   normal messages uncluttered and make silent executions visible.

5. **Unify large-output handling and collapse.**
   Replace special-case archive copies with canonical references. Preserve
   original conversation content that is not already represented by cell records.
   Migrate or maintain compatibility for `outputs`, `collapsed`, and
   `read_output` before removing old behavior.

6. **Update instructions, documentation, and tests.**
   Teach the model the new inspection surface and retention guarantees.
   Test numbering, empty streams, mixed streams, errors, interrupted execution,
   redaction, oversized output, display updates, collapse retrieval, quota
   exhaustion, and compatibility.

## Open decisions

- On-disk format, indexing, and session cleanup/resumption policy.
- Whether stream mappings return strings, lazy text handles, or both.
- How communication-only cells appear while remaining inspectable.
- Rich-asset retention and safe inspection support.
- Compatibility duration for existing archive names and helpers.

## Success criteria

Every displayed reference resolves to the corresponding retained record.
Presentation reduction and context collapse introduce no additional data loss.
Any capture or retention loss is explicit. Inspection stays bounded, and normal
conversation does not require echoing generated Python source.
