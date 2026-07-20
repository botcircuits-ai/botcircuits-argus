"""Coding-evaluation harness: dataset loading, objective scorer, sandbox
isolation, judge parsing, and metric aggregation.

No live model runs here — the agent modes are exercised by the smoke path
(run manually via `workflow eval-coding`); these tests pin the deterministic
pieces the comparison depends on.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from botcircuits.agent.workflow.evaluation.coding_dataset import (
    discover_coding_datasets,
    load_coding_dataset,
    fixture_path,
)
from botcircuits.agent.workflow.evaluation.coding_scorers import (
    ObjectiveScore,
    _extract_score,
    run_objective,
)
from botcircuits.agent.workflow.evaluation.coding_metrics import (
    _case_consistency,
    summarize_mode,
)
from botcircuits.agent.workflow.evaluation.coding_runner import (
    CodingRunResult,
    _make_sandbox,
    _capture_diff,
    _cleanup_sandbox,
)
from botcircuits.agent.workflow.evaluation.coding_scorers import JudgeScore


# ---- dataset loading ------------------------------------------------------


def _write_dataset(tmp_path: Path) -> Path:
    """A minimal coding dataset + one fixture repo under tmp_path/coding/."""
    coding = tmp_path / "coding"
    fx = coding / "fixtures" / "mini"
    (fx / "tests").mkdir(parents=True)
    (fx / "app.py").write_text("def inc(n):\n    return n  # BUG: should be n+1\n")
    (fx / "tests" / "test_app.py").write_text(textwrap.dedent("""\
        import os, sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
        from app import inc
        def test_inc():
            assert inc(1) == 2
    """))
    ds = coding / "d.json"
    ds.write_text(textwrap.dedent("""\
        {"name": "d", "cases": [
          {"id": "d.fix", "fixture": "mini",
           "prompt": "fix inc", "goal": "inc(1)==2",
           "test_command": "python -m pytest -q tests/test_app.py"}
        ]}
    """))
    return ds


def test_load_and_discover(tmp_path):
    ds_path = _write_dataset(tmp_path)
    ds = load_coding_dataset(ds_path)
    assert ds.name == "d"
    assert len(ds.cases) == 1
    c = ds.cases[0]
    assert c.id == "d.fix" and c.fixture == "mini"
    assert fixture_path(ds, c).is_dir()

    found = discover_coding_datasets(tmp_path / "coding")
    assert [d.name for d in found] == ["d"]


# ---- objective scorer -----------------------------------------------------


def test_objective_red_then_green(tmp_path):
    ds = load_coding_dataset(_write_dataset(tmp_path))
    case = ds.cases[0]
    src = fixture_path(ds, case)

    # Copy fixture into a sandbox and score it — the bug makes it RED.
    sandbox = _make_sandbox(src)
    try:
        red = asyncio.run(run_objective(
            sandbox, test_command=case.test_command, guard_command=None))
        assert red.tests_pass is False

        # Apply the fix and re-score — GREEN.
        (sandbox / "app.py").write_text("def inc(n):\n    return n + 1\n")
        green = asyncio.run(run_objective(
            sandbox, test_command=case.test_command, guard_command=None))
        assert green.tests_pass is True
    finally:
        _cleanup_sandbox(sandbox)


def test_objective_missing_binary_is_error(tmp_path):
    ds = load_coding_dataset(_write_dataset(tmp_path))
    sandbox = _make_sandbox(fixture_path(ds, ds.cases[0]))
    try:
        score = asyncio.run(run_objective(
            sandbox, test_command="definitely_not_a_real_binary_xyz",
            guard_command=None))
        assert score.tests_pass is False
        assert score.error is not None
    finally:
        _cleanup_sandbox(sandbox)


# ---- sandbox isolation ----------------------------------------------------


def test_sandboxes_are_isolated(tmp_path):
    ds = load_coding_dataset(_write_dataset(tmp_path))
    src = fixture_path(ds, ds.cases[0])
    a = _make_sandbox(src)
    b = _make_sandbox(src)
    try:
        (a / "scratch.txt").write_text("only in A")
        assert not (b / "scratch.txt").exists()
        assert a.parent != b.parent
        # The original fixture is untouched by either sandbox.
        assert not (src / "scratch.txt").exists()
    finally:
        _cleanup_sandbox(a)
        _cleanup_sandbox(b)


def test_diff_capture(tmp_path):
    ds = load_coding_dataset(_write_dataset(tmp_path))
    sandbox = _make_sandbox(fixture_path(ds, ds.cases[0]))
    try:
        assert _capture_diff(sandbox) == ""  # unchanged seed
        (sandbox / "app.py").write_text("def inc(n):\n    return n + 1\n")
        diff = _capture_diff(sandbox)
        assert "n + 1" in diff
    finally:
        _cleanup_sandbox(sandbox)


# ---- judge parsing --------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ('{"score": 0.8, "reason": "good"}', 0.8),
    ('here you go: {"score": 1.0, "reason": "perfect"} done', 1.0),
    ('```json\n{"score": 0.0, "reason": "no"}\n```', 0.0),
    ('{"score": 5, "reason": "over"}', 1.0),   # clamped
    ('{"score": -2}', 0.0),                     # clamped
    ('not json at all', 0.0),                   # graceful
])
def test_extract_score(text, expected):
    score, _reason = _extract_score(text)
    assert score == expected


# ---- metric aggregation ---------------------------------------------------


def test_case_consistency():
    assert _case_consistency([True, True, True]) == 1.0
    assert _case_consistency([False, False, False]) == 1.0
    assert _case_consistency([True, True, False]) == pytest.approx(2 / 3)
    assert _case_consistency([]) == 0.0


def _run(mode, case_id, tests_pass, no_reg=None, judge=None, in_tok=100,
         calls=3, elapsed=1.0):
    r = CodingRunResult(case_id=case_id, fixture="f", mode=mode)
    r.objective = ObjectiveScore(tests_pass=tests_pass, no_regressions=no_reg)
    if judge is not None:
        r.judge = JudgeScore(score=judge)
    r.input_tokens = in_tok
    r.llm_calls = calls
    r.elapsed_s = elapsed
    return r


def test_summarize_mode():
    runs = [
        _run("pipeline", "c1", True, no_reg=True, judge=0.9),
        _run("pipeline", "c1", True, no_reg=True, judge=0.8),
        _run("pipeline", "c2", False, no_reg=True, judge=0.2),
    ]
    s = summarize_mode("pipeline", runs, case_ids=["c1", "c2"], repeats=2)
    assert s.runs == 3
    assert s.tests_pass_rate == pytest.approx(2 / 3)
    assert s.no_regression_rate == 1.0
    assert s.judge_mean == pytest.approx((0.9 + 0.8 + 0.2) / 3)
    # c1: [True, True] -> 1.0 ; c2: [False] -> 1.0 ; mean 1.0
    assert s.consistency_mean == 1.0
    assert s.avg_input_tokens == 100


def test_summarize_mode_empty():
    s = summarize_mode("native", [], case_ids=["c1"], repeats=3)
    assert s.runs == 0 and s.tests_pass_rate == 0.0


# ---- report rendering -----------------------------------------------------


def test_render_three_column_report():
    from botcircuits.agent.workflow.evaluation.coding_metrics import CodingReport
    from botcircuits.agent.workflow.evaluation.coding_report import (
        render_coding_report,
    )

    report = CodingReport(dataset="d", repeats=1)
    for mode in ("pipeline", "native", "claude_cli"):
        report.mode_summaries[mode] = summarize_mode(
            mode, [_run(mode, "c1", mode != "native")],
            case_ids=["c1"], repeats=1)
        report.per_run.append(
            _run(mode, "c1", mode != "native").to_dict())
    text = render_coding_report(report)
    # All three columns present, plus the headline metric rows.
    assert "pipeline" in text and "native" in text and "claude-cli" in text
    assert "tests pass" in text and "judge" in text
    assert "c1" in text  # per-case grid
