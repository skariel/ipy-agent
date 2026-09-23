#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PY_AGENT_BIN="${PY_AGENT_BIN:-${UV_TOOL_BIN_DIR:-$HOME/.local/bin}/py}"
WORKSPACE="${WORKSPACE:-$ROOT}"
MODEL="${MODEL:-openai-codex/gpt-5.6-sol}"
NETWORK="${NETWORK:-proxy}"

if [[ ! -x "$PY_AGENT_BIN" ]]; then
    echo "error: py-agent is not installed at $PY_AGENT_BIN" >&2
    echo "Run $ROOT/install.sh first, or set PY_AGENT_BIN." >&2
    exit 1
fi

exec "$PY_AGENT_BIN" \
    --workspace "$WORKSPACE" \
    --model "$MODEL" \
    --network "$NETWORK" \
    "$@"
