#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PY_AGENT_BIN="${PY_AGENT_BIN:-${UV_TOOL_BIN_DIR:-$HOME/.local/bin}/py}"

if [[ ! -x "$PY_AGENT_BIN" ]]; then
    echo "error: py-agent is not installed at $PY_AGENT_BIN" >&2
    echo "Run $ROOT/install.sh first, or set PY_AGENT_BIN." >&2
    exit 1
fi

# Kernel management has its own parser and requires an explicit provider or
# selected configuration when starting; do not prepend terminal options.
case "${1:-}" in
    kernel|kernels|attach) exec "$PY_AGENT_BIN" "$@" ;;
esac

arguments=()
has_provider=false
has_model=false
has_config=false
for argument in "$@"; do
    case "$argument" in
        --provider|--provider=*) has_provider=true ;;
        --model|--model=*) has_model=true ;;
        --config|--config=*) has_config=true ;;
    esac
done

# No implicit paid provider request. Override through CLI flags or the explicit
# PROVIDER and MODEL environment variables; the CLI validates combinations.
if [[ "$has_provider" == false ]]; then
    if [[ -n "${PROVIDER:-}" ]]; then
        arguments+=(--provider "$PROVIDER")
    elif [[ "$has_config" == false ]]; then
        arguments+=(--provider fake)
    fi
fi
if [[ "$has_model" == false && -n "${MODEL:-}" ]]; then
    arguments+=(--model "$MODEL")
fi

if [[ -n "${WORKSPACE:-}" ]]; then
    cd -- "$WORKSPACE"
fi
exec "$PY_AGENT_BIN" "${arguments[@]}" "$@"
