"""Streaming and blocking output renderers for the CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from botcircuits.cli.ansi import C, out

if TYPE_CHECKING:
    from botcircuits.agent import Agent
    from .commands import CLIState


def preview(s: str, n: int = 200) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


class _WorkingIndicator:
    """Prints 'argus> ⋯ working...' once when started, nothing on stop.

    All output goes through out() (→ print()) so patch_stdout can safely
    reposition the prompt below it. No raw sys.stdout.write, no cursor-up
    tricks — those bypass patch_stdout's tracking and corrupt the display.
    """

    def __init__(self) -> None:
        self._printed = False

    def start(self) -> None:
        if self._printed:
            return
        out(C.bold(C.cyan("argus> ")) + C.dim("⋯ working..."))
        self._printed = True

    def stop(self) -> None:
        self._printed = False


async def run_streaming(agent: "Agent", msg: str, state: "CLIState") -> None:
    working = _WorkingIndicator()
    working.start()

    saw_text = False
    sid = state.session_id
    last_was_text = False
    prefix_needed = True  # whether to print "argus> " before next text

    async for ev in agent.chat_stream(msg, session_id=sid, system=state.system):
        if ev.session_id and not state.session_id:
            state.session_id = ev.session_id

        if ev.type == "text_delta":
            working.stop()
            if prefix_needed:
                out(C.bold(C.cyan("argus> ")), end="")
                prefix_needed = False
            out(ev.text, end="", flush=True)
            saw_text = True
            last_was_text = True

        elif ev.type == "tool_call":
            working.stop()
            if last_was_text:
                out()
                last_was_text = False
            tc = ev.tool_call
            args_preview = preview(str(tc.arguments), 120)
            out(C.magenta(f"  ▸ tool_call  {tc.name}({args_preview})"))
            prefix_needed = True

        elif ev.type == "tool_result":
            working.stop()
            color = C.red if ev.is_error else C.green
            label = "error" if ev.is_error else "result"
            shown = ev.text if state.show_tool_results else preview(ev.text or "", 200)
            out(color(f"  ◂ {label}      ") + (shown or "(empty)"))
            # Start a fresh working indicator while the model processes the result.
            working.start()
            prefix_needed = True
            last_was_text = False

        elif ev.type == "turn_end":
            pass

        elif ev.type == "done":
            working.stop()
            if prefix_needed and (saw_text or ev.text):
                out(C.bold(C.cyan("argus> ")), end="")
            if not saw_text and ev.text:
                out(ev.text, end="")
            out()
            return

        elif ev.type == "error":
            working.stop()
            out(C.red(f"\nargus> [error] {ev.text}"))
            return

    working.stop()


async def run_blocking(agent: "Agent", msg: str, state: "CLIState") -> None:
    working = _WorkingIndicator()
    working.start()
    try:
        reply, sid = await agent.chat(msg, session_id=state.session_id,
                                      system=state.system)
    finally:
        working.stop()
    state.session_id = sid
    out(C.bold(C.cyan("argus> ")) + reply)
