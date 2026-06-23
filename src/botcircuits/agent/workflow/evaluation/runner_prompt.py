"""Prompt-only baseline runner.

This is the "what if we DIDN'T have the workflow module" baseline. We
render the workflow's flow into a numbered, natural-language plan in the
system prompt and ask the LLM to drive itself — pick a branch, decide
which step is next, emit the trace.

The LLM is forced to reply in a single JSON object so we can parse the
trace and compare it to the expected trace. This is the same comparison
the workflow runner gets — the structural difference is that the
workflow engine evaluates branches deterministically from slots, while
here the LLM has to do the bookkeeping itself in prose.

This isolates the hypothesis under test: do executable workflows
out-perform prompted-steps in accuracy and consistency? If the engine
beats the LLM-as-driver on the same case, that delta is the workflow
module's contribution.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from botcircuits.providers.base import LLMProvider
from botcircuits.types import Message
from botcircuits.agent.workflow import local as wf_local
from botcircuits.agent.workflow.evaluation.dataset import EvalCase


@dataclass
class PromptRunResult:
    case_id: str
    workflow: str
    trace: list[str] = field(default_factory=list)
    final_action: str = ""
    raw_reply: str = ""
    parse_error: str | None = None
    error: str | None = None
    elapsed_s: float = 0.0
    # Tokens reported by the provider when available. Both fields stay
    # at 0 when the provider doesn't surface usage.
    input_tokens: int = 0
    output_tokens: int = 0
    # Steps the LLM emitted that are NOT in the flow. Counts as a
    # hallucination indicator — the workflow engine cannot produce
    # these.
    hallucinated_steps: list[str] = field(default_factory=list)


_REPLY_INSTRUCTIONS = """\
You are simulating a workflow runtime. You must drive the flow
described below using the inputs supplied as user messages.

Rules:
  1. The flow has a strict graph. Follow the `next` and
     `conditions` fields. Do NOT invent new steps.
  2. Conditions are evaluated against the variables you have so far.
     The user messages tell you what variables have been collected.
  3. Reply with a SINGLE JSON object and nothing else. No prose, no
     code fences. Schema:

       {
         "trace": ["step_id", "step_id", ...],
         "final_action": "natural-language text of the final step's action"
       }

     `trace` is the ordered list of agentAction steps you visited,
     starting from the entry point and ending at the terminal step.
     `final_action` is the `action` text of the terminal step, taken
     verbatim from the flow definition.
"""


def _render_flow_as_prompt(flow: dict) -> str:
    """Convert a flow definition into a numbered prose plan.

    The output is dense and machine-readable; the goal is to give the
    LLM exactly the same information the engine has, just as text, so
    the comparison measures execution quality rather than information
    asymmetry.
    """
    lines: list[str] = []
    start = flow.get("start")
    lines.append(f"START step: {start}")
    steps = flow.get("steps") or {}
    for sid, s in steps.items():
        s_type = s.get("type")
        nxt = s.get("next")
        sc = s.get("settings") or {}
        lines.append(f"\nstep {sid!r} (type={s_type}):")
        action = sc.get("action")
        if action:
            lines.append(f"  action: {action}")
        if nxt:
            lines.append(f"  default next: {nxt}")
        conds = s.get("conditions") or []
        for c in conds:
            lines.append(
                f"  IF {c.get('expCondition') or c.get('condition')!r} "
                f"-> next: {c.get('next')!r}"
            )
    variables = flow.get("variables") or []
    if variables:
        lines.append("\nvariables in scope:")
        for v in variables:
            lines.append(
                f"  - {v.get('variableName')}: "
                f"{v.get('dataType','string')} — {v.get('description','')}"
            )
    return "\n".join(lines)


def _format_user_turn(case) -> str:
    """Render the scripted user side of the conversation as one message.

    We collapse the multi-turn workflow conversation into a single
    user-side payload so the prompt-only run is a single LLM round — we
    are not testing the LLM's chat memory, we are testing whether
    prompted steps can match an executable STM given the same inputs.

    Both forms of input are surfaced: any pre-extracted `args` (what
    an agent in production would have parsed out before invoking the
    tool) AND the raw natural-language replies (`user_text`). The
    workflow-side runner sees the same two channels — keeping them
    symmetric is what makes the comparison apples-to-apples.
    """
    transcript: list[dict] = []
    if case.initial_user_text:
        transcript.append({"turn": 0, "user_text": case.initial_user_text})
    for i, t in enumerate(case.turns, 1):
        entry: dict = {"turn": i}
        if t.user_text:
            entry["user_text"] = t.user_text
        if t.args:
            entry["args"] = t.args
        transcript.append(entry)

    payload = {
        "initial_args": case.initial_args,
        "conversation": transcript,
    }
    return (
        "Drive the state machine with the following inputs.\n"
        "  - `initial_args` are slot values the agent pre-extracted "
        "before the first workflow call.\n"
        "  - `conversation` is the chronological list of user replies "
        "and any further tool-call arguments at each turn. Extract "
        "any variable values you need from the natural-language "
        "user_text fields just as the engine's normalizer would.\n"
        + json.dumps(payload, indent=2, ensure_ascii=False)
    )


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(reply: str) -> dict | None:
    """Best-effort: pull the first {...} block out of the model reply."""
    if not reply:
        return None
    m = _JSON_BLOCK_RE.search(reply)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


async def run_case_prompt(
    case: EvalCase,
    provider: LLMProvider,
    *,
    max_tokens: int = 1024,
) -> PromptRunResult:
    """Run one EvalCase through the prompt-only baseline."""
    started = time.perf_counter()
    out = PromptRunResult(case_id=case.id, workflow=case.workflow)

    try:
        record = wf_local._load_workflow_record(case.workflow)
        flow = record.get("flow") or {}
        valid_step_ids = set((flow.get("steps") or {}).keys())

        system = _REPLY_INSTRUCTIONS + "\n\nFlow:\n" + _render_flow_as_prompt(flow)
        user_text = _format_user_turn(case)

        messages = [Message(role="user", blocks=[{"type": "text", "text": user_text}])]
        resp = await provider.complete(
            system=system,
            messages=messages,
            tools=[],
            hosted_mcp=[],
            skills=[],
            max_tokens=max_tokens,
        )
        out.raw_reply = resp.text or ""
        # Token usage when the provider attaches it (LLMResponse doesn't
        # have a guaranteed schema for usage; we look it up defensively).
        usage = getattr(resp, "usage", None)
        if isinstance(usage, dict):
            out.input_tokens = int(usage.get("input_tokens") or 0)
            out.output_tokens = int(usage.get("output_tokens") or 0)

        parsed = _extract_json(out.raw_reply)
        if not parsed:
            out.parse_error = "no JSON object in reply"
        else:
            trace = parsed.get("trace") or []
            if isinstance(trace, list) and all(isinstance(s, str) for s in trace):
                out.trace = trace
                out.hallucinated_steps = [
                    s for s in trace if s not in valid_step_ids
                ]
            else:
                out.parse_error = "trace must be a list of strings"
            fa = parsed.get("final_action")
            out.final_action = fa if isinstance(fa, str) else ""
    except Exception as e:
        out.error = f"{type(e).__name__}: {e}"

    out.elapsed_s = time.perf_counter() - started
    return out
