"""Workflow-side runner — drives the real STM engine end-to-end.

For each `EvalCase`:
  1. Call `run_workflow(case.workflow, case.initial_args)` with the
     case's `initial_user_text` as `last_user_message`. Record the
     state the engine paused on.
  2. For each scripted turn, call again with that turn's `args` and
     `user_text`, plus the same session_id. Record the new pause.
  3. Stop when the engine reports `done=True`.

Layer B (LLM-driven variable normalization) is wired through when a
provider is passed in. That's how natural-phrasing test cases reach
the engine's slots: the user's plain-English reply lands in
`last_user_message`, Layer B extracts named values from it on
re-entry into a branching state, and the workflow can then route.

Without a provider, normalization falls back to Layer A (type
coercion) only — same as production when the agent has no LLM
configured for the workflow tool. That mode is the cheapest smoke
test, but it won't fill slots from raw user text and will mostly fail
to branch on natural-phrasing cases by design.

The trace contains, in order, every state the executor paused on
across turns. With a well-formed STM this equals the sequence of
`agentAction` states the user/LLM would have seen.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from botcircuits.providers.base import LLMProvider
from botcircuits.agent.workflow import local as wf_local
from botcircuits.agent.workflow.evaluation.dataset import EvalCase


@dataclass
class WorkflowRunResult:
    case_id: str
    workflow: str
    trace: list[str] = field(default_factory=list)
    final_action: str = ""
    done: bool = False
    error: str | None = None
    elapsed_s: float = 0.0
    # Tool calls into the workflow engine. Useful sanity check that the
    # case's turn count matches what the engine actually emitted.
    invocations: int = 0


async def run_case_workflow(
    case: EvalCase,
    *,
    provider: LLMProvider | None = None,
) -> WorkflowRunResult:
    """Drive one EvalCase through the workflow engine.

    `provider` enables Layer B normalization on workflow re-entry; pass
    None to fall back to Layer A only.

    User text is accumulated across turns into a rolling transcript
    that's passed as `last_user_message` on every re-entry. This
    simulates the way an agent in production has access to several
    recent user replies when extracting tool-call arguments — Layer B
    is the workflow's own backstop, and a one-message context window
    starves it of values that the user said two turns ago for a
    question whose branch evaluation only fires now (the engine
    pauses on every agentAction, including the question-only ones
    between two branching states). Accumulating keeps the test honest
    to how the same workflow would behave in real chat.
    """
    started = time.perf_counter()
    out = WorkflowRunResult(case_id=case.id, workflow=case.workflow)
    normalize_enabled = provider is not None

    transcript: list[str] = []
    if case.initial_user_text:
        transcript.append(case.initial_user_text)
    # The engine treats `last_assistant_message` as the action it just
    # surfaced; tracking the most recent one keeps that source of
    # context warm for Layer B's hallucination guard across turns.
    last_assistant = ""

    try:
        result = await wf_local.run_workflow(
            case.workflow,
            case.initial_args,
            session_id=None,
            provider=provider,
            normalize_enabled=normalize_enabled,
            last_user_message=_join_transcript(transcript),
            last_assistant_message=last_assistant,
        )
        out.invocations += 1
        sid = result.get("session_id")
        _record_step(out, result)
        last_assistant = out.final_action

        # Detect engine cycles: if the trace keeps growing without ever
        # reporting done=True, a buggy workflow can loop forever.
        # Cutoff after `2 * len(states)` re-visits of the last state
        # (a small constant times the workflow size). The trace still
        # captures the cycle so the report can show what happened.
        repeats_at_tail = 0

        for turn in case.turns:
            if out.done:
                break
            if turn.user_text:
                transcript.append(turn.user_text)
            result = await wf_local.run_workflow(
                case.workflow,
                turn.args,
                session_id=sid,
                provider=provider,
                normalize_enabled=normalize_enabled,
                last_user_message=_join_transcript(transcript),
                last_assistant_message=last_assistant,
            )
            out.invocations += 1
            sid = result.get("session_id") or sid
            prev_tail = out.trace[-1] if out.trace else None
            _record_step(out, result)
            last_assistant = out.final_action

            # Cycle detector — bail if the same state has been the tail
            # for too long. Without this a workflow with a static `next`
            # that loops back will run until the case's turn budget is
            # exhausted, which makes traces unreadable.
            if out.trace and out.trace[-1] == prev_tail:
                repeats_at_tail += 1
            else:
                repeats_at_tail = 0
            if repeats_at_tail >= 6:
                out.error = (
                    f"engine cycled on state {out.trace[-1]!r} — aborted "
                    f"after {repeats_at_tail} repeat visits"
                )
                break
    except Exception as e:
        out.error = f"{type(e).__name__}: {e}"

    out.elapsed_s = time.perf_counter() - started
    return out


# How many trailing user replies the runner stitches together into
# `last_user_message`. Big enough to cover several "non-branching
# question" states between two branch evaluations, small enough to
# keep Layer B's prompt cheap.
_TRANSCRIPT_WINDOW = 8


def _join_transcript(transcript: list[str]) -> str:
    """Render the trailing user messages as one block. The most recent
    reply is last so Layer B's hallucination guard still anchors on
    the "user's direct reply" framing."""
    tail = transcript[-_TRANSCRIPT_WINDOW:]
    return "\n".join(tail)


def _record_step(out: WorkflowRunResult, result: dict) -> None:
    """Append the paused step to the trace and update final_action / done.

    `run_workflow` exposes `running_step` — the step id the executor
    last entered, including the terminal step on the call that ends
    the workflow. That's the right thing to put in the trace: it's the
    step whose `action` was just surfaced.
    """
    rs = result.get("running_step")
    if isinstance(rs, str) and rs:
        out.trace.append(rs)
    action = result.get("action")
    if isinstance(action, str) and action:
        out.final_action = action
    out.done = bool(result.get("done"))
