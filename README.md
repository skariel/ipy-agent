# py

A Rust CLI for a Python coding agent, backed by a persistent CPython worker.
The implementation, embedded specification, worker and tests live in
[`transition/main.rs`](transition/main.rs).

## Requirements

- Rust stable and Cargo
- CPython 3 available as `python3`
- A supported provider account/API key for model calls (not required for offline tests)

## Run

```sh
./run.sh
./run.sh --help
./run.sh --spec
```

`run.sh` builds the debug binary and forwards all arguments. Run the binary
directly after building to avoid build output:

```sh
cargo build --locked --manifest-path transition/Cargo.toml
transition/target/debug/py
```

Install a release binary:

```sh
./install.sh                      # ~/.local/bin/py
PREFIX=/your/prefix ./install.sh  # /your/prefix/bin/py
```

Ensure that directory is on `PATH`. The installed program still requires CPython.

## Behavior

The agent replies with ordinary Python and executes it in a persistent namespace.
`agent` provides shell/model/background/context operations; `H` exposes durable
history. Outputs remain in history until explicitly selected into model context.
Use `agent.help()` for on-demand API documentation and `/help` for CLI commands.
For focused command help, use `/help resume` or `/help /resume`; aliases work too
(e.g. `/help thinking`). Tab completes command names after `/help `.

Sessions restore history and selected context into **fresh Python**, never by
replaying old code. Journals require the current schema, explicit ownership, and
a frozen startup snapshot. There are no old-format migrations: start a new
session if an older journal is rejected.

The built-in specification (`py --spec`) records detailed contracts and outstanding
acceptance work. Passing offline tests is not a claim of live-provider parity.
The former Python/IPython application and plugin system are not supported.

## Verification

```sh
cd transition
cargo fmt --check
cargo build --locked
env -u PY_CACHE_APPEND_ONLY PY_HARNESS_BIN="$PWD/target/debug/py" \
  cargo test --locked -- --test-threads=2
```

`PY_HARNESS_BIN` ensures process tests exercise the newly built CLI.
The tests use isolated temporary homes and local provider fixtures.
Unset `PY_CACHE_APPEND_ONLY` for standard context behavior.
