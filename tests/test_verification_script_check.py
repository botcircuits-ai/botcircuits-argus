"""Deterministic `type: "script"` check execution
(`verification/script_check.py`) — the sandboxed-subprocess contract:
stdin gets the run record as JSON, stdout must be one line of
`{"passed": bool, "detail": str}`."""

from __future__ import annotations

import asyncio
import textwrap

import pytest

from botcircuits.agent.workflow.verification import script_check


def _write_script(tmp_path, name: str, body: str):
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def _check(script_path, **overrides) -> dict:
    check = {
        "id": "chk",
        "type": "script",
        "script": script_path.name,
        "severity": "blocking",
    }
    check.update(overrides)
    return check


def test_passing_script_returns_passed_true(tmp_path):
    script = _write_script(tmp_path, "ok.py", """\
        import json, sys
        run = json.loads(sys.stdin.read())
        print(json.dumps({"passed": True, "detail": f"slots={run['slots']}"}))
    """)
    result = asyncio.run(script_check.run_one(
        _check(script), {"workflow_name": "wf", "slots": {"a": 1},
                          "summary": "", "decisions": [], "done": True},
        base_dir=tmp_path,
    ))
    assert result.passed is True
    assert result.error is None
    assert "slots=" in result.detail


def test_nonzero_exit_is_blocking_error_regardless_of_severity(tmp_path):
    script = _write_script(tmp_path, "bad.py", """\
        import sys
        print("boom", file=sys.stderr)
        sys.exit(1)
    """)
    result = asyncio.run(script_check.run_one(
        _check(script, severity="advisory"), {"slots": {}}, base_dir=tmp_path,
    ))
    assert result.passed is False
    assert result.error is not None
    assert "boom" in result.error
    assert result.is_blocking_failure is True  # error always blocks, despite advisory


def test_timeout_is_killed_and_reported(tmp_path):
    script = _write_script(tmp_path, "slow.py", """\
        import time
        time.sleep(5)
    """)
    result = asyncio.run(script_check.run_one(
        _check(script, timeout_seconds=0.2), {"slots": {}}, base_dir=tmp_path,
    ))
    assert result.passed is False
    assert result.error is not None
    assert "timed out" in result.error


def test_non_json_stdout_is_parse_error(tmp_path):
    script = _write_script(tmp_path, "garbage.py", """\
        print("not json")
    """)
    result = asyncio.run(script_check.run_one(
        _check(script), {"slots": {}}, base_dir=tmp_path,
    ))
    assert result.passed is False
    assert result.error is not None


def test_missing_script_file_is_an_error(tmp_path):
    result = asyncio.run(script_check.run_one(
        _check(tmp_path / "nope.py"), {"slots": {}}, base_dir=tmp_path,
    ))
    assert result.passed is False
    assert "not found" in result.error
