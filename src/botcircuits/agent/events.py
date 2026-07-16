"""Event mapping — translating loop internals into UI-facing signals.

Two seams live here:

- `segment_stream_events` maps an engine-segment sink event to the same
  `StreamEvent` shapes the main loop yields, so a workflow's internal
  segment calls look live to any UI consuming `chat_stream`.
- `human_feedback_pause` detects that the model asked the user a question
  via the `human_feedback` tool this round, so the loop can pause and
  surface it instead of spinning another provider call.
"""

from __future__ import annotations

import json
from typing import Iterator

from botcircuits.types import StreamEvent, ToolCall
from botcircuits.agent.tools.builtins.human_feedback import HUMAN_FEEDBACK_TOOL


def segment_stream_events(kind: str, payload, sid: str) -> Iterator[StreamEvent]:
    """Map an engine-segment sink event to StreamEvents for the UI.

    The segment sink (passed to `run_segment`) emits `("text", str)`,
    `("tool_call", ToolCall)`, and `("tool_result", (ToolCall, out, err))`;
    we translate those to the same StreamEvent shapes the main loop yields.
    """
    if kind == "text":
        yield StreamEvent(type="text_delta", text=payload, session_id=sid)
    elif kind == "tool_call":
        yield StreamEvent(type="tool_call", tool_call=payload, session_id=sid)
    elif kind == "tool_result":
        tc, out, err = payload
        yield StreamEvent(type="tool_result", tool_call_id=tc.id,
                          text=out, is_error=err, session_id=sid)


def human_feedback_pause(
    tool_calls: list[ToolCall],
    results: list[tuple[str, bool]],
) -> str | None:
    """If a `human_feedback` call ran this round, return the question to
    surface to the user (so the loop can pause); else None.

    `human_feedback`'s handler returns `{"paused": true, "question": ...}`,
    JSON-encoded into the result text. We match by tool name and pull the
    question back out of that payload, falling back to the model's own
    `question` argument, then the raw result text.
    """
    for tc, (output, _is_error) in zip(tool_calls, results):
        if tc.name != HUMAN_FEEDBACK_TOOL:
            continue
        question = ""
        try:
            payload = json.loads(output)
            if isinstance(payload, dict):
                question = payload.get("question") or ""
        except (ValueError, TypeError):
            question = ""
        if not question and isinstance(tc.arguments, dict):
            question = tc.arguments.get("question") or ""
        return question or output
    return None
