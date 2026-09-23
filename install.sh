#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "error: py-agent currently requires Linux" >&2
    exit 1
fi

missing=()
for command in uv srt bwrap socat rg; do
    command -v "$command" >/dev/null 2>&1 || missing+=("$command")
done
if ((${#missing[@]})); then
    echo "error: missing required commands: ${missing[*]}" >&2
    echo "Install uv, srt, bubblewrap, socat, and ripgrep, then retry." >&2
    exit 1
fi

echo "Installing py-agent from $ROOT"
uv tool install --force --reinstall --link-mode copy "$ROOT"
echo
echo "Installed. Start it with: $ROOT/run.sh"
