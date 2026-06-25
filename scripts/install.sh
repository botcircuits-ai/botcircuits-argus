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
BOTCIRCUITS_REPO="${BOTCIRCUITS_REPO:-https://github.com/botcircuits-ai/botcircuits-argus.git}"

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

# ── step 6: expose the CLIs on PATH (same approach Hermes' installer uses) ──
# A wrapper per console script in ~/.local/bin, rather than a bare symlink, so
# a stray PYTHONPATH/PYTHONHOME in the caller's shell can't shadow the venv —
# this is also what lets a host agent (Hermes, Claude Code) follow the
# botcircuits-workflow-* skills and shell out to a bare `botcircuits …`.
say "Linking CLIs into ~/.local/bin"
# Termux puts user bins under $PREFIX/bin; everywhere else ~/.local/bin.
if [ -n "${TERMUX_VERSION:-}" ] && [ -n "${PREFIX:-}" ]; then
    BIN_DIR="$PREFIX/bin";   BIN_DISPLAY="\$PREFIX/bin"
else
    BIN_DIR="$HOME/.local/bin"; BIN_DISPLAY="~/.local/bin"
fi
mkdir -p "$BIN_DIR"

VENV_BIN="$BOTCIRCUITS_HOME/.venv/bin"
# One binary: the gateway and manager are `botcircuits gateway`/`manager`
# subcommands, not separate scripts.
if [ ! -x "$VENV_BIN/botcircuits" ]; then
    warn "botcircuits not found in the venv; re-run 'uv sync' in $BOTCIRCUITS_HOME"
else
    cat > "$BIN_DIR/botcircuits" <<EOF
#!/usr/bin/env bash
# BotCircuits CLI wrapper — generated by scripts/install.sh. Scrubs the env
# (same fix Hermes uses) and execs the install's venv console script.
unset PYTHONPATH PYTHONHOME
exec "$VENV_BIN/botcircuits" "\$@"
EOF
    chmod +x "$BIN_DIR/botcircuits"
fi
ok "Linked botcircuits → ${BIN_DISPLAY}"

# Ensure the bin dir is on PATH via the shell rc (idempotent), like Hermes.
if [ -z "${TERMUX_VERSION:-}" ]; then
    case "$SHELL" in
        *zsh)  SHELL_RC="$HOME/.zshrc" ;;
        *bash) SHELL_RC="$HOME/.bashrc"; [ -f "$SHELL_RC" ] || SHELL_RC="$HOME/.bash_profile" ;;
        *)     SHELL_RC="" ;;
    esac
    if ! printf '%s' ":$PATH:" | grep -q ":$BIN_DIR:"; then
        if [ -n "$SHELL_RC" ]; then
            touch "$SHELL_RC" 2>/dev/null || true
            if ! grep -q '\.local/bin' "$SHELL_RC" 2>/dev/null; then
                {
                    echo ""
                    echo "# BotCircuits — ensure ~/.local/bin is on PATH"
                    echo 'export PATH="$HOME/.local/bin:$PATH"'
                } >> "$SHELL_RC"
                ok "Added ${BIN_DISPLAY} to PATH in ${SHELL_RC}"
            fi
        fi
        warn "${BIN_DISPLAY} is not on PATH in THIS shell — open a new terminal, or run: export PATH=\"$BIN_DIR:\$PATH\""
    else
        ok "${BIN_DISPLAY} already on PATH"
    fi
fi

# ── step 7: install workflow skills into host agents (best-effort) ──────────
# So the user can immediately drive workflows from natural language inside
# Claude Code / Hermes right after install. Done via the CLI itself
# (`botcircuits skills install`) — into whichever agent dirs already exist.
say "Installing workflow skills into available host agents"
BC="$VENV_BIN/botcircuits"
installed_any=""
if [ -d "$HOME/.claude" ]; then
    ( unset PYTHONPATH PYTHONHOME; "$BC" skills install --agent claude ) \
        >/dev/null 2>&1 && { ok "Skills → ~/.claude/skills (Claude Code)"; installed_any=1; }
fi
if [ -d "$HOME/.hermes" ]; then
    ( unset PYTHONPATH PYTHONHOME; "$BC" skills install --agent hermes ) \
        >/dev/null 2>&1 && { ok "Skills → ~/.hermes/skills (Hermes)"; installed_any=1; }
fi
[ -n "$installed_any" ] || \
    warn "No Claude/Hermes dir found — run 'botcircuits skills install' later."

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
echo "  2. ${DIM}run the CLI${RST} (open a new terminal first if PATH was just updated)"
echo "       botcircuits"
echo
echo "  3. ${DIM}or the gateway / manager${RST}"
echo "       botcircuits gateway serve"
echo "       botcircuits manager start"
echo
echo "  4. ${DIM}then, inside Claude Code or Hermes:${RST}"
echo "       \"create an order fulfillment workflow with ...\"   ${DIM}# author${RST}"
echo "       \"run order fulfillment\"                            ${DIM}# run${RST}"
echo
echo "     ${DIM}(install skills into another agent later: botcircuits skills install --agent hermes)${RST}"
echo
echo "  Docs: https://github.com/botcircuits-ai/botcircuits-agent"
