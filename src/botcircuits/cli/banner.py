"""Startup banner and info panel — Hermes-style TUI splash screen.

Rendered once at CLI startup via `print_banner()`.
Uses `rich` for the bordered panel; falls back silently if not a TTY.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from botcircuits.agent import Agent
    from botcircuits.providers.base import LLMProvider
    from botcircuits.cli.config import CLIConfig

# ASCII art for "ARGUS" in a chunky block style
_ARGUS_ART = """\
 ▄▄▄     ██▀███    ▄████  █    ██   ██████
▒████▄   ▓██ ▒ ██▒ ██▒ ▀█▒ ██  ▓██▒▒██    ▒
▒██  ▀█▄ ▓██ ░▄█ ▒▒██░▄▄▄░▓██  ▒██░░ ▓██▄
░██▄▄▄▄██▒██▀▀█▄  ░▓█  ██▓▓▓█  ░██░  ▒   ██▒
 ▓█   ▓██░██▓ ▒██▒░▒▓███▀▒▒▒█████▓ ▒██████▒▒
 ▒▒   ▓▒█░ ▒▓ ░▒▓░ ░▒   ▒ ░▒▓▒ ▒ ▒ ▒ ▒▓▒ ▒ ░
  ▒   ▒▒ ░ ░▒ ░ ▒░  ░   ░ ░░▒░ ░ ░ ░ ░▒  ░ ░
  ░   ▒    ░░   ░ ░ ░   ░  ░░░ ░ ░ ░  ░  ░
      ░  ░  ░           ░    ░           ░  """

# Small robot mascot (left column of the info panel)
_ROBOT = """\
   .-------.
  /  O   O  \\
 |   \\___/   |
 |  botcir   |
  \\ cuits  /
   `-------'
   |  | |  |
  /|  | |  |\\
 / |  | |  | \\
"""


def _group_tools(tool_names: list[str]) -> dict[str, list[str]]:
    """Best-effort grouping of tool names by prefix for display."""
    groups: dict[str, list[str]] = {}
    for name in sorted(tool_names):
        prefix = name.split("_")[0] if "_" in name else "general"
        groups.setdefault(prefix, []).append(name)
    return groups


def print_banner(
    agent: "Agent",
    provider: "LLMProvider",
    cfg: "CLIConfig",
    version: str = "0.1.0",
) -> None:
    """Print the full startup banner + info panel to stdout."""
    if not sys.stdout.isatty():
        return

    try:
        from rich.console import Console
        from rich.columns import Columns
        from rich.panel import Panel
        from rich.text import Text
        from rich.table import Table
        from rich import box
    except ImportError:
        return

    console = Console()

    # --- Banner ---
    banner_text = Text(_ARGUS_ART, style="bold yellow")
    console.print(banner_text)

    # --- Build left column: robot mascot + session info ---
    left_lines = Text()
    left_lines.append(_ROBOT, style="dim yellow")
    left_lines.append(f"\n{provider.model[:28]}...\n" if len(provider.model) > 28
                      else f"\n{provider.model}\n", style="dim")
    left_lines.append(f"  {cfg.provider}\n", style="dim cyan")
    session_label = cfg.session or "(new session)"
    left_lines.append(f"  {session_label}\n", style="dim")

    # --- Build right column: tools + skills ---
    tools = agent.tools.all()
    tool_names = [t.name for t in tools]
    skills = getattr(agent, "local_skills", [])

    right = Text()

    right.append("Available Tools\n", style="bold yellow")
    if tool_names:
        # Show up to 3 categories, truncate the rest
        groups = _group_tools(tool_names)
        for prefix, names in list(groups.items())[:6]:
            shown = ", ".join(names[:4])
            trail = ", ..." if len(names) > 4 else ""
            right.append(f"  {prefix}: ", style="dim cyan")
            right.append(f"{shown}{trail}\n", style="white")
    else:
        right.append("  (none)\n", style="dim")

    right.append("\nAvailable Skills\n", style="bold yellow")
    if skills:
        # Group skills by category prefix (e.g. "botcircuits-workflow-authoring" → "botcircuits")
        skill_groups: dict[str, list[str]] = {}
        for sk in skills:
            cat = sk.name.split("-")[0] if "-" in sk.name else "general"
            skill_groups.setdefault(cat, []).append(sk.name)
        for cat, names in list(skill_groups.items())[:10]:
            shown = ", ".join(names[:3])
            trail = ", ..." if len(names) > 3 else ""
            right.append(f"  {cat}: ", style="dim cyan")
            right.append(f"{shown}{trail}\n", style="white")
    else:
        right.append("  (none)\n", style="dim")

    right.append(
        f"\n  {len(tool_names)} tools · {len(skills)} skills · /help for commands\n",
        style="dim",
    )

    # Combine into a two-column layout inside a panel
    table = Table.grid(padding=(0, 2))
    table.add_column(width=18)
    table.add_column()
    table.add_row(left_lines, right)

    subtitle = (
        f"[dim]Argus v{version}[/dim]  ·  "
        f"[dim cyan]{cfg.provider}[/dim cyan]  ·  "
        f"[dim]{provider.model}[/dim]"
    )
    panel = Panel(
        table,
        subtitle=subtitle,
        border_style="yellow",
        box=box.SQUARE,
        padding=(0, 1),
    )
    console.print(panel)

    # --- Welcome line ---
    console.print(
        "[bold]Welcome to Argus![/bold] Type your message or [cyan]/help[/cyan] for commands."
    )
    console.print(
        "[dim]* Tip: Use /workflow add to author a workflow, /tools to list all tools.[/dim]"
    )
    console.print()
