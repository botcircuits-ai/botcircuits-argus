"""`runtime/run_workflow.py::_run`'s automatic verification-gate +
self-repair loop: auto-detects `.build/<name>/verifications/gate.json`,
retries the whole workflow (capped) on a blocking gate failure, and
never silently reports "done" once the repair budget is exhausted."""

from __future__ import annotations

import asyncio
import json
import textwrap

import pytest

import botcircuits.runtime.run_workflow as rw
from botcircuits.agent.workflow import paths
from botcircuits.agent.workflow.engine.runner import SegmentResult


class _FakeRuntimeProvider:
    """Stands in for `_select_provider`'s return value — an
    `AgentRuntimeProvider`. Each call returns a fixed segment result and
    counts how many times the engine actually ran a segment, so tests can
    assert on repair attempt counts without a real LLM/CLI runtime."""

    name = "fake"

    def __init__(self):
        self.run_segment_calls = 0

    async def run_segment(self, **kw):
        self.run_segment_calls += 1
        return SegmentResult(text="done", captured_slots={})

    async def resolve_slots(self, **kw):
        return {}

    async def aclose(self):
        pass


def _record(name: str) -> dict:
    from botcircuits.agent.workflow.engine.segments import compute_segments

    flow = {
        "start": "start",
        "steps": {
            "start": {"type": "start", "next": "s1"},
            "s1": {"type": "agentAction", "settings": {"action": "do it"}},
        },
    }
    flow["segments"] = compute_segments(flow)
    return {"name": name, "description": "test", "flow": flow}


def _write_build(tmp_path, name: str) -> None:
    build = tmp_path / ".build" / name
    build.mkdir(parents=True, exist_ok=True)
    (build / f"{name}.json").write_text(json.dumps(_record(name)), encoding="utf-8")


def _write_gate(tmp_path, name: str, *, script_passes: bool) -> None:
    verif = tmp_path / ".build" / name / "verifications"
    checks_dir = verif / "checks"
    checks_dir.mkdir(parents=True, exist_ok=True)
    (checks_dir / "chk.py").write_text(textwrap.dedent(f"""\
        import json
        print(json.dumps({{"passed": {script_passes}, "detail": "checked"}}))
    """), encoding="utf-8")
    manifest = {
        "workflow_name": name,
        "version": 1,
        "checks": [{
            "id": "chk", "type": "script", "description": "d",
            "script": "verifications/checks/chk.py", "severity": "blocking",
        }],
    }
    (verif / "gate.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTCIRCUITS_WORKFLOWS_DIR", str(tmp_path))
    monkeypatch.setattr(rw, "_detect", lambda: "claude-code")
    fake = _FakeRuntimeProvider()
    monkeypatch.setattr(rw, "_select_provider", lambda *a, **kw: fake)
    return tmp_path, fake


async def _run(name: str):
    return await rw._run(name, initial_args={}, runtime_name="claude-code", reply=None)


def test_gate_passes_first_try_engine_runs_once(_isolated):
    tmp_path, fake = _isolated
    _write_build(tmp_path, "wf_ok")
    _write_gate(tmp_path, "wf_ok", script_passes=True)

    out = asyncio.run(_run("wf_ok"))
    assert out["status"] == "done"
    assert out["gate"]["attempts"] == [out["gate"]["attempts"][0]]
    assert len(out["gate"]["attempts"]) == 1
    assert fake.run_segment_calls == 1


def test_gate_fails_then_passes_engine_runs_twice(_isolated, monkeypatch):
    tmp_path, fake = _isolated
    _write_build(tmp_path, "wf_flaky")
    _write_gate(tmp_path, "wf_flaky", script_passes=True)

    # First gate evaluation fails, second passes — patch run_gate to script
    # this without needing the check script itself to change behavior.
    from botcircuits.agent.workflow.verification.types import CheckResult, GateResult

    calls = {"n": 0}

    async def fake_run_gate(gate_spec, run, *, provider=None, base_dir=None):
        calls["n"] += 1
        passed = calls["n"] >= 2
        return GateResult(
            workflow_name="wf_flaky", passed=passed,
            checks=[CheckResult(check_id="chk", passed=passed, severity="blocking")],
        )

    monkeypatch.setattr(rw, "run_gate", fake_run_gate)

    out = asyncio.run(_run("wf_flaky"))
    assert out["status"] == "done"
    assert len(out["gate"]["attempts"]) == 2
    assert out["gate"]["attempts"][0]["passed"] is False
    assert out["gate"]["attempts"][1]["passed"] is True
    assert fake.run_segment_calls == 2  # original run + one repair re-run


def test_gate_never_passes_surfaces_failure_after_budget(_isolated):
    tmp_path, fake = _isolated
    _write_build(tmp_path, "wf_broken")
    _write_gate(tmp_path, "wf_broken", script_passes=False)

    out = asyncio.run(_run("wf_broken"))
    assert out["status"] == "failure"
    # 1 original attempt + _MAX_REPAIR_ATTEMPTS repairs.
    assert len(out["gate"]["attempts"]) == 1 + rw._MAX_REPAIR_ATTEMPTS
    assert fake.run_segment_calls == 1 + rw._MAX_REPAIR_ATTEMPTS
    assert all(not a["passed"] for a in out["gate"]["attempts"])


def test_no_gate_file_behaves_exactly_like_before(_isolated):
    """A workflow with no `verifications/gate.json` must be byte-for-byte
    unaffected by this feature — no `gate` key, no extra engine calls."""
    tmp_path, fake = _isolated
    _write_build(tmp_path, "wf_nogate")

    out = asyncio.run(_run("wf_nogate"))
    assert out["status"] == "done"
    assert "gate" not in out
    assert fake.run_segment_calls == 1


def _session_doc(before_files: set):
    from botcircuits.agent.workflow.tracing import SessionTrace

    sessions_dir = SessionTrace.sessions_dir()
    files = set(sessions_dir.glob("*-session.json")) - before_files
    assert len(files) == 1
    return json.loads(next(iter(files)).read_text(encoding="utf-8"))


def _existing_session_files() -> set:
    from botcircuits.agent.workflow.tracing import SessionTrace

    sessions_dir = SessionTrace.sessions_dir()
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return set(sessions_dir.glob("*-session.json"))


def test_gate_pass_writes_verification_event_to_trace(_isolated):
    tmp_path, fake = _isolated
    _write_build(tmp_path, "wf_ok")
    _write_gate(tmp_path, "wf_ok", script_passes=True)

    before = _existing_session_files()
    asyncio.run(_run("wf_ok"))

    doc = _session_doc(before)
    verif_events = [e for e in doc["trace"] if e["type"] == "verification"]
    assert len(verif_events) == 1
    assert verif_events[0]["data"]["attempt"] == 1
    assert verif_events[0]["data"]["passed"] is True
    assert verif_events[0]["data"]["checks"][0]["id"] == "chk"
    # No retry needed when the gate passes on the first try.
    assert not [e for e in doc["trace"] if e["type"] == "retry"]


def test_gate_fails_then_passes_writes_retry_and_verification_events(_isolated, monkeypatch):
    tmp_path, fake = _isolated
    _write_build(tmp_path, "wf_flaky")
    _write_gate(tmp_path, "wf_flaky", script_passes=True)

    from botcircuits.agent.workflow.verification.types import CheckResult, GateResult

    calls = {"n": 0}

    async def fake_run_gate(gate_spec, run, *, provider=None, base_dir=None):
        calls["n"] += 1
        passed = calls["n"] >= 2
        return GateResult(
            workflow_name="wf_flaky", passed=passed,
            checks=[CheckResult(
                check_id="chk", passed=passed, severity="blocking",
                error=None if passed else "check failed",
            )],
        )

    monkeypatch.setattr(rw, "run_gate", fake_run_gate)

    before = _existing_session_files()
    asyncio.run(_run("wf_flaky"))

    doc = _session_doc(before)
    verif_events = [e for e in doc["trace"] if e["type"] == "verification"]
    retry_events = [e for e in doc["trace"] if e["type"] == "retry"]

    assert len(verif_events) == 2
    assert [e["data"]["passed"] for e in verif_events] == [False, True]

    assert len(retry_events) == 1
    assert retry_events[0]["data"]["attempt"] == 1
    assert retry_events[0]["data"]["max_attempts"] == rw._MAX_REPAIR_ATTEMPTS
    assert "chk: check failed" in retry_events[0]["data"]["reason"]

    # The retry event must come between the two verification events.
    types_in_order = [e["type"] for e in doc["trace"]
                       if e["type"] in ("verification", "retry")]
    assert types_in_order == ["verification", "retry", "verification"]


def test_gate_never_passes_writes_retry_for_every_repair_attempt(_isolated):
    tmp_path, fake = _isolated
    _write_build(tmp_path, "wf_broken")
    _write_gate(tmp_path, "wf_broken", script_passes=False)

    before = _existing_session_files()
    asyncio.run(_run("wf_broken"))

    doc = _session_doc(before)
    verif_events = [e for e in doc["trace"] if e["type"] == "verification"]
    retry_events = [e for e in doc["trace"] if e["type"] == "retry"]

    assert len(verif_events) == 1 + rw._MAX_REPAIR_ATTEMPTS
    assert all(e["data"]["passed"] is False for e in verif_events)
    # A retry follows every failing attempt except the last (budget exhausted).
    assert len(retry_events) == rw._MAX_REPAIR_ATTEMPTS
    assert [e["data"]["attempt"] for e in retry_events] == list(
        range(1, rw._MAX_REPAIR_ATTEMPTS + 1)
    )
