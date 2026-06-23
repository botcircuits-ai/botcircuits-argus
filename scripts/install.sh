#!/usr/bin/env bash
# ============================================================================
# BotCircuits Agent — one-line installer
# ============================================================================
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/botcircuits-ai/botcircuits-agent/main/scripts/install.sh | bash
#
# Environment variables:
#   BOTCIRCUITS_HOME   install location          (default: ~/.botcircuits/app)
#   BOTCIRCUITS_REF    git branch / tag / SHA    (default: main)
#   BOTCIRCUITS_REPO   override repo URL         (default: HTTPS to the public repo)
#
# Idempotent: re-running updates the checkout and re-syncs dependencies.
# Refuses to clobber local modifications — commit or stash first.
# ============================================================================

set -euo pipefail

# ── config ─────────────────────────────────────────────────────────────────
BOTCIRCUITS_HOME="${BOTCIRCUITS_HOME:-$HOME/.botcircuits/app}"
BOTCIRCUITS_REF="${BOTCIRCUITS_REF:-main}"
BOTCIRCUITS_REPO="${BOTCIRCUITS_REPO:-https://github.com/botcircuits-ai/botcircuits-agent.git}"

# Inherited PYTHONPATH/PYTHONHOME can shadow the install — same fix Hermes uses.
unset PYTHONPATH PYTHONHOME 2>/dev/null || true
export UV_NO_CONFIG=1

# ── pretty output ──────────────────────────────────────────────────────────
if [ -t 1 ]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[31m'
    GRN=$'\033[32m'; YLW=$'\033[33m'; CYN=$'\033[36m'; RST=$'\033[0m'
else
    BOLD=""; DIM=""; RED=""; GRN=""; YLW=""; CYN=""; RST=""
fi

say()  { printf '%s▸%s %s\n' "$CYN" "$RST" "$*"; }
ok()   { printf '%s✓%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '%s!%s %s\n' "$YLW" "$RST" "$*" >&2; }
die()  { printf '%s✗%s %s\n' "$RED" "$RST" "$*" >&2; exit 1; }

# ── OS check ───────────────────────────────────────────────────────────────
case "$(uname -s)" in
    Darwin|Linux) ;;
    *) die "Unsupported OS: $(uname -s). Only macOS and Linux are supported." ;;
esac

# ── step 1: uv ─────────────────────────────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
    say "Installing uv (https://docs.astral.sh/uv/)"
    curl -fsSL https://astral.sh/uv/install.sh | sh
    # uv installs to ~/.local/bin; surface it in this shell only.
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null 2>&1 \
        || die "uv installed but not on PATH. Add ~/.local/bin to your shell rc and re-run."
    ok "uv installed: $(uv --version)"
else
    ok "uv present: $(uv --version)"
fi

# ── step 2: python ─────────────────────────────────────────────────────────
# uv reads `requires-python` from pyproject.toml and `.python-version` from
# the checkout, then auto-installs a matching interpreter during `uv sync`.
# No explicit `uv python install` step needed.

# ── step 3: clone or update ────────────────────────────────────────────────
mkdir -p "$(dirname "$BOTCIRCUITS_HOME")"

if [ -d "$BOTCIRCUITS_HOME/.git" ]; then
    say "Updating existing checkout at ${BOTCIRCUITS_HOME}"
    # Don't blow away the user's work — let them resolve it.
    if ! git -C "$BOTCIRCUITS_HOME" diff --quiet || \
       ! git -C "$BOTCIRCUITS_HOME" diff --cached --quiet; then
        warn "${BOTCIRCUITS_HOME} has uncommitted changes."
        warn "Commit, stash, or move the checkout aside before re-running."
        die "Refusing to overwrite local modifications."
    fi
    git -C "$BOTCIRCUITS_HOME" fetch --tags origin
    git -C "$BOTCIRCUITS_HOME" checkout --quiet "${BOTCIRCUITS_REF}"
    # Fast-forward only when on a branch; tags/SHAs are detached and don't pull.
    if git -C "$BOTCIRCUITS_HOME" symbolic-ref -q HEAD >/dev/null; then
        git -C "$BOTCIRCUITS_HOME" pull --ff-only origin "${BOTCIRCUITS_REF}"
    fi
    ok "Checkout updated to ${BOTCIRCUITS_REF}"
elif [ -e "$BOTCIRCUITS_HOME" ]; then
    die "${BOTCIRCUITS_HOME} exists but isn't a git checkout. Move it aside or set BOTCIRCUITS_HOME=<other path>."
else
    say "Cloning ${BOTCIRCUITS_REPO} → ${BOTCIRCUITS_HOME}"
    git clone --quiet --branch "${BOTCIRCUITS_REF}" "${BOTCIRCUITS_REPO}" "${BOTCIRCUITS_HOME}" \
        || git clone --quiet "${BOTCIRCUITS_REPO}" "${BOTCIRCUITS_HOME}"
    # Second clone (without --branch) catches the case where REF is a tag/SHA
    # that --branch doesn't accept; check it out explicitly.
    git -C "$BOTCIRCUITS_HOME" checkout --quiet "${BOTCIRCUITS_REF}" 2>/dev/null || true
    ok "Cloned at $(git -C "$BOTCIRCUITS_HOME" rev-parse --short HEAD)"
fi

# ── step 4: dependencies ───────────────────────────────────────────────────
say "Installing dependencies (uv sync)"
# Unset VIRTUAL_ENV so a parent shell's venv doesn't confuse uv. The Python
# version comes from the repo's .python-version + pyproject.toml's
# requires-python; uv auto-installs a matching interpreter if needed.
( cd "$BOTCIRCUITS_HOME" && unset VIRTUAL_ENV && uv sync --quiet )
ok "Dependencies installed"

# ── step 5: .env scaffold ──────────────────────────────────────────────────
if [ ! -f "$BOTCIRCUITS_HOME/.env" ] && [ -f "$BOTCIRCUITS_HOME/.env.example" ]; then
    cp "$BOTCIRCUITS_HOME/.env.example" "$BOTCIRCUITS_HOME/.env"
    ok "Created .env from .env.example"
fi

# ── done ───────────────────────────────────────────────────────────────────
echo
echo "${BOLD}BotCircuits installed.${RST}"
echo
echo "  Location: ${BOTCIRCUITS_HOME}"
echo "  Version:  $(git -C "$BOTCIRCUITS_HOME" describe --tags --always --dirty 2>/dev/null || echo unknown)"
echo
echo "${BOLD}Next:${RST}"
echo "  1. ${DIM}edit your API keys${RST}"
echo "       \$EDITOR ${BOTCIRCUITS_HOME}/.env"
echo
echo "  2. ${DIM}run the CLI${RST}"
echo "       cd ${BOTCIRCUITS_HOME} && uv run botcircuits"
echo
echo "  3. ${DIM}or the gateway${RST}"
echo "       cd ${BOTCIRCUITS_HOME} && uv run botcircuits-gateway"
echo
echo "  Docs: https://github.com/botcircuits-ai/botcircuits-agent"
