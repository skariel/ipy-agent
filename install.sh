#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PREFIX=${PREFIX:-"$HOME/.local"}
command -v cargo >/dev/null 2>&1 || { echo "Rust (cargo) is required" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "CPython 3 is required" >&2; exit 1; }
cargo build --release --locked --manifest-path "$ROOT/transition/Cargo.toml"
mkdir -p "$PREFIX/bin"
install -m 755 "$ROOT/transition/target/release/py" "$PREFIX/bin/py"
printf 'Installed %s/bin/py\n' "$PREFIX"
