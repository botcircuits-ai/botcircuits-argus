"""Segment execution (`agent/segments.py`) — termination semantics.

The bug these pin: `record_slots` used to terminate EVERY segment, so a
data-only segment that recorded its findings before performing its last
action (write the report to disk) never got the round to do it — the
workflow "completed" with the file never written.

Now recording is terminal only when the engine is actually waiting on the
values: a BRANCHING segment (branch_variables) or a listDecision. A
data-only record captures and keeps looping until the model stops calling
tools.
"""

from __future__ import annotations

import asyncio

from botcircuits.agent.loop import Agent
from botcircuits.agent.tools import ToolRegistry
from botcircuits.agent.tools.registry import LocalTool
from botcircuits.agent.workflow.engine.segment_exec import RECORD_SLOTS_TOOL

from fakes import ScriptedProvider, text_response, tool_call_response

_DATA_VARS = [{"variableName": "report", "description": "the report"}]
_BRANCH_VARS = [{"variableName": "status", "description": "the status"}]


def _agent(provider, ran) -> Agent:
    reg = ToolRegistry()
    reg.register(LocalTool(
        name="write_file", description="t",
        input_schema={"type": "object", "properties": {}},
        handler=lambda _a: ran.setdefault("wrote", True) and "written",
    ))
    return Agent(provider=provider, tools=reg, local_skills_paths=[],
                 enable_workflows=False, enable_subagents=False)


def test_data_only_record_is_not_terminal():
    """record → write → done: the write AFTER the record must still run."""
    provider = ScriptedProvider([
        tool_call_response(RECORD_SLOTS_TOOL, {"report": "# findings"}, "r1"),
        tool_call_response("write_file", {"path": "out.md"}, "w1"),
        text_response("saved"),
    ])
    ran: dict = {}

    async def run():
        agent = _agent(provider, ran)
        await agent.start()
        return await agent._run_segment(
            actions=["research", "write the report to disk"],
            branch_variables=[], system_notes=[], slots={},
            data_variables=_DATA_VARS,
        )

    seg = asyncio.run(run())
    assert seg.captured_slots == {"report": "# findings"}
    assert ran.get("wrote") is True          # the post-record action ran
    assert seg.text == "saved"
    assert not provider.responses            # all three rounds consumed


def test_branching_record_still_terminates_the_segment():
    """With branch variables, the recorded values ARE what the engine is
    waiting on — one round, no further provider calls (an extra call would
    pop from an exhausted script and fail)."""
    provider = ScriptedProvider([
        tool_call_response(RECORD_SLOTS_TOOL, {"status": "ok"}, "r1"),
    ])

    async def run():
        agent = _agent(provider, {})
        await agent.start()
        return await agent._run_segment(
            actions=["check the status"],
            branch_variables=_BRANCH_VARS, system_notes=[], slots={},
        )

    seg = asyncio.run(run())
    assert seg.captured_slots == {"status": "ok"}
    assert not provider.responses
