"""`botcircuits-cli setup ...` — interactive configuration wizard.

Modular wizard inspired by `hermes setup`. Each section is an independently
runnable function that mutates a settings.json layer and (for sections that
own secrets) writes to a sibling `.env`. The dispatcher runs sections in
order when no specific section is requested.

Currently registered sections:
  - llm     LLM provider, model, and API key

Run forms:
  botcircuits setup            # full wizard (all sections)
  botcircuits setup llm        # just the LLM section
  botcircuits setup --user     # default — write to ~/.botcircuits/
  botcircuits setup --local    # write to ./.botcircuits/settings.local.json

Files touched:
  ~/.botcircuits/settings.json   (or project equivalent)  — non-secret config
  ~/.botcircuits/.env             — API keys; loaded on import by botcircuits/__init__.py
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import Callable

from botcircuits.cli.ansi import C, out
from botcircuits.cli.config import ConfigError, _read_raw, _write_raw
from botcircuits.cli.settings import (
    ensure_local_gitignored,
    ensure_parent_dir,
    project_local_settings_path,
    resolve_target_path,
    user_settings_path,
)


# ---------------------------------------------------------------------------
# Provider catalog
# ---------------------------------------------------------------------------

# Hardcoded model lists per provider. Keep these short — the goal is a
# "good default" picker, not an exhaustive catalog. Users can always type a
# free-form model name. The first entry is the default offered.
PROVIDER_CATALOG: dict[str, dict] = {
    "anthropic": {
        "label": "Anthropic Claude",
        "env_var": "ANTHROPIC_API_KEY",
        "api_key_url": "https://console.anthropic.com/settings/keys",
        "models": [
            "claude-opus-4-7",
            "claude-sonnet-4-6"
        ],
    },
    "openai": {
        "label": "OpenAI",
        "env_var": "OPENAI_API_KEY",
        "api_key_url": "https://platform.openai.com/api-keys",
        "models": [
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-4.1",
            "gpt-4.1-mini"
        ],
    },
    "gemini": {
        "label": "Google Gemini",
        "env_var": "GEMINI_API_KEY",
        "api_key_url": "https://aistudio.google.com/app/apikey",
        "models": [
            "gemini-3.5-flash",
            "gemini-3.1-pro-preview",
            "gemini-3.1-flash-lite",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
        ],
    },
}


# ---------------------------------------------------------------------------
# Prompt helpers — small, dependency-free
# ---------------------------------------------------------------------------


def _print_header(title: str) -> None:
    out()
    out(C.cyan(C.bold(f"◆ {title}")))


def _print_info(msg: str) -> None:
    out(C.dim(f"  {msg}"))


def _print_success(msg: str) -> None:
    out(C.green(f"  ✓ {msg}"))


def _print_warning(msg: str) -> None:
    out(C.yellow(f"  ! {msg}"))


def _print_error(msg: str) -> None:
    out(C.red(f"  ✗ {msg}"))


def _prompt(question: str, default: str | None = None, password: bool = False) -> str:
    """Prompt for a line of input. Returns the trimmed value, or the default
    if the user just hit Enter. Ctrl-C exits the wizard."""
    suffix = f" [{default}]" if default else ""
    label = C.yellow(f"  {question}{suffix}: ")
    try:
        if password:
            # getpass writes its own prompt to /dev/tty; pre-print the label
            # so the formatting is consistent with the non-password path.
            value = getpass.getpass(label)
        else:
            value = input(label)
    except (KeyboardInterrupt, EOFError):
        out()
        sys.exit(1)
    value = value.strip()
    return value or (default or "")


def _prompt_choice(question: str, choices: list[str], default: int = 0) -> int:
    """Single-select with arrow-key navigation when stdin is a TTY and
    curses is available; falls back to a numbered prompt otherwise.

    Returns the chosen index. Enter / Space confirms; Esc cancels and
    keeps the default; Ctrl-C exits the wizard.
    """
    if sys.stdin.isatty():
        idx = _curses_radiolist(question, choices, default)
        if idx is not None:
            # Echo the picked choice so the transcript reflects it after
            # curses tears the screen down.
            out(C.green(f"  ✓ {choices[idx]}"))
            return idx
        # curses unavailable — fall through to numbered prompt
    return _numbered_choice(question, choices, default)


def _curses_radiolist(question: str, choices: list[str], default: int) -> int | None:
    """Curses-driven radio list. Returns the chosen index, or None when
    curses can't be initialized (caller should fall back).

    Esc returns the default (matches hermes — cancelling keeps current).
    """
    try:
        import curses
    except ImportError:
        return None

    result: list[int | None] = [None]

    def _draw(stdscr):
        curses.curs_set(0)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
        cursor = default
        scroll_offset = 0

        while True:
            stdscr.clear()
            max_y, max_x = stdscr.getmaxyx()
            row = 0

            try:
                hattr = curses.A_BOLD
                if curses.has_colors():
                    hattr |= curses.color_pair(2)
                stdscr.addnstr(row, 0, question, max_x - 1, hattr)
                row += 1
                stdscr.addnstr(
                    row, 0,
                    "  ↑↓ navigate  ENTER select  ESC cancel",
                    max_x - 1, curses.A_DIM,
                )
                row += 1
            except curses.error:
                pass

            items_start = row + 1
            visible_rows = max_y - items_start - 1
            if cursor < scroll_offset:
                scroll_offset = cursor
            elif cursor >= scroll_offset + visible_rows:
                scroll_offset = cursor - visible_rows + 1

            for draw_i, i in enumerate(
                range(scroll_offset, min(len(choices), scroll_offset + visible_rows))
            ):
                y = draw_i + items_start
                if y >= max_y - 1:
                    break
                radio = "●" if i == cursor else "○"
                arrow = "→" if i == cursor else " "
                line = f" {arrow} ({radio}) {choices[i]}"
                attr = curses.A_NORMAL
                if i == cursor:
                    attr = curses.A_BOLD
                    if curses.has_colors():
                        attr |= curses.color_pair(1)
                try:
                    stdscr.addnstr(y, 0, line, max_x - 1, attr)
                except curses.error:
                    pass

            stdscr.refresh()
            key = stdscr.getch()

            if key in {curses.KEY_UP, ord("k")}:
                cursor = (cursor - 1) % len(choices)
            elif key in {curses.KEY_DOWN, ord("j")}:
                cursor = (cursor + 1) % len(choices)
            elif key in {ord(" "), curses.KEY_ENTER, 10, 13}:
                result[0] = cursor
                return
            elif key in {27, ord("q")}:
                result[0] = default
                return

    try:
        curses.wrapper(_draw)
    except KeyboardInterrupt:
        out()
        sys.exit(1)
    except Exception:
        return None

    _flush_stdin()
    return result[0]


def _flush_stdin() -> None:
    """Drain stray bytes from the stdin buffer after curses exits.

    Arrow-key escape sequences (and terminal mode-switch responses) can
    linger in the OS input buffer past `curses.endwin()`; the next
    `input()` / `getpass()` would otherwise silently swallow them, which
    corrupts user data (e.g. writing `^[^[` into the .env file).
    """
    try:
        if not sys.stdin.isatty():
            return
        import termios
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:
        pass


def _numbered_choice(question: str, choices: list[str], default: int) -> int:
    """Plain numbered prompt — used when curses is unavailable or stdin
    isn't a TTY (piped input, CI)."""
    out(C.yellow(f"  {question}"))
    for i, choice in enumerate(choices):
        marker = "●" if i == default else "○"
        line = f"    {i + 1}. {marker} {choice}"
        out(C.green(line) if i == default else line)
    prompt = C.dim(f"    Select [1-{len(choices)}] ({default + 1}): ")
    while True:
        try:
            raw = input(prompt).strip()
        except (KeyboardInterrupt, EOFError):
            out()
            sys.exit(1)
        if not raw:
            return default
        try:
            idx = int(raw) - 1
        except ValueError:
            _print_error("Please enter a number")
            continue
        if 0 <= idx < len(choices):
            return idx
        _print_error(f"Please enter a number between 1 and {len(choices)}")


# ---------------------------------------------------------------------------
# .env handling — for API keys
# ---------------------------------------------------------------------------


def _env_path_for(settings_path: Path) -> Path:
    """`.env` lives in the same directory as the settings file. For the
    user-level layer that's `~/.botcircuits/.env`; for project layers it's
    `./.botcircuits/.env`."""
    return settings_path.parent / ".env"


def _save_env_value(env_path: Path, key: str, value: str) -> None:
    """Upsert `KEY=value` in a dotenv-style file. Preserves other lines."""
    ensure_parent_dir(env_path)
    lines: list[str] = []
    found = False
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip()
            if stripped.startswith(f"{key}=") or stripped.startswith(f"export {key}="):
                if not found:
                    lines.append(f"{key}={value}")
                    found = True
                # drop duplicates
                continue
            lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        # Best-effort on platforms where chmod is restricted (Windows).
        pass


# ---------------------------------------------------------------------------
# Section: LLM provider, model, API key
# ---------------------------------------------------------------------------


def setup_llm(settings_path: Path) -> None:
    """Configure the LLM provider, model, and API key.

    Writes `provider` and `model` to the targeted settings.json and the
    API key to the sibling .env. Existing values are shown as defaults so
    pressing Enter keeps them.
    """
    _print_header("LLM Provider & Model")
    _print_info("Pick a provider, model, and supply an API key.")

    existing = _read_raw(str(settings_path))
    current_provider = existing.get("provider") if isinstance(existing.get("provider"), str) else None
    current_model = existing.get("model") if isinstance(existing.get("model"), str) else None

    # --- Provider ---
    provider_keys = list(PROVIDER_CATALOG.keys())
    labels = [f"{k} — {PROVIDER_CATALOG[k]['label']}" for k in provider_keys]
    default_idx = provider_keys.index(current_provider) if current_provider in provider_keys else 0
    idx = _prompt_choice("Provider:", labels, default=default_idx)
    provider = provider_keys[idx]
    spec = PROVIDER_CATALOG[provider]

    # --- Model ---
    out()
    model = _choose_model(spec, current_model if current_provider == provider else None)

    # --- API key ---
    env_path = _env_path_for(settings_path)
    env_var = spec["env_var"]
    out()
    _print_info(f"API key URL: {spec['api_key_url']}")
    _print_info(f"Stored as {env_var} in {env_path}")

    existing_key = _read_env_value(env_path, env_var) or os.environ.get(env_var)
    api_key, clear_key = _choose_api_key(env_var, existing_key)

    # --- Persist ---
    existing["provider"] = provider
    existing["model"] = model
    ensure_parent_dir(settings_path)
    _write_raw(str(settings_path), existing)
    _print_success(f"Wrote provider={provider}, model={model} → {settings_path}")

    if clear_key and existing_key:
        _remove_env_value(env_path, env_var)
        _print_success(f"Cleared {env_var} from {env_path}")
    elif api_key and api_key != existing_key:
        _save_env_value(env_path, env_var, api_key)
        _print_success(f"Saved {env_var} → {env_path}")
    elif api_key and api_key == existing_key:
        _print_info(f"{env_var} unchanged.")
    elif not api_key and not clear_key:
        _print_warning(
            f"No API key set — set {env_var} before running the agent."
        )


def _choose_model(spec: dict, current_model: str | None) -> str:
    """Pick a model via radio list. Last entry opens a free-form prompt.

    `current_model` (when supplied and matching the provider) is shown as
    the highlighted default — same UX as if the user had run setup before.
    """
    suggested: list[str] = list(spec["models"])
    custom_label = "Type a custom model name…"

    # If the current model isn't in the suggested list, slot it in at the
    # top so "keep what you had" stays one keystroke away.
    if current_model and current_model not in suggested:
        suggested = [current_model] + suggested

    choices = suggested + [custom_label]
    default = suggested.index(current_model) if current_model in suggested else 0

    idx = _prompt_choice(f"Model ({spec['label']}):", choices, default=default)
    if idx == len(choices) - 1:
        # Custom entry — fall through to a text prompt
        return _prompt(
            "Custom model name",
            default=current_model or suggested[0],
        )
    return suggested[idx]


def _choose_api_key(env_var: str, existing_key: str | None) -> tuple[str | None, bool]:
    """Pick how to handle the API key. Returns `(api_key, clear)`.

    - First run (no existing key): jump straight to a getpass prompt.
    - Existing key present: radio list — Keep / Replace / Clear.

    `clear=True` means the caller should remove the env var. `api_key` is
    the value to persist (either the existing one, a newly entered one,
    or None when cleared/skipped).
    """
    if not existing_key:
        # Nothing to keep — just ask for a key.
        value = _prompt(f"  {env_var}", password=True).strip()
        return (value or None), False

    masked = _mask(existing_key)
    idx = _prompt_choice(
        f"API key ({env_var}):",
        [
            f"Keep existing ({masked})",
            "Replace with a new key",
            "Clear (remove the saved key)",
        ],
        default=0,
    )
    if idx == 0:
        return existing_key, False
    if idx == 1:
        value = _prompt(f"  New {env_var}", password=True).strip()
        if not value:
            _print_info("No key entered — keeping existing.")
            return existing_key, False
        return value, False
    # idx == 2
    return None, True


def _remove_env_value(env_path: Path, key: str) -> None:
    """Drop `KEY=...` lines from a dotenv-style file. No-op if missing."""
    if not env_path.exists():
        return
    kept: list[str] = []
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"export {key}="):
            continue
        kept.append(line)
    env_path.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")


def _read_env_value(env_path: Path, key: str) -> str | None:
    """Read a value from a dotenv-style file. Returns None if missing."""
    if not env_path.exists():
        return None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.lstrip()
        prefix = None
        if stripped.startswith(f"{key}="):
            prefix = f"{key}="
        elif stripped.startswith(f"export {key}="):
            prefix = f"export {key}="
        if prefix:
            return stripped[len(prefix):].strip().strip('"').strip("'")
    return None


def _mask(value: str | None) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]}"


# ---------------------------------------------------------------------------
# Section registry & dispatcher
# ---------------------------------------------------------------------------

# Order matters — full wizard runs sections top to bottom.
SETUP_SECTIONS: list[tuple[str, str, Callable[[Path], None]]] = [
    ("llm", "LLM Provider & Model", setup_llm),
]


def add_setup_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Wire the `setup` command onto the top-level subparser."""
    p = subparsers.add_parser(
        "setup",
        help="Interactive setup wizard (provider, model, API key, ...)",
    )
    p.add_argument(
        "section",
        nargs="?",
        default=None,
        choices=[k for k, _, _ in SETUP_SECTIONS],
        help="Run a single section instead of the full wizard.",
    )
    scope = p.add_mutually_exclusive_group()
    scope.add_argument(
        "--user", action="store_true",
        help="Write to ~/.botcircuits/settings.json (default)",
    )
    scope.add_argument(
        "--local", action="store_true",
        help="Write to ./.botcircuits/settings.local.json (project, gitignored)",
    )
    scope.add_argument(
        "--project", action="store_true",
        help="Write to ./.botcircuits/settings.json (project, shared)",
    )


def _resolve_setup_target(args: argparse.Namespace) -> Path:
    """Pick which settings.json the wizard should write to.

    Default is user-level (~/.botcircuits/settings.json) — matches where
    we put the .env, so a single setup run gives the agent everything it
    needs from any cwd. `--project` and `--local` override.
    """
    if args.local:
        path = project_local_settings_path()
        ensure_parent_dir(path)
        ensure_local_gitignored(path)
        return path
    if args.project:
        return resolve_target_path()  # project shared
    # Default: user-level
    return user_settings_path()


def run_setup_wizard(args: argparse.Namespace) -> int:
    """Entry point invoked by `botcircuits setup ...`."""
    try:
        target = _resolve_setup_target(args)
    except ConfigError as e:
        _print_error(str(e))
        return 2

    out()
    out(C.magenta(C.bold("⚙ BotCircuits Agent Setup")))
    _print_info(f"Writing config to: {target}")

    if args.section:
        for key, label, func in SETUP_SECTIONS:
            if key == args.section:
                func(target)
                out()
                _print_success(f"{label} configuration complete.")
                return 0
        _print_error(f"Unknown section: {args.section}")
        return 2

    for _key, label, func in SETUP_SECTIONS:
        func(target)

    out()
    _print_success("Setup complete.")
    return 0
