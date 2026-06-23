#!/usr/bin/env bash
# ============================================================================
# BotCircuits — install the workflow skills into a host agent
# ============================================================================
# Copies the `botcircuits-workflow-authoring` and `botcircuits-workflow-running`
# skills into a host agent's skills directory so the agent (claude-code, hermes,
# …) can author and run BotCircuits workflows from natural language. The
# `botcircuits-` prefix keeps these skills clearly separated from any others in
# the target directory:
#
#     claude > "create an order fulfillment workflow with ..."
#     claude > "run order fulfillment"
#
# Usage:
#   scripts/install-skills.sh [--target <dir>] [--link]
#
#   --target <dir>   Skills directory to install into.
#                    Default: ~/.claude/skills  (Claude Code, personal scope)
#                    Project scope:  .claude/skills
#                    Hermes:         point at its skills dir, e.g.
#                                    ~/.hermes/skills
#   --link           Symlink the skill folders instead of copying (edits to the
#                    repo are reflected live; good for development).
#
# The skills shell out to the `botcircuits` CLI (e.g. `botcircuits workflow
# run`), so the `botcircuits` package must be installed and on PATH in the host
# agent's environment (install this repo, e.g. `uv sync` / `pip install -e .`).
# This script prints a reminder if it can't import it.
# ============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILLS_SRC="$REPO_ROOT/skills"
TARGET="${HOME}/.claude/skills"
MODE="copy"

while [ $# -gt 0 ]; do
    case "$1" in
        --target) TARGET="$2"; shift 2 ;;
        --link)   MODE="link"; shift ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [ -t 1 ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; GRN=$'\033[32m'; YLW=$'\033[33m'; RST=$'\033[0m'
else
    BOLD=""; DIM=""; GRN=""; YLW=""; RST=""
fi
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s!%s %s\n' "$YLW" "$RST" "$*" >&2; }

mkdir -p "$TARGET"

for skill in botcircuits-workflow-authoring botcircuits-workflow-running; do
    src="$SKILLS_SRC/$skill"
    dst="$TARGET/$skill"
    [ -d "$src" ] || { echo "missing skill source: $src" >&2; exit 1; }
    rm -rf "$dst"
    if [ "$MODE" = "link" ]; then
        ln -s "$src" "$dst"
        ok "linked $skill → $dst"
    else
        cp -R "$src" "$dst"
        ok "installed $skill → $dst"
    fi
done

# Warn if the botcircuits package isn't importable — the running skill needs it.
if ! python -c "import botcircuits" >/dev/null 2>&1; then
    warn "the 'botcircuits' package is not importable in this Python env."
    warn "Install the repo so the skills can drive the engine, e.g.:"
    warn "    cd $REPO_ROOT && uv sync     # or: pip install -e ."
fi

echo
echo "${BOLD}Skills installed.${RST} Try in your agent:"
echo "  ${DIM}\"create an order fulfillment workflow with ...\"${RST}"
echo "  ${DIM}\"run order fulfillment\"${RST}"
