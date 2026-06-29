"""Streaming and blocking output renderers for the CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from botcircuits.cli.ansi import C, Spinner, out

if TYPE_CHECKING:
    from botcircuits.agent import Agent
    from .commands import CLIState


def preview(s: str, n: int = 200) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


async def run_streaming(agent: "Agent", msg: str, state: "CLIState") -> None:
    spinner = Spinner()

    def _print_prefix() -> None:
        out(C.bold(C.cyan("argus> ")), end="")

    _print_prefix()
    spinner.start()  # spins until the first event of this turn arrives
    saw_text = False
    sid = state.session_id
    last_was_text = False

    async for ev in agent.chat_stream(msg, session_id=sid, system=state.system):
        if ev.session_id and not state.session_id:
            state.session_id = ev.session_id  # capture new session id

        if ev.type == "text_delta":
            spinner.stop()
            out(ev.text, end="", flush=True)
            saw_text = True
            last_was_text = True

        elif ev.type == "tool_call":
            spinner.stop()
            if last_was_text:
                out()  # break the line we were streaming on
                last_was_text = False
            tc = ev.tool_call
            args_preview = preview(str(tc.arguments), 120)
            out(C.magenta(f"  ▸ tool_call  {tc.name}({args_preview})"))

        elif ev.type == "tool_result":
            spinner.stop()
            color = C.red if ev.is_error else C.green
            label = "error" if ev.is_error else "result"
            shown = ev.text if state.show_tool_results else preview(ev.text or "", 200)
            out(color(f"  ◂ {label}      ") + (shown or "(empty)"))
            # Next round of assistant text starts fresh; reprint prefix.
            _print_prefix()
            spinner.start()
            last_was_text = False

        elif ev.type == "turn_end":
            # Internal marker; useful only for debugging. Ignore in normal output.
            pass

        elif ev.type == "done":
            spinner.stop()
            if not saw_text and ev.text:
                # Some providers/turns won't have streamed any text deltas
                # (rare, but possible). Print the final text we got.
                out(ev.text, end="")
            out()  # newline at the end of the turn
            return

        elif ev.type == "error":
            spinner.stop()
            out()
            out(C.red(f"[error] {ev.text}"))
            return

    spinner.stop()


async def run_blocking(agent: "Agent", msg: str, state: "CLIState") -> None:
    async with Spinner():
        reply, sid = await agent.chat(msg, session_id=state.session_id,
                                      system=state.system)
    state.session_id = sid
    out(C.bold(C.cyan("argus> ")) + reply)
