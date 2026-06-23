"""Build-time segment computation.

A *segment* is a maximal run of consecutive steps the engine can hand to
the LLM in a single call — there is no branch decision between them, so
the engine never needs to inspect slot values to know what runs next.
Branch points (a step carrying `choices`/`conditions`) terminate a
segment, because the engine must see the captured slots before it can
pick the next step.

The result is the §3.1 token win: the engine's LLM-call count scales with
the number of branch points (segments), not the number of steps.

`compute_segments` is a pure function over the compiled `flow` graph. The
workflow build command calls it AFTER `generate_expressions_and_variables`
(so `choices` are present) and stores the result on `flow["segments"]` in
the `.build/` artifact. The runner reads `flow["segments"]`; when the
field is absent it falls back to one-step-per-segment, so un-rebuilt
workflows keep working.

Shape of each emitted segment::

    {
        "id": "<first step id of the run>",
        "steps": ["step_a", "step_b", ...],   # in execution order
        "branchStep": "step_b" | None,         # the terminating branch
                                               # step, if the run ends on
                                               # one (else None)
    }

`steps` always holds the ordered step ids walked in this segment. The
LAST entry is the branch step when `branchStep` is set; otherwise the run
ended because the next step is itself the head of another segment (a
branch target) or the workflow ended.
"""

from __future__ import annotations

from typing import Any


def _is_branch_step(step: dict) -> bool:
    """A step branches when it carries runtime `choices` (or authored
    `conditions`). Either means the engine must evaluate a decision after
    the step before it knows what runs next — a segment boundary."""
    return bool(step.get("choices")) or bool(step.get("conditions"))


def _pausing(step: dict) -> bool:
    """Steps that put a payload in front of the LLM. `systemAction` is
    non-pausing bookkeeping the engine walks without a round-trip, so it
    never forces a segment boundary on its own. `listDecision` (S3) makes one
    LLM call to gather the per-item fact list, so it pauses like an
    agentAction."""
    return step.get("type") in ("agentAction", "question", "listDecision")


def compute_segments(flow: dict) -> list[dict[str, Any]]:
    """Walk `flow` from its start and partition reachable steps into
    branch-delimited segments.

    Pure: never mutates `flow`. Returns the ordered list of segments,
    each a dict of `{id, steps, branchStep}` (see module docstring).

    Walking rules:
      - Begin a new segment at the start step and at every branch target.
      - Accumulate consecutive non-branching pausing steps into the
        current segment, following each step's static `next`.
      - A `question` step always ends a segment: it pauses for the user,
        so the engine yields there regardless of branching.
      - A branch step ends the current segment (recorded as `branchStep`);
        each of its choice targets seeds a fresh segment.
      - `start`/`systemAction` steps are transparent for batching: the
        walk passes through them to the next pausing step. (They still
        execute in the engine; they just don't get their own LLM call.)
    """
    steps: dict[str, dict] = flow.get("steps") or {}
    start = flow.get("start")
    if not isinstance(start, str) or start not in steps:
        return []

    segments: list[dict[str, Any]] = []
    seen_segment_heads: set[str] = set()
    # Queue of step ids that each begin a segment. Seeded with the start
    # step; branch targets are appended as we discover them.
    queue: list[str] = [start]

    while queue:
        head = queue.pop(0)
        if head in seen_segment_heads:
            continue
        seen_segment_heads.add(head)

        ordered: list[str] = []
        branch_step: str | None = None
        cursor: str | None = head
        guard = 0

        while cursor is not None:
            guard += 1
            if guard > len(steps) + 1:
                # Defensive: a `next` cycle through non-branching steps.
                # Stop walking this segment; the engine's own walk guard
                # will surface a clearer error at runtime.
                break
            step = steps.get(cursor)
            if step is None:
                break

            # A `question` always BEGINS its own segment. If we reached one
            # while a segment is already accumulating (it's not this segment's
            # head), stop before it and re-queue it as a fresh head. Bundling a
            # preceding action with a question breaks pause/resume: the resumed
            # segment replays the earlier action and re-asks, so the user's
            # reply is never consumed and a branching question (e.g. a retry
            # loop) never evaluates its choices. Isolated, the question's
            # segment re-runs only itself on resume and captures the answer.
            if step.get("type") == "question" and ordered:
                queue.append(cursor)
                break

            if _pausing(step):
                ordered.append(cursor)

            if _is_branch_step(step):
                branch_step = cursor
                for choice in step.get("choices") or []:
                    nxt = choice.get("next")
                    if isinstance(nxt, str) and nxt:
                        queue.append(nxt)
                default_next = step.get("next")
                if isinstance(default_next, str) and default_next:
                    queue.append(default_next)
                break

            # A question pauses for the user — end the segment here so the
            # engine yields control even though it isn't a branch. Its static
            # `next` still seeds the following segment; without queueing it the
            # graph past the question is unreachable and the run ends early.
            if step.get("type") == "question":
                nxt = step.get("next")
                if isinstance(nxt, str) and nxt:
                    queue.append(nxt)
                break

            nxt = step.get("next")
            if not isinstance(nxt, str) or not nxt:
                break  # workflow ends after this step
            cursor = nxt

        if ordered or branch_step:
            segments.append({
                "id": head,
                "steps": ordered,
                "branchStep": branch_step,
            })

    return segments
