"""Streaming and blocking output renderers for the CLI."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from botcircuits.cli.ansi import C, out

if TYPE_CHECKING:
    from botcircuits.agent import Agent
    from .commands import CLIState


def preview(s: str, n: int = 200) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


# Emoji icons for known tool name prefixes (mirrors Hermes style)
_TOOL_ICONS: dict[str, str] = {
    "shell": "⚙",
    "grep": "🔍",
    "read": "📄",
    "write": "✏️",
    "edit": "✏️",
    "search": "🔍",
    "web": "🌐",
    "http": "🌐",
    "fetch": "🌐",
    "skill": "🧩",
    "workflow": "🔄",
    "build": "🔨",
    "plan": "📋",
    "memory": "🧠",
    "proc": "⚙",
}


def _tool_icon(name: str) -> str:
    prefix = name.split("_")[0].lower()
    return _TOOL_ICONS.get(prefix, "●")


async def run_streaming(agent: "Agent", msg: str, state: "CLIState") -> None:
    saw_text = False
    sid = state.session_id
    last_was_text = False
    prefix_needed = True
    status_line = False
    # Track per-tool-call start time for elapsed display
    _tool_start: dict[str, float] = {}

    async for ev in agent.chat_stream(msg, session_id=sid, system=state.system):
        if ev.session_id and not state.session_id:
            state.session_id = ev.session_id

        if ev.type == "text_delta":
            if prefix_needed:
                if status_line:
                    out()
                    status_line = False
                out(C.bold(C.cyan("argus> ")), end="")
                prefix_needed = False
            out(ev.text, end="", flush=True)
            saw_text = True
            last_was_text = True

        elif ev.type == "tool_call":
            if last_was_text:
                out()
                last_was_text = False
            if status_line:
                out()
                status_line = False
            tc = ev.tool_call
            icon = _tool_icon(tc.name)
            args_preview = preview(str(tc.arguments), 80)
            _tool_start[tc.name] = time.monotonic()
            out(C.dim(f"  | {icon} {tc.name:<12}  {args_preview}"))
            prefix_needed = True

        elif ev.type == "tool_result":
            col = C.red if ev.is_error else C.green
            label = "error" if ev.is_error else "result"
            shown = ev.text if state.show_tool_results else preview(ev.text or "", 200)
            out(col(f"  ◂ {label}      ") + (shown or "(empty)"))
            out(C.dim("  ⋯ processing..."))
            status_line = True
            prefix_needed = True
            last_was_text = False

        elif ev.type == "turn_end":
            pass

        elif ev.type == "done":
            if status_line:
                out()
                status_line = False
            if prefix_needed and (saw_text or ev.text):
                out(C.bold(C.cyan("argus> ")), end="")
            if not saw_text and ev.text:
                out(ev.text, end="")
            out()
            return

        elif ev.type == "error":
            if status_line:
                out()
            out(C.red(f"argus> [error] {ev.text}"))
            return


async def run_blocking(agent: "Agent", msg: str, state: "CLIState") -> None:
    out(C.dim("  ⋯ thinking..."))
    reply, sid = await agent.chat(msg, session_id=state.session_id,
                                  system=state.system)
    state.session_id = sid
    out(C.bold(C.cyan("argus> ")) + reply)
