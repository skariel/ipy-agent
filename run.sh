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

# The installed launcher defaults to DeepSeek Flash through litelm. A provider
# flag, PROVIDER/MODEL, or an explicitly selected config always takes precedence.
selected_provider=""
if [[ "$has_provider" == false ]]; then
    if [[ -n "${PROVIDER:-}" ]]; then
        selected_provider="$PROVIDER"
        arguments+=(--provider "$selected_provider")
    elif [[ "$has_config" == false ]]; then
        selected_provider="litelm"
        arguments+=(--provider "$selected_provider")
    fi
fi
if [[ "$has_model" == false ]]; then
    if [[ -n "${MODEL:-}" ]]; then
        arguments+=(--model "$MODEL")
    elif [[ "$has_config" == false && "$has_provider" == false && "$selected_provider" == "litelm" ]]; then
        arguments+=(--model "deepseek/deepseek-flash")
    fi
fi

if [[ -n "${WORKSPACE:-}" ]]; then
    cd -- "$WORKSPACE"
fi
exec "$PY_AGENT_BIN" "${arguments[@]}" "$@"
