"""`manager/workflows.py::list_workflows` — `last_gate` derivation from the
most recent session per workflow name."""

from __future__ import annotations

import json

import pytest

from botcircuits.agent.workflow.paths import WORKFLOWS_DIR_ENV
from botcircuits.manager import store, workflows


@pytest.fixture
def env(tmp_path, monkeypatch):
    workflows_dir = tmp_path / "workflows"
    workflows_dir.mkdir()
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    monkeypatch.setenv(WORKFLOWS_DIR_ENV, str(workflows_dir))
    monkeypatch.setenv(store.SESSIONS_DIR_ENV, str(sessions_dir))
    return workflows_dir, sessions_dir


def _write_workflow(workflows_dir, name: str) -> None:
    doc = {"name": name, "description": "d", "flow": {"start": "s1", "steps": {"s1": {}}}}
    (workflows_dir / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")


def _write_session(sessions_dir, sid: str, *, name: str, verification_events: list[dict]) -> None:
    trace = [{"seq": 0, "ts": "t", "type": "session_start", "step": None,
              "duration_ms": None, "slots": {}, "data": {}}]
    for i, ev in enumerate(verification_events, start=1):
        trace.append({"seq": i, "ts": "t", **ev})
    doc = {
        "session_id": sid,
        "agent": {"runtime": "claude-code"},
        "workflow": {"name": name, "start": "2026-01-01T00:00:00Z", "end": None, "initial_slots": {}},
        "trace": trace,
        "memory": {"nodes": [], "edges": []},
    }
    (sessions_dir / f"{sid}-session.json").write_text(json.dumps(doc), encoding="utf-8")


def test_no_sessions_last_gate_is_none(env):
    workflows_dir, _ = env
    _write_workflow(workflows_dir, "wf_a")

    out = workflows.list_workflows()
    assert len(out) == 1
    assert out[0]["last_gate"] is None


def test_no_gate_events_last_gate_is_none(env):
    workflows_dir, sessions_dir = env
    _write_workflow(workflows_dir, "wf_a")
    _write_session(sessions_dir, "s1", name="wf_a", verification_events=[])

    out = workflows.list_workflows()
    assert out[0]["last_gate"] is None


def test_last_gate_reflects_most_recent_session(env):
    workflows_dir, sessions_dir = env
    _write_workflow(workflows_dir, "wf_a")
    _write_session(sessions_dir, "s1", name="wf_a", verification_events=[
        {"type": "verification", "step": None, "duration_ms": None, "slots": {},
         "data": {"attempt": 1, "passed": False, "checks": [{"id": "chk", "passed": False}]}},
    ])
    _write_session(sessions_dir, "s2", name="wf_a", verification_events=[
        {"type": "verification", "step": None, "duration_ms": None, "slots": {},
         "data": {"attempt": 1, "passed": True, "checks": [{"id": "chk", "passed": True}]}},
    ])
    # s2 must be the newer file for "most recent" to pick it.
    import os
    import time
    s1 = sessions_dir / "s1-session.json"
    s2 = sessions_dir / "s2-session.json"
    now = time.time()
    os.utime(s1, (now - 100, now - 100))
    os.utime(s2, (now, now))

    out = workflows.list_workflows()
    assert out[0]["last_gate"]["passed"] is True
    assert out[0]["last_gate"]["attempts"] == 1
