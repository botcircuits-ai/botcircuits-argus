"""Subagents (`agent/subagents.py`) — isolated, parallel sub-loops.

A subagent returns the answer, not its transcript; its tool view is the
parent's registry minus anything user-facing, workflow-advancing, or
recursive; fan_out preserves order and isolates per-subtask failure.
"""

from __future__ import annotations

import asyncio

from botcircuits.agent.loop import Agent
from botcircuits.agent.subagents import (
    DELEGATE_TOOL,
    FAN_OUT_TOOL,
    fan_out,
    run_subagent,
    subagent_registry,
)
from botcircuits.agent.tools import ToolRegistry
from botcircuits.agent.tools.registry import LocalTool

from fakes import ScriptedProvider, text_response


class EchoProvider(ScriptedProvider):
    """Replies deterministically with the task text."""

    name = "echo"

    async def complete(self, system, messages, tools, hosted_mcp,
                       skills, max_tokens):
        task = messages[-1].blocks[0]["text"]
        return text_response(f"answer: {task}")


def _tool(name: str) -> LocalTool:
    return LocalTool(name=name, description="t",
                     input_schema={"type": "object", "properties": {}},
                     handler=lambda _a: "ok")


def test_subagent_registry_filters_unsafe_tools():
    parent = ToolRegistry()
    for name in ("read_file", "human_feedback", "plan_and_confirm",
                 DELEGATE_TOOL, FAN_OUT_TOOL, "build_workflow"):
        parent.register(_tool(name))
    wf = _tool("wf_order")
    wf._workflow_state = {"active": None}  # the workflow-tool marker
    parent.register(wf)

    names = {t.name for t in subagent_registry(parent).all()}
    assert names == {"read_file"}


def test_run_subagent_returns_only_the_answer():
    reply = asyncio.run(run_subagent("count the files",
                                     provider=EchoProvider()))
    assert reply == "answer: count the files"


def test_fan_out_preserves_order_and_isolates_failure():
    class FlakyProvider(EchoProvider):
        async def complete(self, system, messages, tools, hosted_mcp,
                           skills, max_tokens):
            task = messages[-1].blocks[0]["text"]
            if task == "boom":
                raise RuntimeError("subtask crashed")
            return await super().complete(system, messages, tools,
                                          hosted_mcp, skills, max_tokens)

    results = asyncio.run(fan_out(["a", "boom", "c"],
                                  provider=FlakyProvider()))
    assert results[0] == "answer: a"
    assert results[1].startswith("error: RuntimeError")
    assert results[2] == "answer: c"


def test_agent_start_registers_delegate_and_fan_out():
    async def run():
        agent = Agent(provider=EchoProvider(), tools=ToolRegistry(),
                      local_skills_paths=[], enable_workflows=False)
        await agent.start()
        return {t.name for t in agent.tools.all()}

    names = asyncio.run(run())
    assert DELEGATE_TOOL in names and FAN_OUT_TOOL in names


def test_subagent_worker_gets_no_spawning_tools():
    async def run():
        agent = Agent(provider=EchoProvider(), tools=ToolRegistry(),
                      local_skills_paths=[], enable_workflows=False,
                      enable_subagents=False)
        await agent.start()
        return {t.name for t in agent.tools.all()}

    names = asyncio.run(run())
    assert DELEGATE_TOOL not in names and FAN_OUT_TOOL not in names


def test_delegate_tool_runs_via_parent_registry():
    async def run():
        provider = EchoProvider()
        agent = Agent(provider=provider, tools=ToolRegistry(),
                      local_skills_paths=[], enable_workflows=False)
        await agent.start()
        out, err = await agent.tools.run(DELEGATE_TOOL,
                                         {"task": "summarize x"}, None)
        return out, err

    out, err = asyncio.run(run())
    assert err is False
    assert out == "answer: summarize x"


def test_fan_out_tool_rejects_non_list():
    async def run():
        agent = Agent(provider=EchoProvider(), tools=ToolRegistry(),
                      local_skills_paths=[], enable_workflows=False)
        await agent.start()
        out, _err = await agent.tools.run(FAN_OUT_TOOL,
                                          {"tasks": '["a", "b"]'}, None)
        return out

    assert "must be a non-empty list" in asyncio.run(run())
