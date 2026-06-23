"""Tests for the non-pausing `systemAction` step type.

Bookkeeping steps (record a decision, mark an item processed, terminal
"all done") used to be authored as `agentAction`s, each costing a full
LLM round-trip with the whole context attached. `systemAction` runs them
engine-side:

  - the (slot-interpolated) action text is recorded as an audit note and
    surfaced on the NEXT pause (so the transcript keeps the trail),
  - branching on a systemAction is evaluated IMMEDIATELY against current
    slots (no pendingBranch / re-entry cycle),
  - a chain of systemActions collapses into the surrounding pauses — and
    a terminal chain ends the workflow with `done=True, action=None`,
    which the tool wrapper flags as a "quiet finish" so the agent loop
    skips the closing provider call.
"""

from __future__ import annotations

import asyncio
import json

import botcircuits.agent.workflow.local as wf_local
from botcircuits.agent.tools import ToolRegistry
from botcircuits.agent.workflow import (
    workflow_finished_quietly,
    workflow_tool,
)
from botcircuits.agent.workflow.cli_commands import render_system_notes


def _var(name: str, dtype: str = "string", description: str = "") -> dict:
    return {"variableName": name, "dataType": dtype, "description": description}


def _record_with_system_chain() -> dict:
    """start → act1 (pauses, branches on `verdict`) → [mark_ok | mark_bad]
    (systemActions) → act2 (pauses) → finish (terminal systemAction).

    `mark_ok` itself branches on an already-filled slot (`amount`) to
    prove immediate evaluation: amount > 100 → mark_big, else mark_ok's
    static next (act2).
    """
    return {
        "name": "wf_sys",
        "description": "test workflow",
        "flow": {
            "start": "start",
            "variables": [
                _var("verdict", "string", "one of: ok | bad"),
                _var("amount", "number", "the checked amount"),
            ],
            "steps": {
                "start": {"type": "start", "next": "act1"},
                "act1": {
                    "type": "agentAction",
                    "settings": {"action": "check the thing"},
                    "next": "mark_bad",
                    "choices": [
                        {
                            "operator": "AND",
                            "expressionList": [
                                {"variable": "verdict", "operator": "is",
                                 "value": "ok"},
                            ],
                            "next": "mark_ok",
                        }
                    ],
                },
                "mark_ok": {
                    "type": "systemAction",
                    "settings": {"action": "Recorded verdict=ok amount={amount}"},
                    "next": "act2",
                    "choices": [
                        {
                            "operator": "AND",
                            "expressionList": [
                                {"variable": "amount",
                                 "operator": "greater than", "value": 100},
                            ],
                            "next": "mark_big",
                        }
                    ],
                },
                "mark_big": {
                    "type": "systemAction",
                    "settings": {"action": "Flagged as big-ticket"},
                    "next": "act2",
                },
                "mark_bad": {
                    "type": "systemAction",
                    "settings": {"action": "Recorded verdict=bad"},
                    "next": "act2",
                },
                "act2": {
                    "type": "agentAction",
                    "settings": {"action": "summarize the outcome"},
                    "next": "finish",
                },
                "finish": {
                    "type": "systemAction",
                    "settings": {"action": "Processing complete."},
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


def _start(tmp_path, monkeypatch, record: dict) -> None:
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    _write_build(tmp_path, record)
    wf_local._SESSIONS.clear()


# --- engine: collapse, notes, immediate branching ----------------------------

def test_system_chain_collapses_into_next_pause(tmp_path, monkeypatch):
    _start(tmp_path, monkeypatch, _record_with_system_chain())

    first = asyncio.run(wf_local.run_workflow("wf_sys", {}))
    assert first["running_step"] == "act1"

    # Re-enter with branch args: verdict=ok routes to mark_ok; amount=50
    # keeps mark_ok's static next. Both systemActions run engine-side and
    # the walk lands on act2 in ONE call.
    second = asyncio.run(wf_local.run_workflow(
        "wf_sys", {"verdict": "ok", "amount": 50},
        session_id=first["session_id"],
    ))
    assert second["running_step"] == "act2"
    assert not second["done"]
    # The note is slot-interpolated and surfaced for the directive.
    assert second["system_notes"] == ["Recorded verdict=ok amount=50"]


def test_system_action_branches_immediately_on_filled_slots(tmp_path, monkeypatch):
    _start(tmp_path, monkeypatch, _record_with_system_chain())

    first = asyncio.run(wf_local.run_workflow("wf_sys", {}))
    second = asyncio.run(wf_local.run_workflow(
        "wf_sys", {"verdict": "ok", "amount": 500},
        session_id=first["session_id"],
    ))
    # amount=500 > 100 → mark_ok's choice routed through mark_big too.
    assert second["running_step"] == "act2"
    assert second["system_notes"] == [
        "Recorded verdict=ok amount=500",
        "Flagged as big-ticket",
    ]


def test_terminal_system_chain_finishes_quietly(tmp_path, monkeypatch):
    _start(tmp_path, monkeypatch, _record_with_system_chain())

    first = asyncio.run(wf_local.run_workflow("wf_sys", {}))
    second = asyncio.run(wf_local.run_workflow(
        "wf_sys", {"verdict": "bad"}, session_id=first["session_id"],
    ))
    assert second["running_step"] == "act2"

    # act2 is a plain pause; the next (auto-recall-shaped) call walks the
    # terminal systemAction and the workflow ends with no action left.
    third = asyncio.run(wf_local.run_workflow(
        "wf_sys", {}, session_id=second["session_id"],
    ))
    assert third["done"] is True
    assert third["action"] is None
    assert third["system_notes"] == ["Processing complete."]
    # Session dropped — a fresh call restarts from the beginning.
    assert second["session_id"] not in wf_local._SESSIONS


# --- tool wrapper: notes in directive, quiet-finish flag ---------------------

def test_workflow_tool_prepends_notes_and_flags_quiet_finish(tmp_path, monkeypatch):
    _start(tmp_path, monkeypatch, _record_with_system_chain())

    tool = workflow_tool(_record_with_system_chain(),
                         provider=None, normalize_enabled=False)
    reg = ToolRegistry()
    reg.register(tool)

    out1 = asyncio.run(tool.handler({}))
    assert "check the thing" in out1
    assert not workflow_finished_quietly(reg, "wf_sys")

    out2 = asyncio.run(tool.handler({"verdict": "bad"}))
    # The systemAction audit note rides on the next directive.
    assert "Recorded by the workflow engine" in out2
    assert "Recorded verdict=bad" in out2
    assert "summarize the outcome" in out2
    assert not workflow_finished_quietly(reg, "wf_sys")

    out3 = asyncio.run(tool.handler({}))
    # Terminal systemAction → quiet finish: notes + "finished" fallback.
    # (Legacy per-step path: still exercised when the engine `run_segment`
    # callback isn't supplied in the tool context.)
    assert "Processing complete." in out3
    assert "finished with no further actions" in out3
    assert workflow_finished_quietly(reg, "wf_sys")


def test_render_system_notes_block():
    assert render_system_notes([]) == ""
    block = render_system_notes(["a", "b"])
    assert block.startswith("Recorded by the workflow engine")
    assert "- a" in block and "- b" in block
