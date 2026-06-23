"""ReAct-style text prompting as an alternative to native function-calling.

The agent's default mode (`"native"`) hands tools to the provider's
structured tool-use API and reads `resp.tool_calls` back. This module
implements the `"react"` mode: tools are described *in the system prompt*
and the model is asked to emit a `Thought / Action / Action Input` text
block, which we parse ourselves. The result is fed back as an
`Observation:` and the loop repeats — the classic ReAct (Yao et al., 2022)
pattern.

Why keep it: ReAct works on any provider regardless of native tool-use
quality (it's pure text), it's auditable (the reasoning trace is visible),
and it's a useful baseline for evals. The trade-off is brittleness —
the model can malform the format — so the parser is deliberately lenient.

The agent loop calls three functions here:

  - `render_react_preamble(tools)`  -> system-prompt block describing the
    tools and the required output format.
  - `parse_react_step(text)`        -> a `ReactStep` saying whether the
    model wants to act (and with what) or is done.
  - `format_observation(output, is_error)` -> the text we feed back as the
    next turn's user message.

Canonical ReAct is one action per turn, so the parser extracts a single
action (the first one) per model response.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from botcircuits.agent.tools import LocalTool
from botcircuits.types import ToolCall


@dataclass
class ReactStep:
    """Parsed outcome of one ReAct model turn.

    Exactly one of `action` / `final` is meaningful:
      - action set  -> the model wants to call a tool; run it, feed back
        an Observation, and continue the loop.
      - final set (action None) -> the model produced a Final Answer (or
        emitted no parseable Action); the loop should stop and return
        `final` as the assistant's reply.
    """
    action: ToolCall | None
    final: str | None


# Markers. Case-insensitive, tolerant of leading whitespace and markdown
# bold (**Action:**), which models sprinkle in unprompted.
_ACTION_RE = re.compile(
    r"^\s*\**\s*Action\s*:\s*\**\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE
)
_ACTION_INPUT_RE = re.compile(
    r"^\s*\**\s*Action\s+Input\s*:\s*\**\s*(.*)$",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_FINAL_RE = re.compile(
    r"^\s*\**\s*Final\s+Answer\s*:\s*\**\s*(.*)$",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def render_react_preamble(tools: list[LocalTool]) -> str:
    """Build the ReAct instruction block appended to the system prompt.

    Lists each tool with its name, description, and JSON input schema, then
    spells out the strict Thought/Action/Action Input/Observation loop and
    the Final Answer terminator. Returns "" when there are no tools (the
    model should then just answer directly).
    """
    if not tools:
        return ""
    lines = ["", "You operate in a reasoning-and-acting (ReAct) loop. You have "
             "access to the following tools:", ""]
    for t in tools:
        schema = json.dumps(t.input_schema or {"type": "object"},
                            ensure_ascii=False)
        desc = (t.description or "").strip().replace("\n", " ")
        lines.append(f"- {t.name}: {desc}")
        lines.append(f"  input schema: {schema}")
    names = ", ".join(t.name for t in tools)
    lines += [
        "",
        "To use a tool, respond in EXACTLY this format (one action at a time):",
        "",
        "Thought: <your reasoning about what to do next>",
        "Action: <one of: " + names + ">",
        "Action Input: <a single-line JSON object of arguments>",
        "",
        "Then STOP and wait — the tool will run and the result is returned "
        "to you as:",
        "",
        "Observation: <tool result>",
        "",
        "Continue this Thought/Action/Action Input/Observation cycle as many "
        "times as needed. Emit only ONE Action per response. When you have "
        "enough information to answer the user, respond instead with:",
        "",
        "Thought: <final reasoning>",
        "Final Answer: <your complete answer to the user>",
        "",
        "Rules:",
        "- Action Input MUST be valid JSON on a single line, e.g. "
        '{"path": "src/main.py"}. Use {} for tools that take no arguments.',
        "- Never write your own Observation: — that text comes from the tool.",
        "- Never combine an Action and a Final Answer in the same response.",
    ]
    return "\n".join(lines)


def parse_react_step(text: str) -> ReactStep:
    """Parse one model response into a `ReactStep`.

    Precedence: a Final Answer wins over an Action only if it appears and no
    Action precedes it; otherwise the first Action is taken (models
    sometimes append a stray "Final Answer:" after deciding to act). When
    neither marker parses cleanly, we treat the whole text as a final answer
    so a malformed turn ends the loop instead of spinning.
    """
    action_m = _ACTION_RE.search(text)
    final_m = _FINAL_RE.search(text)

    # If a Final Answer appears before any Action (or there is no Action),
    # the model is done.
    if final_m and (not action_m or final_m.start() < action_m.start()):
        return ReactStep(action=None, final=final_m.group(1).strip())

    if not action_m:
        # No structured action and no Final Answer marker: the model just
        # talked. Return its prose as the final answer rather than looping.
        return ReactStep(action=None, final=text.strip())

    name = action_m.group(1).strip().strip("`").strip()
    args = _parse_action_input(text, action_m.end())
    # Synthesize a stable-ish id; the agent loop only needs it to pair the
    # call with its result block.
    call_id = f"react-{abs(hash((name, json.dumps(args, sort_keys=True, default=str)))) & 0xffffff:06x}"
    return ReactStep(action=ToolCall(id=call_id, name=name, arguments=args),
                     final=None)


def _parse_action_input(text: str, after: int) -> dict:
    """Extract and JSON-parse the Action Input that follows an Action line.

    Searches from `after` (end of the Action match) so we don't pick up an
    Action Input belonging to an earlier block. Falls back to {} when the
    input is absent or unparseable — a bad-JSON action becomes a no-arg
    call, and the tool's own validation surfaces the problem as an
    Observation the model can correct.
    """
    m = _ACTION_INPUT_RE.search(text, after)
    if not m:
        return {}
    raw = m.group(1).strip()
    if not raw:
        return {}
    # The DOTALL capture can swallow a trailing "Observation:"/"Thought:"
    # the model hallucinated; cut at the first such marker.
    for marker in ("\nObservation:", "\nThought:", "\nAction:",
                   "\nFinal Answer:"):
        idx = raw.find(marker)
        if idx != -1:
            raw = raw[:idx]
    raw = raw.strip().strip("`").strip()
    # Strip a ```json fence if present.
    if raw.startswith("json"):
        raw = raw[4:].strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except (json.JSONDecodeError, ValueError):
        return {}


def format_observation(output: str, is_error: bool) -> str:
    """Render a tool result as the `Observation:` text fed back to the model.

    `is_error` is prefixed so the model can tell a failure from a normal
    result without parsing the payload — mirrors the `is_error` flag native
    mode carries on tool_result blocks.
    """
    prefix = "Observation (error): " if is_error else "Observation: "
    return prefix + output
