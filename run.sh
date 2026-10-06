#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PY_AGENT_BIN="${PY_AGENT_BIN:-${UV_TOOL_BIN_DIR:-$HOME/.local/bin}/py}"

if [[ ! -x "$PY_AGENT_BIN" ]]; then
    echo "error: py-agent is not installed at $PY_AGENT_BIN" >&2
    echo "Run $ROOT/install.sh first, or set PY_AGENT_BIN." >&2
    exit 1
fi

# Management commands have their own parsers; do not prepend terminal options.
case "${1:-}" in
    kernel|kernels|attach|login|logout|auth) exec "$PY_AGENT_BIN" "$@" ;;
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

# The installed launcher defaults to Codex GPT-6.1-Sol (medium effort is the
# CLI default). Flags, PROVIDER/MODEL, or a selected config take precedence.
selected_provider=""
if [[ "$has_provider" == false ]]; then
    if [[ -n "${PROVIDER:-}" ]]; then
        selected_provider="$PROVIDER"
        arguments+=(--provider "$selected_provider")
    elif [[ "$has_config" == false ]]; then
        selected_provider="codex"
        arguments+=(--provider "$selected_provider")
    fi
fi
if [[ "$has_model" == false ]]; then
    if [[ -n "${MODEL:-}" ]]; then
        arguments+=(--model "$MODEL")
    elif [[ "$has_config" == false && "$has_provider" == false && "$selected_provider" == "codex" ]]; then
        arguments+=(--model "openai-codex/gpt-6.1-sol")
    fi
fi

if [[ -n "${WORKSPACE:-}" ]]; then
    cd -- "$WORKSPACE"
fi
exec "$PY_AGENT_BIN" "${arguments[@]}" "$@"
