"""Inline / self runtime: the host agent performs each segment in-session.

Drives the real engine with `InlineRuntime`, asserting it hands off each
segment as an action (reusing the engine's pause machinery) and advances when
the host's observed values are seeded back.
"""

import asyncio

from botcircuits.runtime.providers.inline import (
    InlineRuntime,
    decode_action,
    encode_action,
)
from botcircuits.agent.workflow.engine.runner import SegmentResult, run_workflow_engine


def _flow():
    return {
        "start": "decide",
        "steps": {
            "decide": {
                "id": "decide", "type": "agentAction",
                "settings": {"action": "Decide the application"},
                "next": "deny",
                "choices": [{
                    "next": "approve", "operator": "AND",
                    "expressionList": [
                        {"variable": "approved", "operator": "is", "value": "true"}
                    ],
                }],
                "conditions": [{"condition": "approved", "next": "approve"}],
            },
            "approve": {"id": "approve", "type": "agentAction",
                        "settings": {"action": "Approve it"}},
            "deny": {"id": "deny", "type": "agentAction",
                     "settings": {"action": "Deny it"}},
        },
        "variables": [
            {"variableName": "approved", "dataType": "boolean",
             "description": "approved?"}
        ],
        "segments": [
            {"id": "decide", "steps": ["decide"], "branchStep": "decide"},
            {"id": "approve", "steps": ["approve"], "branchStep": None},
            {"id": "deny", "steps": ["deny"], "branchStep": None},
        ],
    }


def test_encode_decode_roundtrip():
    payload = {"actions": ["x"], "branch_variables": [], "item_variables": [],
               "system_notes": []}
    q = encode_action(payload)
    assert decode_action(q) == payload
    # A real question is not an inline marker.
    assert decode_action("What is your income?") is None


def test_run_segment_hands_off_first_segment():
    rt = InlineRuntime()
    res = asyncio.run(run_workflow_engine(
        _flow(), workflow_name="loan",
        run_segment=lambda **kw: rt.run_segment(**kw),
        resolve_unfilled=lambda **kw: rt.resolve_slots(**kw),
    ))
    # The first segment pauses as an action hand-off; the engine yields the
    # resume cursor on the branch step.
    assert res.paused is True
    assert res.paused_step == "decide"
    action = decode_action(res.question)
    assert action is not None
    assert action["actions"] == ["Decide the application"]
    assert [v["variableName"] for v in action["branch_variables"]] == ["approved"]


def test_seed_advances_and_routes_branch():
    flow = _flow()
    # Round 1: start -> hand off `decide`.
    rt = InlineRuntime()
    r1 = asyncio.run(run_workflow_engine(
        flow, workflow_name="loan",
        run_segment=lambda **kw: rt.run_segment(**kw),
        resolve_unfilled=lambda **kw: rt.resolve_slots(**kw),
    ))
    assert r1.paused and r1.paused_step == "decide"

    # Round 2: resume at `decide`, seed approved=true; engine evaluates the
    # branch to `approve`, which then hands off.
    rt2 = InlineRuntime()
    rt2.seed_result(SegmentResult(captured_slots={"approved": True}))
    r2 = asyncio.run(run_workflow_engine(
        flow, workflow_name="loan",
        run_segment=lambda **kw: rt2.run_segment(**kw),
        start_step_id=r1.paused_step,
        slots=dict(r1.slots),
        resolve_unfilled=lambda **kw: rt2.resolve_slots(**kw),
    ))
    assert r2.paused and r2.paused_step == "approve"
    action = decode_action(r2.question)
    assert action["actions"] == ["Approve it"]

    # Round 3: resume at `approve`, seed empty; non-branch segment -> workflow
    # ends.
    rt3 = InlineRuntime()
    rt3.seed_result(SegmentResult(captured_slots={}))
    r3 = asyncio.run(run_workflow_engine(
        flow, workflow_name="loan",
        run_segment=lambda **kw: rt3.run_segment(**kw),
        start_step_id=r2.paused_step,
        slots=dict(r2.slots),
        resolve_unfilled=lambda **kw: rt3.resolve_slots(**kw),
    ))
    assert r3.done is True
    assert r3.slots.get("approved") is True


def test_run_segment_warns_once_when_agent_pin_is_ignored(capsys):
    rt = InlineRuntime()
    asyncio.run(rt.run_segment(
        actions=["x"], branch_variables=[], system_notes=[], slots={},
        agent="researcher",
    ))
    asyncio.run(rt.run_segment(
        actions=["y"], branch_variables=[], system_notes=[], slots={},
        agent="researcher",
    ))
    err = capsys.readouterr().err
    assert err.count("researcher") == 1
    assert "no per-agent overrides" in err


def test_resolve_slots_is_deterministic_tier0_only():
    rt = InlineRuntime()
    flow = {
        "steps": {"s1": {"type": "question", "choices": [{
            "expressionList": [
                {"variable": "amount", "operator": "greater than", "value": "100"}
            ]}]}}
    }
    out = asyncio.run(rt.resolve_slots(
        flow=flow, step_id="s1",
        variables=[{"variableName": "amount", "dataType": "number"}],
        slots={"__last_user_message__": "the amount is 42"},
    ))
    assert out == {"amount": 42}
