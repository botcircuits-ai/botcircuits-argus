"""Engine-driven runner + build-time segment computation.

These cover the inversion-of-control core directly (no provider): the
engine owns the loop, batches non-branching steps into segments, evaluates
branches deterministically against captured slots, records per-branch audit
structs, and routes a REQUIRED-but-unfillable branch variable to a
clarification pause instead of a silent default-branch fallthrough.
"""

from __future__ import annotations

import asyncio

from botcircuits.agent.workflow.engine.runner import (
    SegmentResult,
    run_workflow_engine,
)
from botcircuits.agent.workflow.engine.segments import compute_segments


def _built(flow: dict) -> dict:
    """Attach build-time segments, mirroring what the workflow builder
    emits into `.build/`. The runner reads `flow['segments']`; without it
    it falls back to one-step-per-segment (covered separately)."""
    flow["segments"] = compute_segments(flow)
    return flow


def _linear_flow() -> dict:
    """start → a → b → c (no branches): one segment of three actions."""
    return {
        "start": "start",
        "variables": [],
        "steps": {
            "start": {"type": "start", "next": "a"},
            "a": {"type": "agentAction", "settings": {"action": "do a"}, "next": "b"},
            "b": {"type": "agentAction", "settings": {"action": "do b"}, "next": "c"},
            "c": {"type": "agentAction", "settings": {"action": "do c"}},
        },
    }


def _branch_flow(required: bool = False) -> dict:
    """start → s1 (branch on color) → red | blue."""
    var = {"variableName": "color", "dataType": "string", "description": "the color"}
    if required:
        var["required"] = True
    return {
        "start": "start",
        "variables": [var],
        "steps": {
            "start": {"type": "start", "next": "s1"},
            "s1": {
                "type": "agentAction",
                "settings": {"action": "pick a color"},
                "next": "blue",
                "choices": [{
                    "operator": "OR",
                    "expressionList": [
                        {"variable": "color", "operator": "is", "value": "red"}
                    ],
                    "next": "red",
                }],
            },
            "red": {"type": "agentAction", "settings": {"action": "go red"}},
            "blue": {"type": "agentAction", "settings": {"action": "go blue"}},
        },
    }


# -- segment computation ----------------------------------------------------


def test_compute_segments_batches_linear_run():
    segs = compute_segments(_linear_flow())
    assert len(segs) == 1
    assert segs[0]["steps"] == ["a", "b", "c"]
    assert segs[0]["branchStep"] is None


def test_compute_segments_splits_on_branch():
    segs = compute_segments(_branch_flow())
    by_id = {s["id"]: s for s in segs}
    # s1 segment ends on the branch; red and blue are their own segments.
    assert by_id["start"]["branchStep"] == "s1"
    assert "red" in by_id and "blue" in by_id


def test_compute_segments_splits_on_agent_change():
    """Two consecutive non-branching steps pinned to different agents must
    NOT be merged into one segment — a segment is one LLM call, and that
    call can only go to one agent/model."""
    flow = {
        "start": "start",
        "variables": [],
        "steps": {
            "start": {"type": "start", "next": "a"},
            "a": {"type": "agentAction", "agent": "researcher",
                  "settings": {"action": "do a"}, "next": "b"},
            "b": {"type": "agentAction", "agent": "researcher",
                  "settings": {"action": "do b"}, "next": "c"},
            "c": {"type": "agentAction", "settings": {"action": "do c"}},
        },
    }
    segs = compute_segments(flow)
    by_id = {s["id"]: s for s in segs}
    # a+b share the "researcher" agent and batch together (segment id is
    # "start", the walk's head — "start" is transparent); c has no agent
    # (the run default) and gets re-queued as its own segment.
    assert by_id["start"]["steps"] == ["a", "b"]
    assert by_id["start"]["agent"] == "researcher"
    assert by_id["c"]["steps"] == ["c"]
    assert by_id["c"]["agent"] is None


def test_compute_segments_default_agent_is_none():
    segs = compute_segments(_linear_flow())
    assert segs[0]["agent"] is None


def test_question_step_is_isolated_into_its_own_segment():
    """A `question` must never be bundled with a preceding action step.
    Bundling breaks pause/resume — the resumed segment replays the earlier
    action and re-asks, so the reply is never consumed and a branching retry
    question never evaluates its choices (the stuck-retry-loop bug)."""
    flow = {
        "start": "start",
        "variables": [],
        "steps": {
            "start": {"type": "start", "next": "inform"},
            # An info action that statically flows into a branching question.
            "inform": {
                "type": "agentAction",
                "settings": {"action": "tell the user something"},
                "next": "ask_retry",
            },
            "ask_retry": {
                "type": "question",
                "settings": {"action": "check another? (yes/no)"},
                "next": "end",
                "choices": [{
                    "operator": "OR",
                    "expressionList": [
                        {"variable": "again", "operator": "is", "value": "yes"}
                    ],
                    "next": "inform",
                }],
            },
            "end": {"type": "agentAction", "settings": {"action": "bye"}},
        },
    }
    by_id = {s["id"]: s for s in compute_segments(flow)}
    # `inform` and `ask_retry` are SEPARATE segments — not bundled.
    assert by_id["inform"]["steps"] == ["inform"]
    assert by_id["inform"]["branchStep"] is None
    assert by_id["ask_retry"]["steps"] == ["ask_retry"]
    assert by_id["ask_retry"]["branchStep"] == "ask_retry"


# -- engine loop ------------------------------------------------------------


def _collect_runner():
    seen: list[list[str]] = []

    async def run(*, actions, branch_variables, system_notes, slots):
        seen.append(list(actions))
        return SegmentResult(text="ok", captured_slots={})

    return run, seen


def test_linear_runs_as_single_segment_call():
    run, seen = _collect_runner()
    res = asyncio.run(run_workflow_engine(
        _built(_linear_flow()), workflow_name="lin", run_segment=run))
    assert res.done and not res.paused
    # One LLM call for the whole 3-step linear run — cost scales with
    # branches (zero here → one segment), not steps.
    assert len(seen) == 1
    assert seen[0] == ["do a", "do b", "do c"]


def test_branch_takes_matching_path_from_captured_slot():
    order: list[list[str]] = []

    async def run(*, actions, branch_variables, system_notes, slots):
        order.append(list(actions))
        cap = {"color": "red"} if any(
            v["variableName"] == "color" for v in branch_variables) else {}
        return SegmentResult(text="ok", captured_slots=cap)

    res = asyncio.run(run_workflow_engine(
        _built(_branch_flow()), workflow_name="br", run_segment=run))
    assert res.done
    flat = [a for tup in order for a in tup]
    assert "go red" in flat and "go blue" not in flat
    # A decision record was persisted for the branch.
    assert any(d.get("variable") == "color" for d in res.decisions)


def test_branch_defaults_when_optional_var_empty():
    async def run(*, actions, branch_variables, system_notes, slots):
        return SegmentResult(text="ok", captured_slots={})  # never reports color

    res = asyncio.run(run_workflow_engine(
        _built(_branch_flow(required=False)), workflow_name="br", run_segment=run))
    # Optional empty → default branch (blue), NOT a clarification pause.
    assert res.done and not res.paused


def test_required_unfilled_var_routes_to_clarification():
    async def run(*, actions, branch_variables, system_notes, slots):
        return SegmentResult(text="ok", captured_slots={})  # never reports color

    res = asyncio.run(run_workflow_engine(
        _built(_branch_flow(required=True)), workflow_name="br", run_segment=run))
    assert res.paused and not res.done
    assert res.paused_step == "start"
    assert "color" in res.question.lower() or "information" in res.question.lower()


def test_user_pause_yields_with_resume_cursor():
    async def run(*, actions, branch_variables, system_notes, slots):
        return SegmentResult(paused=True, question="What color?")

    res = asyncio.run(run_workflow_engine(
        _built(_branch_flow()), workflow_name="br", run_segment=run))
    assert res.paused
    assert res.question == "What color?"
    assert res.paused_step == "start"


def test_resolve_unfilled_backfills_before_branch():
    async def run(*, actions, branch_variables, system_notes, slots):
        return SegmentResult(text="ok", captured_slots={})

    async def resolver(*, flow, step_id, variables, slots):
        return {"color": "red"}  # Tier-0/2 supplies what record_slots didn't

    res = asyncio.run(run_workflow_engine(
        _built(_branch_flow(required=True)), workflow_name="br",
        run_segment=run, resolve_unfilled=resolver))
    # Backfilled → branch resolves, no clarification.
    assert res.done and not res.paused
    assert res.slots.get("color") == "red"


def _retry_loop_flow() -> dict:
    """start → q1 (question) → q2 (branching question; "again" loops back to q1).
    Models the order-status retry loop: two question steps, one looping back."""
    return {
        "start": "start",
        "variables": [
            {"variableName": "again", "dataType": "string", "description": "loop?"},
        ],
        "steps": {
            "start": {"type": "start", "next": "q1"},
            "q1": {"type": "question", "settings": {"action": "Ask: value?"},
                   "next": "q2"},
            "q2": {
                "type": "question",
                "settings": {"action": "Ask: again? (yes/no)"},
                "next": "end",
                "choices": [{
                    "operator": "OR",
                    "expressionList": [
                        {"variable": "again", "operator": "is", "value": "yes"}
                    ],
                    "next": "q1",
                }],
            },
            "end": {"type": "agentAction", "settings": {"action": "done"}},
        },
    }


def test_stale_reply_cleared_so_loopback_question_pauses():
    """The resume reply (`__last_user_message__`) must be consumed ONCE. When a
    branching question loops back to an earlier question in the same in-process
    walk, the earlier question must PAUSE for fresh input — not re-consume the
    stale reply and spin the loop forever (the stuck-retry-loop bug)."""

    async def run(*, actions, branch_variables, system_notes, slots):
        reply = slots.get("__last_user_message__")
        bvars = [v["variableName"] for v in branch_variables]
        # A question with no fresh reply pauses; with a reply it consumes it.
        if reply is None:
            return SegmentResult(paused=True, question=" ".join(actions))
        if bvars:  # q2: map reply to the branch var
            return SegmentResult(captured_slots={bvars[0]: reply})
        return SegmentResult(captured_slots={})  # q1: just consumes the reply

    flow = _built(_retry_loop_flow())

    # First leg: pauses at q1.
    r = asyncio.run(run_workflow_engine(flow, workflow_name="loop", run_segment=run))
    assert r.paused and r.paused_step == "start"

    # Reply to q1 → engine advances to q2 and pauses there. Crucially it does
    # NOT carry "v1" forward into q2.
    s = {**r.slots, "__last_user_message__": "v1"}
    r = asyncio.run(run_workflow_engine(
        flow, workflow_name="loop", run_segment=run, start_step_id=r.paused_step,
        slots=s))
    assert r.paused and r.paused_step == "q2"

    # Reply "yes" to q2 → loops back to q1, which MUST pause again (the stale
    # "yes" was cleared, so q1 sees no reply). Before the fix this looped
    # forever, re-deciding "yes" without ever pausing.
    s = {**r.slots, "__last_user_message__": "yes"}
    r = asyncio.run(run_workflow_engine(
        flow, workflow_name="loop", run_segment=run, start_step_id=r.paused_step,
        slots=s))
    assert r.paused and r.paused_step == "q1"
    assert "__last_user_message__" not in r.slots


# -- data variables (carried key-value memory) ------------------------------


def _scrape_save_flow() -> dict:
    """start → scrape (branch on count>0) → save | none.

    `scraped_jobs` is a DATA variable (non-branch): the scrape segment
    produces it, the save segment must receive it back via slots.
    """
    return {
        "start": "start",
        "variables": [
            {"variableName": "count", "dataType": "number", "description": "n"},
            {"variableName": "scraped_jobs", "dataType": "string",
             "description": "carried payload"},
        ],
        "steps": {
            "start": {"type": "start", "next": "scrape"},
            "scrape": {
                "type": "agentAction",
                "settings": {"action": "scrape jobs"},
                "next": "none",
                "choices": [{
                    "operator": "AND",
                    "expressionList": [
                        {"variable": "count", "operator": "greater than", "value": 0}
                    ],
                    "next": "save",
                }],
            },
            "save": {"type": "agentAction", "settings": {"action": "save jobs"}},
            "none": {"type": "agentAction", "settings": {"action": "print none"}},
        },
    }


def test_data_variable_carries_from_scrape_to_save():
    seen_data_vars: list[list[str]] = []
    save_slots: dict = {}

    async def run(*, actions, branch_variables, system_notes, slots,
                  data_variables=None, **_):
        # Every segment is offered the data variable in scope.
        seen_data_vars.append(
            [v["variableName"] for v in (data_variables or [])]
        )
        if "scrape jobs" in actions:
            # Scrape produces BOTH the branch var and the data payload.
            return SegmentResult(
                text="scraped",
                captured_slots={"count": 2, "scraped_jobs": '[{"t":"SWE"}]'},
            )
        if "save jobs" in actions:
            # Save must SEE the carried payload in its slots.
            save_slots.update(slots)
        return SegmentResult(text="ok", captured_slots={})

    res = asyncio.run(run_workflow_engine(
        _built(_scrape_save_flow()), workflow_name="js", run_segment=run))

    assert res.done and not res.paused
    # Took the save branch (count>0), and save received the carried payload.
    assert save_slots.get("scraped_jobs") == '[{"t":"SWE"}]'
    assert save_slots.get("count") == 2
    # The data variable was advertised to segments (not the branch-only set).
    assert any("scraped_jobs" in dv for dv in seen_data_vars)


def test_data_variable_absent_means_no_data_kwarg():
    # A flow with ONLY branch variables passes no data_variables — simple
    # runners that don't accept the kwarg still work (back-compat).
    async def run(*, actions, branch_variables, system_notes, slots):
        return SegmentResult(text="ok", captured_slots={"color": "red"})

    res = asyncio.run(run_workflow_engine(
        _built(_branch_flow()), workflow_name="br", run_segment=run))
    assert res.done
