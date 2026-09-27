# Readability and typing roadmap

## Baseline and first slice

The initial suite passed 592 tests. An unrestricted strict-mypy audit reported
463 errors across 23 modules in the default development environment (including
missing optional Jupyter imports). The existing repository-wide mypy configuration
also reports errors; it is not a clean quality gate.

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
