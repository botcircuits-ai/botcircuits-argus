"""Workflow routing — "run <name> workflow" must reach the tool, always.

The reported failure: the model answered "run ai_trends workflow" with
clarifying questions instead of calling the `ai_trends` tool. Three layers
fix it, from best-effort to guaranteed:

  - every workflow tool's description carries an explicit invocation rule;
  - the CLI appends a `## Workflows` system-prompt block naming the
    registered workflows and forbidding clarifying questions;
  - DETERMINISTIC: the agent loop matches "run <workflow>" itself
    (`match_workflow_trigger`) and invokes the tool directly before any
    provider call — the model can't fumble routing it never performs.
"""

from __future__ import annotations

import asyncio
import json

import botcircuits.agent.workflow.local as wf_local
from botcircuits.agent.loop import Agent
from botcircuits.agent.tools import ToolRegistry
from botcircuits.agent.workflow import (
    match_workflow_trigger,
    workflow_tool,
    workflows_system_prompt,
)

from fakes import ScriptedProvider, text_response


def _record(name: str = "ai_trends", description: str | None = None) -> dict:
    rec = {
        "name": name,
        "flow": {"start": "s1", "steps": {"s1": {"type": "start"}}},
    }
    if description is not None:
        rec["description"] = description
    return rec


def test_tool_description_carries_invocation_rule():
    tool = workflow_tool(_record())
    assert tool.name == "ai_trends"
    assert "'ai_trends'" in tool.description
    assert "IMMEDIATELY" in tool.description
    assert "never ask clarifying questions" in tool.description


def test_tool_description_keeps_authored_description_as_prefix():
    tool = workflow_tool(_record(description="Summarize AI trends weekly."))
    assert tool.description.startswith("Summarize AI trends weekly.")
    assert "IMMEDIATELY" in tool.description


def test_system_prompt_block_names_workflows_and_forbids_questions():
    block = workflows_system_prompt(["order_fulfillment", "ai_trends"])
    assert "## Workflows" in block
    assert "ai_trends, order_fulfillment" in block  # sorted, listed
    assert "do NOT ask clarifying questions" in block


def test_system_prompt_block_empty_without_workflows():
    assert workflows_system_prompt([]) == ""


# -- deterministic trigger matching -------------------------------------------


def test_trigger_matches_run_requests():
    names = ["ai_trends", "order_fulfillment"]
    assert match_workflow_trigger("run ai_trends workflow", names) == "ai_trends"
    assert match_workflow_trigger("please start order_fulfillment", names) == \
        "order_fulfillment"
    assert match_workflow_trigger("Execute the ai_trends flow now", names) == \
        "ai_trends"


def test_trigger_prefers_longest_name():
    names = ["order", "order_fulfillment"]
    assert match_workflow_trigger("run order_fulfillment", names) == \
        "order_fulfillment"


def test_trigger_ignores_questions_and_non_run_text():
    names = ["ai_trends"]
    assert match_workflow_trigger("how do I run ai_trends?", names) is None
    assert match_workflow_trigger("what does ai_trends do", names) is None
    assert match_workflow_trigger("tell me about ai_trends", names) is None
    assert match_workflow_trigger("run the tests", names) is None
    # substring of another word must not match
    assert match_workflow_trigger("run ai_trends_v2", names) is None


# -- segment tool surface -------------------------------------------------------


def test_segments_hide_plan_and_confirm():
    """A workflow IS the plan — its segments must not re-plan and re-gate
    the run behind another approval prompt. `human_feedback` stays (question
    steps need it); workflow tools stay excluded as before."""
    from botcircuits.agent.tools.registry import LocalTool

    def _tool(name):
        return LocalTool(name=name, description="t",
                         input_schema={"type": "object", "properties": {}},
                         handler=lambda _a: "ok")

    reg = ToolRegistry()
    for n in ("plan_and_confirm", "human_feedback", "web_search"):
        reg.register(_tool(n))
    wf = _tool("wf_x")
    wf._workflow_state = {}
    reg.register(wf)

    agent = Agent(provider=ScriptedProvider(), tools=reg,
                  local_skills_paths=[], enable_subagents=False)
    agent.tools = agent.user_tools  # inspect pre-start surface directly

    names = {t.name for t in agent._engine_tools(None)}
    assert names == {"human_feedback", "web_search"}


# -- end-to-end: the loop routes even when the model never calls the tool ------


def _action_record() -> dict:
    return {
        "name": "ai_trends",
        "description": "check AI trends",
        "flow": {
            "start": "s1",
            "steps": {
                "s1": {"type": "agentAction",
                       "settings": {"action": "Summarize AI trends."}},
            },
        },
    }


def test_run_request_executes_workflow_without_model_routing(tmp_path, monkeypatch):
    """The provider NEVER issues a workflow tool call — it only ever answers
    with text (the clarifying-questions failure mode). The loop's trigger
    must still run the workflow and the transcript must show its result."""
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    build = tmp_path / ".build"
    build.mkdir(parents=True)
    (build / "ai_trends.json").write_text(json.dumps(_action_record()))
    wf_local._SESSIONS.clear()

    reg = ToolRegistry()
    reg.register(workflow_tool(_action_record()))

    provider = ScriptedProvider([
        text_response("acted on the segment"),   # engine segment call
        text_response("workflow finished, here's the summary"),  # relay
    ])

    async def run():
        agent = Agent(provider=provider, tools=reg, local_skills_paths=[],
                      enable_subagents=False)
        reply, sid = await agent.chat("run ai_trends workflow")
        convo = agent.store.get_or_create(sid)
        wf_calls = [
            b for m in convo.messages if m.role == "assistant"
            for b in m.blocks
            if b.get("type") == "tool_call" and b.get("name") == "ai_trends"
        ]
        return reply, wf_calls

    reply, wf_calls = asyncio.run(run())
    assert wf_calls, "loop did not inject the workflow call"
    assert wf_calls[0]["id"].startswith("wf-autoresume-")  # loop-injected
    assert reply == "workflow finished, here's the summary"


def test_missing_inputs_surface_one_deterministic_question(tmp_path, monkeypatch):
    """A workflow with `input: true` variables and nothing to resolve them
    from pauses BEFORE its first segment: the reply the user sees is the
    engine's authored-description question — not a segment-model improv."""
    record = {
        "name": "wf_research",
        "description": "research a topic",
        "flow": {
            "start": "start",
            "variables": [
                {"variableName": "topic", "description": "The topic to research.",
                 "input": True},
            ],
            "steps": {
                "start": {"type": "start", "next": "s1"},
                "s1": {"type": "agentAction",
                       "settings": {"action": "Research `topic`."}},
            },
        },
    }
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    build = tmp_path / ".build"
    build.mkdir(parents=True)
    (build / "wf_research.json").write_text(json.dumps(record))
    wf_local._SESSIONS.clear()

    reg = ToolRegistry()
    reg.register(workflow_tool(record))
    provider = ScriptedProvider([
        # Only the conversational relay of the pause — NO segment call
        # happens, proving collection gates the first segment.
        text_response("To run wf_research, please provide:\n"
                      "- topic — The topic to research."),
    ])

    async def run():
        agent = Agent(provider=provider, tools=reg, local_skills_paths=[],
                      enable_subagents=False)
        reply, sid = await agent.chat("run wf_research")
        convo = agent.store.get_or_create(sid)
        results = [
            b["content"] for m in convo.messages for b in m.blocks
            if b.get("type") == "tool_result" and b.get("name") == "wf_research"
        ]
        return reply, results

    reply, results = asyncio.run(run())
    assert results and "topic — The topic to research." in results[0]
    assert "topic" in reply
