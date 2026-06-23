"""Tests for Option 2 — the main loop's tool call carries the slots.

When the engine pauses on a branching step, the slots the branch needs
should ride the model's own re-call of the workflow tool (raw args, the
resolver's highest-priority source) instead of being re-derived from a
transcript snapshot. Covered here:

  - `run_workflow` surfaces the pending branch's filtered variable
    schema as `branch_variables`, and a follow-up call carrying those
    values branches deterministically with no provider.
  - `workflow_tool` mirrors `branch_variables` onto the tool's
    `input_schema` and into `_workflow_state`, and resets both when the
    pause isn't branching / the workflow ends.
  - `compose_workflow_step_directive` asks for the re-call (with the
    variable list) only when a branch is pending.
  - The agent loop's `[Active workflow]` reminder flips from "do NOT
    call" to "call '<name>' passing …" when branch variables exist.
"""

from __future__ import annotations

import asyncio
import json

import botcircuits.agent.workflow.local as wf_local
from botcircuits.agent.core import _with_workflow_reminder
from botcircuits.agent.tools import LocalTool, ToolRegistry
from botcircuits.agent.workflow import (
    workflow_branch_variables,
    workflow_tool,
)
from botcircuits.agent.workflow.cli_commands import (
    compose_workflow_step_directive,
    render_branch_variable_lines,
)


def _var(name: str, dtype: str = "string", description: str = "") -> dict:
    return {"variableName": name, "dataType": dtype, "description": description}


def _branching_record() -> dict:
    """A built workflow: start → s1 (branches on order_status) →
    s_delivered | s_escalate (both terminal)."""
    return {
        "name": "wf_branch",
        "description": "test workflow",
        "flow": {
            "start": "start",
            "variables": [
                _var("order_status", "string", "one of: delivered | shipped"),
                _var("unrelated", "number", "not referenced by the branch"),
            ],
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


# --- run_workflow: branch_variables + args-driven branching -----------------

def test_run_workflow_surfaces_branch_variables(tmp_path, monkeypatch):
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    _write_build(tmp_path, _branching_record())
    wf_local._SESSIONS.clear()

    result = asyncio.run(wf_local.run_workflow("wf_branch", {}))

    assert result["running_step"] == "s1"
    assert not result["done"]
    names = [v["variableName"] for v in result["branch_variables"]]
    # Filtered to what the branch references — `unrelated` is excluded.
    assert names == ["order_status"]


def test_run_workflow_args_resolve_branch_without_provider(tmp_path, monkeypatch):
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    _write_build(tmp_path, _branching_record())
    wf_local._SESSIONS.clear()

    first = asyncio.run(wf_local.run_workflow("wf_branch", {}))
    second = asyncio.run(wf_local.run_workflow(
        "wf_branch",
        {"order_status": "delivered"},
        session_id=first["session_id"],
    ))

    # The model-supplied arg hit the resolver's raw-args source and the
    # branch routed to the matching step — no provider, no Layer B.
    assert second["running_step"] == "s_delivered"
    # Terminal step (no next/conditions) → no pending branch surfaced.
    assert second["branch_variables"] == []


def test_run_workflow_no_branch_variables_on_terminal_pause(tmp_path, monkeypatch):
    record = _branching_record()
    # Make s1 non-branching and terminal: drop choices and next.
    record["flow"]["steps"]["s1"] = {
        "type": "agentAction",
        "settings": {"action": "say hi"},
    }
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    _write_build(tmp_path, record)
    wf_local._SESSIONS.clear()

    result = asyncio.run(wf_local.run_workflow("wf_branch", {}))
    assert result["done"]
    assert result["branch_variables"] == []


# --- workflow_tool: schema mirroring + state --------------------------------

def _fake_run_workflow(results: list[dict]):
    """Async stand-in for local.run_workflow popping canned results."""
    async def fake(workflow_name, args, **kwargs):
        return results.pop(0)
    return fake


def _result(**overrides) -> dict:
    base = {
        "status": "ok",
        "workflow_name": "wf_branch",
        "session_id": "sid-1",
        "action": "do the step",
        "done": False,
        "kind": None,
        "running_step": "s1",
        "messages": [],
        "conditions": [],
        "choices": [],
        "variables": [],
        "branch_variables": [],
    }
    base.update(overrides)
    return base


def test_workflow_tool_widens_and_resets_input_schema(monkeypatch):
    branch_vars = [_var("order_status", "string", "delivery state"),
                   _var("order_total", "number")]
    monkeypatch.setattr(wf_local, "run_workflow", _fake_run_workflow([
        _result(branch_variables=branch_vars),
        _result(),
        _result(done=True),
    ]))
    tool = workflow_tool({"name": "wf_branch", "description": "test"})
    assert tool.input_schema == {"type": "object", "properties": {}}

    out = asyncio.run(tool.handler({}))
    props = tool.input_schema["properties"]
    assert props["order_status"] == {
        "type": "string", "description": "delivery state",
    }
    assert props["order_total"] == {"type": "number"}
    assert tool._workflow_state["branch_variables"] == branch_vars
    assert "call 'wf_branch' again" in out
    assert "order_status (string): delivery state" in out

    # Non-branching pause → schema and state reset, no re-call ask.
    out = asyncio.run(tool.handler({}))
    assert tool.input_schema == {"type": "object", "properties": {}}
    assert tool._workflow_state["branch_variables"] == []
    assert "call 'wf_branch' again" not in out

    # Terminal turn → session cleared too.
    asyncio.run(tool.handler({}))
    assert tool._workflow_state["session_id"] is None
    assert tool.input_schema == {"type": "object", "properties": {}}


def test_workflow_branch_variables_reads_registry_state(monkeypatch):
    branch_vars = [_var("order_status")]
    monkeypatch.setattr(wf_local, "run_workflow", _fake_run_workflow([
        _result(branch_variables=branch_vars),
    ]))
    reg = ToolRegistry()
    tool = workflow_tool({"name": "wf_branch", "description": "test"})
    reg.register(tool)

    assert workflow_branch_variables(reg, "wf_branch") == []
    asyncio.run(tool.handler({}))
    assert workflow_branch_variables(reg, "wf_branch") == branch_vars
    assert workflow_branch_variables(reg, "missing") == []


# --- directive wording -------------------------------------------------------

def test_directive_asks_for_recall_only_when_branching():
    plain = compose_workflow_step_directive("wf", done=False)
    assert plain.footer == ""

    branching = compose_workflow_step_directive(
        "wf", done=False,
        branch_variables=[_var("order_status", "string", "state")],
    )
    assert "call 'wf' again" in branching.footer
    assert "- order_status (string): state" in branching.footer

    # done=True wins: final step never asks for a re-call.
    final = compose_workflow_step_directive(
        "wf", done=True, branch_variables=[_var("order_status")],
    )
    assert final.footer == "This is the FINAL step of this workflow."


def test_question_directive_keeps_human_feedback_then_recall():
    d = compose_workflow_step_directive(
        "wf", done=False, kind="question",
        branch_variables=[_var("order_id", "string")],
    )
    assert "human_feedback" in d.body
    assert "After the user replies, call 'wf'" in d.footer
    assert "- order_id (string)" in d.footer


def test_render_branch_variable_lines_skips_malformed():
    lines = render_branch_variable_lines([
        _var("a", "number", "the a"),
        {"dataType": "string"},          # no name → skipped
        {"variableName": ""},            # empty name → skipped
        _var("b"),
    ])
    assert lines == "- a (number): the a\n- b (string)"


# --- agent-loop reminder ------------------------------------------------------

def _registry_with_state(state: dict) -> ToolRegistry:
    reg = ToolRegistry()
    tool = LocalTool(
        name="wf_branch", description="t",
        input_schema={"type": "object", "properties": {}},
        handler=lambda args: "",
    )
    tool._workflow_state = state
    reg.register(tool)
    return reg


def test_reminder_resume_when_workflow_paused():
    # Engine-driven mode: a workflow with a live session_id is paused
    # waiting on the user. The reminder asks the model to re-call the tool
    # to resume — the engine owns advancement, so there is no per-step
    # "act then re-call with branch args" dance anymore.
    reg = _registry_with_state(
        {"session_id": "sid", "branch_variables": []}
    )
    system = _with_workflow_reminder("base", reg)
    assert "paused waiting for the user's reply" in system
    assert "call 'wf_branch' again to resume" in system


def test_reminder_trigger_when_no_workflow_active():
    # No active workflow but a workflow tool is registered: the reminder
    # tells the model the tool MUST be its first action on a match.
    reg = _registry_with_state(
        {"session_id": None, "branch_variables": []}
    )
    system = _with_workflow_reminder("base", reg)
    assert "[Available workflows]" in system
    assert "MANDATORY" in system
