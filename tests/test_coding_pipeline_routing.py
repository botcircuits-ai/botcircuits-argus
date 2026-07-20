"""The native loop routes a coding request to the coding pipeline BEFORE
any provider call — the same deterministic-entry contract as the workflow
trigger route.

We register a stub `safe_agentic_workflow` tool (tagged like a real
workflow tool) and drive `Agent._auto_workflow_call` directly, asserting:
  - a coding request fires the pipeline tool;
  - a question about code does not;
  - an explicit `run <other-workflow>` trigger still wins over coding
    detection;
  - with the pipeline tool absent, a coding request routes nowhere;
  - the `enable_coding_pipeline=False` flag disables routing.
"""

from __future__ import annotations

import asyncio

from botcircuits.agent.loop import Agent
from botcircuits.agent.tools import LocalTool, ToolRegistry
from botcircuits.agent.workflow.coding_route import CODING_PIPELINE_WORKFLOW
from botcircuits.types import Message

from fakes import ScriptedProvider


def _workflow_stub(name: str, calls: list[str]) -> LocalTool:
    """A LocalTool that records each invocation, tagged with the
    `_workflow_state` attribute the loop uses to recognize workflow tools."""
    async def _handler(args: dict, context: dict | None = None) -> str:
        calls.append(name)
        return f"ran {name}"

    tool = LocalTool(
        name=name,
        description=f"stub workflow {name}",
        input_schema={"type": "object", "properties": {}},
        handler=_handler,
    )
    tool._workflow_state = {"session_id": None}  # type: ignore[attr-defined]
    return tool


class _Convo:
    """Minimal stand-in for a conversation the loop's helpers read."""
    def __init__(self):
        self.messages: list[Message] = []
        self.session_id = "test-sid"


def _route(agent: Agent, text: str, calls: list[str]) -> list[str]:
    """Run one `_auto_workflow_call` for `text`; return workflow names fired."""
    async def _go():
        if not agent._tools_built:
            await agent.start()
        convo = _Convo()
        convo.messages.append(
            Message(role="user", blocks=[{"type": "text", "text": text}]))
        await agent._auto_workflow_call(convo, text)
    asyncio.run(_go())
    return calls


def test_coding_request_fires_pipeline():
    calls: list[str] = []
    reg = ToolRegistry()
    reg.register(_workflow_stub(CODING_PIPELINE_WORKFLOW, calls))
    agent = Agent(provider=ScriptedProvider(), tools=reg)
    assert _route(agent, "add a dark-mode toggle component", calls) == [
        CODING_PIPELINE_WORKFLOW]


def test_question_does_not_fire_pipeline():
    calls: list[str] = []
    reg = ToolRegistry()
    reg.register(_workflow_stub(CODING_PIPELINE_WORKFLOW, calls))
    agent = Agent(provider=ScriptedProvider(), tools=reg)
    assert _route(agent, "how do I add a route in flask?", calls) == []


def test_explicit_trigger_wins_over_coding():
    calls: list[str] = []
    reg = ToolRegistry()
    reg.register(_workflow_stub(CODING_PIPELINE_WORKFLOW, calls))
    reg.register(_workflow_stub("deploy_release", calls))
    agent = Agent(provider=ScriptedProvider(), tools=reg)
    # Names a specific workflow to run — the trigger route must take it,
    # not the coding pipeline.
    assert _route(agent, "run deploy_release", calls) == ["deploy_release"]


def test_no_pipeline_registered_routes_nowhere():
    calls: list[str] = []
    reg = ToolRegistry()
    reg.register(_workflow_stub("some_other_wf", calls))
    agent = Agent(provider=ScriptedProvider(), tools=reg)
    assert _route(agent, "implement a rate-limit middleware", calls) == []


def test_flag_disables_routing():
    calls: list[str] = []
    reg = ToolRegistry()
    reg.register(_workflow_stub(CODING_PIPELINE_WORKFLOW, calls))
    agent = Agent(provider=ScriptedProvider(), tools=reg,
                  enable_coding_pipeline=False)
    assert _route(agent, "add a dark-mode toggle component", calls) == []
