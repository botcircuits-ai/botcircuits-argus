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
        "agent": "researcher" | None,          # the named agent (see
                                               # `agents.<name>` at the
                                               # workflow root) every step in
                                               # this segment is pinned to;
                                               # None = the run's default
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


def _step_agent(step: dict) -> str | None:
    """The named agent a step is pinned to (`agents.<name>` in the workflow
    doc), or None for the run's default agent/model."""
    agent = step.get("agent")
    return agent if isinstance(agent, str) and agent else None


def _walk_chain(
    steps: dict[str, dict],
    head: str,
    *,
    forbid_branch_and_pause: bool = False,
    chain_label: str = "",
) -> tuple[list[dict[str, Any]], set[str]]:
    """Walk one linear region of the graph starting at `head`, splitting it
    into branch-delimited segments exactly like the main `compute_segments`
    walk does. Returns `(segments, step_ids_consumed)`.

    Shared by the top-level graph walk and by each `parallel` branch chain
    (§ parallel nodes) so the two don't duplicate the segment-splitting
    rules. `forbid_branch_and_pause`, when set (branch-chain mode), rejects
    `choices`/`conditions`, `question`, and `parallel` steps with a
    `ValueError` naming `chain_label` — branches must run to completion
    without pausing or branching internally (see module docstring addendum).
    """
    segments: list[dict[str, Any]] = []
    seen_segment_heads: set[str] = set()
    consumed: set[str] = set()
    queue: list[str] = [head]

    while queue:
        seg_head = queue.pop(0)
        if seg_head in seen_segment_heads:
            continue
        seen_segment_heads.add(seg_head)

        ordered: list[str] = []
        branch_step: str | None = None
        segment_agent: str | None = None
        cursor: str | None = seg_head
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
            consumed.add(cursor)

            if forbid_branch_and_pause and step.get("type") == "parallel":
                raise ValueError(
                    f"branch {chain_label!r}: nested 'parallel' step "
                    f"{cursor!r} is not supported"
                )

            # A `question` always BEGINS its own segment. If we reached one
            # while a segment is already accumulating (it's not this segment's
            # head), stop before it and re-queue it as a fresh head. Bundling a
            # preceding action with a question breaks pause/resume: the resumed
            # segment replays the earlier action and re-asks, so the user's
            # reply is never consumed and a branching question (e.g. a retry
            # loop) never evaluates its choices. Isolated, the question's
            # segment re-runs only itself on resume and captures the answer.
            if step.get("type") == "question" and ordered:
                if forbid_branch_and_pause:
                    raise ValueError(
                        f"branch {chain_label!r}: 'question' step {cursor!r} "
                        f"is not allowed inside a parallel branch"
                    )
                queue.append(cursor)
                break

            if _pausing(step):
                # A step pinned to a different agent than the ones already
                # accumulated can't join this segment — a segment is one LLM
                # call, and that call can only go to one agent/model. Stop
                # before it and re-queue it as a fresh head, same as the
                # mid-walk `question` case above. Only pausing steps make an
                # LLM call, so transparent `start`/`systemAction` steps never
                # trigger this check regardless of any stray `agent` field.
                if ordered and _step_agent(step) != segment_agent:
                    queue.append(cursor)
                    break
                if not ordered:
                    segment_agent = _step_agent(step)
                ordered.append(cursor)

            if _is_branch_step(step):
                if forbid_branch_and_pause:
                    raise ValueError(
                        f"branch {chain_label!r}: step {cursor!r} carries "
                        f"'choices'/'conditions', which is not allowed "
                        f"inside a parallel branch"
                    )
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
                if forbid_branch_and_pause:
                    raise ValueError(
                        f"branch {chain_label!r}: 'question' step {cursor!r} "
                        f"is not allowed inside a parallel branch"
                    )
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
                "id": seg_head,
                "steps": ordered,
                "branchStep": branch_step,
                "agent": segment_agent,
            })

    return segments, consumed


def _compile_parallel_branches(
    steps: dict[str, dict],
    parallel_step_id: str,
    parallel_step: dict,
) -> tuple[dict[str, list[dict[str, Any]]], set[str]]:
    """Compile each `branches.<name>` chain of a `parallel` step into its own
    segment list via `_walk_chain` in branch-chain mode (no internal
    branching/pausing allowed). Returns `(compiled_branches, all_consumed_ids)`.
    """
    branches = parallel_step.get("branches")
    if not isinstance(branches, dict) or not branches:
        raise ValueError(
            f"'parallel' step {parallel_step_id!r} must declare a non-empty "
            f"'branches' mapping of name -> [step ids]"
        )

    compiled: dict[str, list[dict[str, Any]]] = {}
    all_consumed: set[str] = set()
    for name, chain in branches.items():
        if not isinstance(chain, list) or not chain or not isinstance(chain[0], str):
            raise ValueError(
                f"'parallel' step {parallel_step_id!r}: branch {name!r} must "
                f"be a non-empty list of step ids"
            )
        chain_head = chain[0]
        if chain_head not in steps:
            raise ValueError(
                f"'parallel' step {parallel_step_id!r}: branch {name!r} head "
                f"{chain_head!r} is not a known step"
            )
        branch_segments, consumed = _walk_chain(
            steps, chain_head,
            forbid_branch_and_pause=True,
            chain_label=f"{parallel_step_id}.{name}",
        )
        compiled[name] = branch_segments
        all_consumed |= consumed
    return compiled, all_consumed


def compute_segments(flow: dict) -> list[dict[str, Any]]:
    """Walk `flow` from its start and partition reachable steps into
    branch-delimited segments.

    Pure: never mutates `flow`. Returns the ordered list of segments,
    each a dict of `{id, steps, branchStep, agent}` (see module docstring).

    Walking rules:
      - Begin a new segment at the start step and at every branch target.
      - Accumulate consecutive non-branching pausing steps into the
        current segment, following each step's static `next`.
      - A `question` step always ends a segment: it pauses for the user,
        so the engine yields there regardless of branching.
      - A step pinned to a different `agent` than the segment's own also
        ends the segment — each segment is exactly one LLM call, so it
        can't span two different agents/models. Re-queued as a fresh
        segment head, same as a mid-walk `question`.
      - A branch step ends the current segment (recorded as `branchStep`);
        each of its choice targets seeds a fresh segment.
      - `start`/`systemAction` steps are transparent for batching: the
        walk passes through them to the next pausing step. (They still
        execute in the engine; they just don't get their own LLM call.)
      - A `parallel` step always gets its OWN isolated segment (never
        merged with a preceding or following run — same treatment as a
        `question`). Its segment carries a `parallel` sub-record: each of
        the step's `branches.<name>` chains, pre-compiled into its own
        inner segment list via `_walk_chain` (branch-chain mode: no
        `choices`/`conditions`, no `question`, no nested `parallel` — a
        branch must run to completion without pausing or branching, so the
        engine can run every branch concurrently and treat a pause as a
        hard error). The parallel step's own `next` seeds the segment that
        runs once every branch has completed.
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

        head_step = steps.get(head)
        if head_step is not None and head_step.get("type") == "parallel":
            # Isolated segment: just the parallel step itself, plus its
            # compiled branch chains. Its `next` seeds the post-join segment.
            compiled_branches, _consumed = _compile_parallel_branches(
                steps, head, head_step,
            )
            segments.append({
                "id": head,
                "steps": [head],
                "branchStep": None,
                "agent": None,
                "parallel": {
                    "branches": compiled_branches,
                    "next": head_step.get("next"),
                    "onError": head_step.get("onError"),
                },
            })
            nxt = head_step.get("next")
            if isinstance(nxt, str) and nxt:
                queue.append(nxt)
            continue

        ordered: list[str] = []
        branch_step: str | None = None
        segment_agent: str | None = None
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

            # A `parallel` step (see above) always begins its own segment too
            # — stop before it (transparent steps like `start` may have
            # walked straight here with nothing yet accumulated) and re-queue
            # it as a fresh head, so the outer loop's head-is-parallel branch
            # compiles it rather than this walk treating it as a pass-through.
            if step.get("type") == "parallel":
                queue.append(cursor)
                break

            if _pausing(step):
                # A step pinned to a different agent than the ones already
                # accumulated can't join this segment — a segment is one LLM
                # call, and that call can only go to one agent/model. Stop
                # before it and re-queue it as a fresh head, same as the
                # mid-walk `question` case above. Only pausing steps make an
                # LLM call, so transparent `start`/`systemAction` steps never
                # trigger this check regardless of any stray `agent` field.
                if ordered and _step_agent(step) != segment_agent:
                    queue.append(cursor)
                    break
                if not ordered:
                    segment_agent = _step_agent(step)
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
                "agent": segment_agent,
            })

    return segments
