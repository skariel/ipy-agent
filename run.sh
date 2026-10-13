#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cargo build --locked --manifest-path "$ROOT/transition/Cargo.toml"
exec "$ROOT/transition/target/debug/py" "$@"
