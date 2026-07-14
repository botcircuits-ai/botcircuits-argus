"""Engine-driven advancement + legacy resolver-parity guarantees.

  1. Engine-driven: when a workflow tool fires, the ENGINE owns the loop
     and advances the workflow itself — the model never has to re-call
     the tool to step it forward. A branching workflow resolves its
     branch from the slots captured in the segment call.
  2. Legacy `run_workflow` path (still used when the engine `run_segment`
     callback isn't supplied) hands the slot resolver `raw_args={}` plus
     the last user message, and Layer B still fires for whatever the
     resolver leaves unresolved.
"""

from __future__ import annotations

import asyncio
import json

import botcircuits.agent.workflow.local as wf_local
from botcircuits.agent.loop import Agent
from botcircuits.agent.tools import ToolRegistry
from botcircuits.agent.workflow import workflow_tool
from botcircuits.agent.workflow.engine.segment_exec import RECORD_SLOTS_TOOL
from botcircuits.providers.base import LLMProvider
from botcircuits.types import LLMResponse, ToolCall


def _var(name: str, dtype: str = "string", description: str = "") -> dict:
    return {"variableName": name, "dataType": dtype, "description": description}


def _branching_record() -> dict:
    """start → s1 (branches on order_status) → s_delivered | s_escalate."""
    return {
        "name": "wf_branch",
        "description": "test workflow",
        "flow": {
            "start": "start",
            "variables": [_var("order_status", "string", "delivery state")],
            "steps": {
                "start": {"type": "start", "next": "s1"},
                "s1": {
                    "type": "agentAction",
                    "settings": {"action": "look up the order status"},
                    "next": "s_escalate",
                    "choices": [
                        {
                            "operator": "OR",
                            "expressionList": [
                                {
                                    "variable": "order_status",
                                    "operator": "is",
                                    "value": "delivered",
                                }
                            ],
                            "next": "s_delivered",
                        }
                    ],
                },
                "s_delivered": {
                    "type": "agentAction",
                    "settings": {"action": "tell the user it was delivered"},
                },
                "s_escalate": {
                    "type": "agentAction",
                    "settings": {"action": "escalate to support"},
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


class ScriptedProvider(LLMProvider):
    """Plays back canned LLMResponses; records each system prompt seen."""

    name = "scripted"
    model = "test"

    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.seen_systems: list[str] = []

    async def complete(self, system, messages, tools, hosted_mcp,
                       skills, max_tokens) -> LLMResponse:
        self.seen_systems.append(system or "")
        return self.responses.pop(0)

    async def stream(self, system, messages, tools, hosted_mcp,
                     skills, max_tokens):
        yield ("final", await self.complete(
            system, messages, tools, hosted_mcp, skills, max_tokens))


def _text(text: str) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=[], stop_reason="end_turn", raw=None)


def _call(name: str, args: dict) -> LLMResponse:
    return LLMResponse(
        text="", stop_reason="tool_use", raw=None,
        tool_calls=[ToolCall(id="t1", name=name, arguments=args)],
    )


def test_engine_drives_workflow_end_to_end(tmp_path, monkeypatch):
    """End-to-end through the REAL agent loop in engine-driven mode: the
    model kicks off the workflow with one tool call, then the ENGINE owns
    the loop. It calls back per segment; the segment call performs the s1
    action and reports `order_status` via the synthetic `record_slots`
    tool, so the engine branches deterministically to s_delivered, runs
    it, and completes — all inside the single workflow-tool call. The model
    never re-calls the workflow tool to advance.
    """
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    _write_build(tmp_path, _branching_record())
    wf_local._SESSIONS.clear()

    from botcircuits.agent.workflow.engine.segment_exec import ENGINE_SYSTEM_PROMPT

    class RoutingProvider(LLMProvider):
        """Routes by system prompt: the engine-mode prompt → segment
        responses (perform action, record slots); anything else → the main
        conversational loop responses."""
        name = "routing"
        model = "test"

        def __init__(self):
            self.segment_calls = 0

        async def complete(self, system, messages, tools, hosted_mcp,
                           skills, max_tokens):
            if system == ENGINE_SYSTEM_PROMPT:
                self.segment_calls += 1
                # Has this segment a record_slots tool? Then it's the
                # branching s1 segment — report delivered. Else just act.
                has_record = any(
                    getattr(t, "name", "") == RECORD_SLOTS_TOOL for t in tools
                )
                if has_record:
                    return LLMResponse(
                        text="checked", stop_reason="tool_use", raw=None,
                        tool_calls=[ToolCall(
                            id=f"rs{self.segment_calls}", name=RECORD_SLOTS_TOOL,
                            arguments={"order_status": "delivered"})],
                    )
                return _text("acted on the segment")
            # Main loop: round 1 triggers the workflow; round 2 (after the
            # summary result) is the final reply.
            triggered = any(
                b.get("type") == "tool_call" and b.get("name") == "wf_branch"
                for m in messages if m.role == "assistant" for b in m.blocks
            )
            if not triggered:
                return _call("wf_branch", {})
            return _text("all done")

        async def stream(self, system, messages, tools, hosted_mcp,
                         skills, max_tokens):
            yield ("final", await self.complete(
                system, messages, tools, hosted_mcp, skills, max_tokens))

    provider = RoutingProvider()
    reg = ToolRegistry()
    reg.register(workflow_tool(_branching_record()))

    async def run():
        async with Agent(provider=provider, tools=reg,
                         local_skills_paths=[]) as agent:
            return await agent.chat("the courier says delivered"), agent

    (reply, sid), agent = asyncio.run(run())
    assert reply == "all done"
    # The engine made at least the s1 (branching) segment call.
    assert provider.segment_calls >= 1

    # The workflow tool's result (the summary line) shows the workflow
    # completed — proving the engine advanced past the branch on its own.
    convo = agent.store.get_or_create(sid)
    wf_results = [
        b["content"] for m in convo.messages if m.role == "user"
        for b in m.blocks
        if b.get("type") == "tool_result" and b.get("name") == "wf_branch"
    ]
    assert wf_results
    assert "completed" in wf_results[0]


def test_empty_args_recall_invokes_resolver_like_before(tmp_path, monkeypatch):
    """run_workflow-level parity: an empty-args re-entry hands the slot
    resolver exactly what the pre-Option-2 implementation did —
    raw_args={} plus the last user message — and branches on its result.
    """
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    _write_build(tmp_path, _branching_record())
    wf_local._SESSIONS.clear()

    seen: dict = {}
    orig_resolve = wf_local.resolve_slots

    def spy(**kwargs):
        seen.update(kwargs)
        return orig_resolve(**kwargs)

    monkeypatch.setattr(wf_local, "resolve_slots", spy)

    first = asyncio.run(wf_local.run_workflow("wf_branch", {}))
    second = asyncio.run(wf_local.run_workflow(
        "wf_branch", {},
        session_id=first["session_id"],
        last_user_message="the courier says delivered",
    ))

    assert seen["raw_args"] == {}
    assert seen["last_user_message"] == "the courier says delivered"
    assert seen["step_id"] == "s1"
    assert [v["variableName"] for v in seen["variables"]] == ["order_status"]
    assert second["running_step"] == "s_delivered"


def test_empty_args_recall_still_falls_back_to_layer_b(tmp_path, monkeypatch):
    """When the resolver can't satisfy the branch variable from an
    empty-args recall, Layer B still runs over the transcript context —
    same degradation chain as before Option 2.
    """
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    _write_build(tmp_path, _branching_record())
    wf_local._SESSIONS.clear()

    recorded: dict = {}

    async def fake_normalize(**kwargs):
        recorded.update(kwargs)
        return {"order_status": "delivered"}

    monkeypatch.setattr(wf_local, "normalize_variables", fake_normalize)
    provider = object()  # run_workflow only checks `is not None`

    first = asyncio.run(wf_local.run_workflow("wf_branch", {}))
    second = asyncio.run(wf_local.run_workflow(
        "wf_branch", {},
        session_id=first["session_id"],
        provider=provider,
        # No choice literal, no number, no yes/no → resolver leaves
        # order_status unresolved → Layer B must be consulted.
        last_user_message="it arrived at my door this morning",
    ))

    assert [v["variableName"] for v in recorded["variables"]] == ["order_status"]
    assert recorded["raw_args"] == {}
    assert recorded["last_user_message"] == "it arrived at my door this morning"
    assert second["running_step"] == "s_delivered"
