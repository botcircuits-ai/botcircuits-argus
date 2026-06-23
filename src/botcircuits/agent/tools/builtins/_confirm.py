"""Shared y/N confirmation + auto-mode helpers used by gated tools.

Tools that mutate state (`shell_exec`, `write_file`, `edit_file`,
`plan_and_confirm`) all gate themselves the same way: print a proposal
to stderr, read y/N from stdin without blocking the event loop, deny by
default. `auto=True` skips the prompt and only prints a warning banner;
non-tty contexts (gateway, piped stdin) force auto on so the tool
doesn't deadlock waiting for input that never arrives.

Each tool calls `effective_auto(auto)` once at construction, then
`confirm(title, lines)` / `warn(title, lines)` per call.
"""

from __future__ import annotations

import asyncio
import os
import sys


def effective_auto(auto: bool) -> bool:
    """True if the tool should skip its y/N prompt. Auto is forced on
    when stdin isn't a TTY — the gateway and piped CLI have no human."""
    return auto or not sys.stdin.isatty()


def is_tty(stream) -> bool:
    return hasattr(stream, "isatty") and stream.isatty()


def color(code: str, s: str) -> str:
    if not is_tty(sys.stderr) or os.getenv("NO_COLOR"):
        return s
    return f"\033[{code}m{s}\033[0m"


def _format_block(title: str, lines: list[str]) -> str:
    body = "\n".join(f"      {ln}" for ln in lines)
    return color("33", f"  ▸ {title}") + "\n" + body + "\n"


def _format_warn(title: str, lines: list[str]) -> str:
    body = "\n".join(f"      {ln}" for ln in lines)
    return color("33", f"  ⚠ {title} (auto mode)") + "\n" + body + "\n"


def warn(title: str, lines: list[str]) -> None:
    """Auto-mode banner. Stderr so streaming stdout stays clean."""
    sys.stderr.write(_format_warn(title, lines))
    sys.stderr.flush()


async def confirm(title: str, lines: list[str], prompt: str = "run? [y/N]: ") -> bool:
    """Prompt on stderr, read y/N from stdin in an executor so the event
    loop keeps spinning. Default is deny (Enter / anything not y/yes)."""
    sys.stderr.write(_format_block(title, lines))
    sys.stderr.write(color("33", f"      {prompt}"))
    sys.stderr.flush()
    loop = asyncio.get_running_loop()
    try:
        answer = await loop.run_in_executor(None, lambda: input(""))
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n")
        return False
    return answer.strip().lower() in ("y", "yes")
