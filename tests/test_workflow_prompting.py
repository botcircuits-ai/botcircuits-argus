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
    strip_workflow_trigger,
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
    # substring of another SLUG token must not match ("ai_trends" ⊂
    # "ai_trends_v2" only as a prefix — a different workflow).
    assert match_workflow_trigger("run ai_trends_v2", names) is None


def test_trigger_matches_the_name_as_spoken():
    """The name routes whether written as the exact slug OR spelled out with
    spaces/hyphens and even a per-word typo — otherwise a natural phrasing
    silently falls through to the model, which then skips the deterministic
    inputs/options flow. (Regression: "run deep research assistance".)"""
    names = ["deep_research_assistant"]
    for msg in ("run deep_research_assistant",
                "run deep research assistant",
                "run deep-research-assistant",
                "run deep research assistance",   # "assistance" ≈ "assistant"
                "run deep researh assistnat"):    # two typos
        assert match_workflow_trigger(msg, names) == "deep_research_assistant", msg


def test_spoken_and_slug_forms_route_the_same_longest_name():
    names = ["order_fulfillment", "order_fulfillment_eu"]
    assert match_workflow_trigger("run order_fulfillment_eu", names) == \
        "order_fulfillment_eu"
    assert match_workflow_trigger("run order fulfillment eu", names) == \
        "order_fulfillment_eu"
    assert match_workflow_trigger("run order fulfillment", names) == \
        "order_fulfillment"


def test_trigger_verb_matched_as_whole_word_not_substring():
    """"overrun"/"prerun" contain "run" but are NOT run requests — the old
    substring gate falsely accepted them, and the typo'd verb then leaked
    into slot extraction. (Regression: "trun deep_research_assistant" saved
    topic="trun".)"""
    names = ["deep_research_assistant"]
    assert match_workflow_trigger("overrun deep_research_assistant", names) is None
    assert match_workflow_trigger("prerun deep_research_assistant", names) is None


def test_trigger_verb_tolerates_a_typo():
    names = ["deep_research_assistant", "ai_trends"]
    assert match_workflow_trigger("trun deep_research_assistant", names) == \
        "deep_research_assistant"
    assert match_workflow_trigger("starrt ai_trends", names) == "ai_trends"


def test_strip_drops_a_typoed_leading_verb():
    """A typo'd verb must not survive into `last_user_message` — else
    extraction turns "trun" into the topic."""
    assert strip_workflow_trigger(
        "trun deep_research_assistant", "deep_research_assistant") == ""
    out = strip_workflow_trigger(
        "starrt ai_trends on climate policy", "ai_trends")
    assert out == "on climate policy"


# -- model-issued trigger args can't poison produced variables -----------------


def test_trigger_args_seed_only_input_variables(tmp_path, monkeypatch):
    """A model-issued workflow call sometimes pads its args with junk for
    PRODUCED variables (research_report: "N/A"). With `input: true` marks
    declared, only input variables may be seeded from the call."""
    record = {
        "name": "wf_r",
        "description": "research",
        "flow": {
            "start": "start",
            "variables": [
                {"variableName": "topic", "description": "the topic",
                 "input": True},
                {"variableName": "report", "description": "produced report"},
            ],
            "steps": {
                "start": {"type": "start", "next": "s1"},
                "s1": {"type": "agentAction",
                       "settings": {"action": "Research `topic` into `report`."}},
            },
        },
    }
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    build = tmp_path / ".build"
    build.mkdir(parents=True)
    (build / "wf_r.json").write_text(json.dumps(record))
    wf_local._SESSIONS.clear()

    seen: dict = {}

    async def run_segment(**kw):
        seen.update(kw)
        from botcircuits.agent.workflow.engine.runner import SegmentResult
        return SegmentResult(text="ok", captured_slots={})

    tool = workflow_tool(record)

    async def run():
        return await tool.handler(
            {"topic": "AI in finance", "report": "N/A"},
            {"run_segment": run_segment, "last_user_message": ""},
        )

    asyncio.run(run())
    assert seen["slots"]["topic"] == "AI in finance"
    assert "report" not in seen["slots"]  # junk for a produced var dropped


# -- trigger stripping: the command must never become a variable value ---------


def test_strip_removes_pure_command_entirely():
    assert strip_workflow_trigger("run deep_research_assistant", "deep_research_assistant") == ""
    assert strip_workflow_trigger("please start ai_trends workflow now", "ai_trends") == ""


def test_strip_keeps_the_actual_input():
    out = strip_workflow_trigger(
        "run deep_research_assistant on AI in finance, 3 pages",
        "deep_research_assistant")
    assert "AI in finance" in out and "3 pages" in out
    assert "deep_research_assistant" not in out


def test_strip_tolerates_spaces_hyphens_and_typos():
    """The name is matched as spoken, not just as the exact slug — including
    per-word typos ("researh assistnat"). A typo'd bare trigger must strip
    to "" or extraction turns the command into a topic."""
    name = "deep_research_assistant"
    assert strip_workflow_trigger("run deep research assistant", name) == ""
    assert strip_workflow_trigger("run deep-research-assistant", name) == ""
    assert strip_workflow_trigger("run deep researh assistnat", name) == ""
    out = strip_workflow_trigger(
        "run deep researh assistnat on AI in finance, 3 pages", name)
    assert "AI in finance" in out and "researh" not in out


def test_strip_never_eats_topic_words_shared_with_the_name():
    """Name words are only removed as a contiguous sequence — a topic that
    shares a word with the workflow name survives."""
    out = strip_workflow_trigger(
        "run deep_research_assistant about deep learning",
        "deep_research_assistant")
    assert "about deep learning" in out


def test_triggered_workflow_receives_stripped_context():
    """The tool context's last_user_message for a triggered call is the
    remainder after the command phrase — "" for a bare "run <name>"."""
    seen: dict = {}

    def _handler(args: dict, context: dict | None = None) -> str:
        seen["last_user_message"] = (context or {}).get("last_user_message")
        return "done"

    from botcircuits.agent.tools.registry import LocalTool
    wf = LocalTool(name="wf_x", description="t",
                   input_schema={"type": "object", "properties": {}},
                   handler=_handler)
    wf._workflow_state = {}
    reg = ToolRegistry()
    reg.register(wf)

    provider = ScriptedProvider([text_response("ok")])

    async def run():
        agent = Agent(provider=provider, tools=reg, local_skills_paths=[],
                      enable_subagents=False)
        await agent.chat("run wf_x workflow")

    asyncio.run(run())
    assert seen["last_user_message"] == ""


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
