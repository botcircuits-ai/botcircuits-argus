"""`verification/executor.py::run_gate` — the ONE fixed entry point every
workflow's gate runs through. These tests exercise the framework itself
(dispatch, blocking-vs-advisory aggregation, provider requirement) with
inline script/judge checks standing in for generated ones — the
executor must not care where the checks came from."""

from __future__ import annotations

import asyncio
import textwrap

import pytest

from tests.fakes import ScriptedProvider, text_response

from botcircuits.agent.workflow.verification import run_gate
from botcircuits.agent.workflow.verification.types import VerificationError


def _write_script(tmp_path, name: str, passed: bool):
    path = tmp_path / name
    path.write_text(textwrap.dedent(f"""\
        import json
        print(json.dumps({{"passed": {passed}, "detail": "d"}}))
    """), encoding="utf-8")
    return path


def test_one_blocking_fail_and_one_advisory_fail_fails_the_gate_but_isolates_blocking(tmp_path):
    _write_script(tmp_path, "fail.py", passed=False)
    gate_spec = {
        "workflow_name": "wf",
        "checks": [
            {"id": "a", "type": "script", "script": "fail.py", "severity": "blocking"},
            {"id": "b", "type": "script", "script": "fail.py", "severity": "advisory"},
        ],
    }
    result = asyncio.run(run_gate(gate_spec, {"slots": {}}, base_dir=tmp_path))
    assert result.passed is False
    assert [c.check_id for c in result.blocking_failures()] == ["a"]


def test_all_advisory_failing_still_passes_the_gate(tmp_path):
    _write_script(tmp_path, "fail.py", passed=False)
    gate_spec = {
        "workflow_name": "wf",
        "checks": [
            {"id": "a", "type": "script", "script": "fail.py", "severity": "advisory"},
        ],
    }
    result = asyncio.run(run_gate(gate_spec, {"slots": {}}, base_dir=tmp_path))
    assert result.passed is True
    assert result.blocking_failures() == []


def test_llm_judge_check_without_provider_raises_immediately(tmp_path):
    gate_spec = {
        "workflow_name": "wf",
        "checks": [
            {"id": "j", "type": "llm_judge", "rubric": "r", "inputs": [],
             "severity": "blocking"},
        ],
    }
    with pytest.raises(VerificationError):
        asyncio.run(run_gate(gate_spec, {"slots": {}}, provider=None, base_dir=tmp_path))


def test_mixed_script_and_judge_checks_both_run(tmp_path):
    _write_script(tmp_path, "pass.py", passed=True)
    provider = ScriptedProvider([text_response('{"passed": true, "detail": "ok"}')])
    gate_spec = {
        "workflow_name": "wf",
        "checks": [
            {"id": "a", "type": "script", "script": "pass.py", "severity": "blocking"},
            {"id": "b", "type": "llm_judge", "rubric": "r", "inputs": [],
             "severity": "advisory"},
        ],
    }
    result = asyncio.run(run_gate(
        gate_spec, {"slots": {}}, provider=provider, base_dir=tmp_path,
    ))
    assert result.passed is True
    assert {c.check_id for c in result.checks} == {"a", "b"}


def test_unknown_check_type_is_a_blocking_error(tmp_path):
    gate_spec = {
        "workflow_name": "wf",
        "checks": [{"id": "x", "type": "regex_match", "severity": "advisory"}],
    }
    result = asyncio.run(run_gate(gate_spec, {"slots": {}}, base_dir=tmp_path))
    assert result.passed is False
    assert result.checks[0].error is not None
