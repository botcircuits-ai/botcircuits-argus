#!/usr/bin/env bash
# ============================================================================
# DEPRECATED — folded into the CLI as `botcircuits skills install`.
# ============================================================================
# Kept as a thin shim so existing references keep working. Forwards to the CLI,
# translating the old flags:
#
#   scripts/install-skills.sh                 →  botcircuits skills install
#   scripts/install-skills.sh --target DIR    →  botcircuits skills install --target DIR
#   scripts/install-skills.sh --link          →  botcircuits skills install --link
#
# Prefer calling the CLI directly. After `scripts/install.sh`, `botcircuits` is
# on PATH and the installer already runs `skills install` for any detected
# Claude/Hermes agent.
# ============================================================================
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Resolve a botcircuits CLI: PATH first, then the repo venv, then `uv run`.
if command -v botcircuits >/dev/null 2>&1; then
    BC=(botcircuits)
elif [ -x "$REPO_ROOT/.venv/bin/botcircuits" ]; then
    BC=("$REPO_ROOT/.venv/bin/botcircuits")
elif command -v uv >/dev/null 2>&1; then
    BC=(uv run --project "$REPO_ROOT" botcircuits)
else
    echo "botcircuits CLI not found; install the repo first (scripts/install.sh)." >&2
    exit 1
fi

echo "note: install-skills.sh is deprecated — use 'botcircuits skills install'." >&2
exec "${BC[@]}" skills install "$@"
