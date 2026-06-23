"""Wrap the agent so every run emits a DeepEval trace.

Task Completion judges the agent's *trace*: the chat turns, the tool calls,
and the tool results. DeepEval builds that trace from `@observe`-decorated
callables and the `update_current_*` helpers. The agent itself has no DeepEval
dependency, so we monkeypatch two seams at import time:

  - `ToolRegistry.run`  -> one TOOL span per tool call, with input/output set
  - `Agent.chat`        -> one span per chat turn

The actual *trace* (the unit Task Completion scores) is opened by the harness
around the whole task via `observe`, so a multi-turn workflow — many `chat`
calls — lands in a single trace. These patches just make the inner structure
(which tool ran, with what args, returning what) visible to the judge.

Import this module before constructing an Agent:

    import evals.instrument  # noqa: F401  (side-effecting patch)
"""

from __future__ import annotations

import functools

from deepeval.tracing import observe, update_current_span

from botcircuits.agent.core import Agent
from botcircuits.agent.tools.registry import ToolRegistry

_PATCHED = False


def patch() -> None:
    """Idempotently patch Agent.chat and ToolRegistry.run for tracing."""
    global _PATCHED
    if _PATCHED:
        return

    _orig_run = ToolRegistry.run

    @functools.wraps(_orig_run)
    async def traced_run(self, name, args, context=None):
        # One TOOL-type span per tool invocation. Naming the span after the
        # tool is what lets a Tool Correctness metric (and a human reading the
        # trace) see which tools fired and in what order.
        @observe(type="tool", name=name)
        async def _inner():
            output, is_error = await _orig_run(self, name, args, context)
            update_current_span(
                input={"name": name, "args": args},
                output={"output": output, "is_error": is_error},
            )
            return output, is_error

        return await _inner()

    ToolRegistry.run = traced_run

    _orig_chat = Agent.chat

    @functools.wraps(_orig_chat)
    async def traced_chat(self, user_input, session_id=None, system=None):
        @observe(name="agent.chat")
        async def _inner():
            text, sid = await _orig_chat(self, user_input, session_id, system)
            update_current_span(input=user_input, output=text)
            return text, sid

        return await _inner()

    Agent.chat = traced_chat
    _PATCHED = True


patch()
