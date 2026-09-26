#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv is required to install py-agent" >&2
    exit 1
fi

# Install a private, non-editable copy. The default local executor does not
# require srt, bubblewrap, socat, or any other isolation runtime.
echo "Installing py-agent from $ROOT"
uv tool install --force --reinstall --link-mode copy "$ROOT"
echo
echo "Installed. Start the deterministic fake provider with: $ROOT/run.sh"
echo "For a real model: PROVIDER=codex MODEL=openai-codex/MODEL $ROOT/run.sh"
echo "Execution is unrestricted by default; use your own isolation wrapper if needed."
