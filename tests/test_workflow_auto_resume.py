"""End-to-end: a paused workflow resumes WITHOUT the model deciding to
re-call the tool.

Once a `question` step pauses (via `human_feedback`), the agent loop must
resume the engine directly from the user's next message — no system-prompt
reminder, no model tool-call decision. These tests prove that by NEVER
scripting a `wf_greet` tool call on the second turn: if the old
reminder-driven re-call were still required, there would be nothing making
the model call the tool again and the scripted provider would run out of
responses.
"""

from __future__ import annotations

import asyncio
import json

import botcircuits.agent.workflow.local as wf_local
from botcircuits.agent.loop import Agent
from botcircuits.agent.tools import ToolRegistry
from botcircuits.agent.tools.builtins.human_feedback import HUMAN_FEEDBACK_TOOL
from botcircuits.agent.workflow import active_workflow_names, workflow_tool

from fakes import (
    ScriptedProvider,
    text_response as _text,
    tool_call_response as _call,
)


def _question_record() -> dict:
    """start -> ask_name (question) -> greet (terminal)."""
    return {
        "name": "wf_greet",
        "description": "test workflow",
        "flow": {
            "start": "start",
            "steps": {
                "start": {"type": "start", "next": "ask_name"},
                "ask_name": {
                    "type": "question",
                    "settings": {"action": "What is your name?"},
                    "next": "greet",
                },
                "greet": {
                    "type": "agentAction",
                    "settings": {"action": "Greet the user by name."},
                },
            },
        },
    }


def _write_build(tmp_path, record: dict) -> None:
    build = tmp_path / ".build"
    build.mkdir(parents=True, exist_ok=True)
    (build / f"{record['name']}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )


def test_chat_auto_resumes_paused_workflow_without_model_recall(tmp_path, monkeypatch):
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    _write_build(tmp_path, _question_record())
    wf_local._SESSIONS.clear()

    reg = ToolRegistry()
    reg.register(workflow_tool(_question_record()))

    # Turn 1 (chat("run...")): model triggers the workflow -> the question
    # step's segment calls human_feedback -> the engine pauses -> the model
    # relays the question in plain text (terminal, no tool call).
    #
    # Turn 2 (chat("Ada", ...)): NO trigger call scripted here at all — if
    # the old reminder-driven re-call were still required, there would be
    # nothing making the model call `wf_greet` again and this test would
    # hang waiting on an empty `responses` queue. Auto-resume instead calls
    # the tool directly with "Ada" as the answer; the resumed segment
    # performs the greet action and the model's plain-text reply is the
    # ONLY response left to consume.
    provider = ScriptedProvider([
        _call("wf_greet", {}),                                    # trigger
        _call(HUMAN_FEEDBACK_TOOL, {"question": "What is your name?"}),
        _text("(relaying) What is your name?"),                   # turn 1 ends
        # Resume re-enters the PAUSED segment first (now with "Ada" as the
        # reply available) — its action already succeeded, so it just acts.
        _text("got the name"),                                    # resumed "ask_name" segment
        _text("greeted Ada"),                                     # next "greet" segment (terminal)
        _text("Nice to meet you, Ada!"),                          # turn 2 ends
    ])

    async def run():
        async with Agent(provider=provider, tools=reg,
                         local_skills_paths=[]) as agent:
            first_reply, sid = await agent.chat("run the greet workflow")
            assert "What is your name?" in first_reply
            assert active_workflow_names(agent.tools) == ["wf_greet"]

            second_reply, _ = await agent.chat("Ada", session_id=sid)
            return second_reply, agent

    second_reply, agent = asyncio.run(run())

    # The workflow advanced past the question step and finished —
    # session_id cleared means the engine completed, not just re-paused.
    assert active_workflow_names(agent.tools) == []
    assert second_reply == "Nice to meet you, Ada!"
    # No responses left unconsumed and none missing: exactly the scripted
    # turns ran, with no `wf_greet` re-call anywhere in turn 2.
    assert provider.responses == []


def test_chat_stream_auto_resumes_paused_workflow_without_model_recall(
    tmp_path, monkeypatch,
):
    """Same scenario through `chat_stream` — the resume must happen before
    the provider stream starts, and the synthetic resume call must surface
    as `tool_call`/`tool_result` events for the UI."""
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    _write_build(tmp_path, _question_record())
    wf_local._SESSIONS.clear()

    reg = ToolRegistry()
    reg.register(workflow_tool(_question_record()))

    provider = ScriptedProvider([
        _call("wf_greet", {}),
        _call(HUMAN_FEEDBACK_TOOL, {"question": "What is your name?"}),
        _text("(relaying) What is your name?"),
        _text("got the name"),
        _text("greeted Ada"),
        _text("Nice to meet you, Ada!"),
    ])

    async def run():
        async with Agent(provider=provider, tools=reg,
                         local_skills_paths=[]) as agent:
            sid = None
            async for ev in agent.chat_stream("run the greet workflow"):
                sid = ev.session_id or sid
            assert active_workflow_names(agent.tools) == ["wf_greet"]

            events = []
            async for ev in agent.chat_stream("Ada", session_id=sid):
                events.append(ev)
            return events, agent

    events, agent = asyncio.run(run())

    assert active_workflow_names(agent.tools) == []
    done = [e for e in events if e.type == "done"][0]
    assert done.text == "Nice to meet you, Ada!"
    # The synthetic resume call surfaced as a tool_call/tool_result pair
    # for `wf_greet` before any provider streaming started for turn 2.
    resume_calls = [
        e for e in events
        if e.type == "tool_call" and e.tool_call is not None
        and e.tool_call.name == "wf_greet"
    ]
    assert len(resume_calls) == 1
    assert provider.responses == []
