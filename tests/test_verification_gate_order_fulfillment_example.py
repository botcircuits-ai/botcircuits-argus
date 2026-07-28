"""End-to-end verification-gate test against the `order_fulfillment` example
under `examples/order_fulfillment/` (see its README + TASK.md).

Unlike the framework-level unit tests (`test_verification_executor.py`,
`test_verification_script_check.py`, `test_verification_judge_check.py`,
`test_build_workflow_gate_generation.py`, `test_run_workflow_repair_loop.py`),
which exercise the verification-gate machinery in isolation with minimal
inline fixtures, this test drives a REAL, documented example workflow through
the REAL build pipeline (condition indexing + gate generation) and the REAL
engine (including a real `listDecision` with exec-backed `itemFacts` making
real HTTP calls to the example's own mock inventory API) — proving the
feature end-to-end against something a user would actually run, not just a
synthetic fixture.

Only the two `agentAction` steps' "LLM calls" are faked (via a scripted
provider) — `decide_lines` itself runs fully deterministically through the
engine's exec path, zero LLM calls, exactly as it would for a real user.

Requires `node` on PATH to run the example's mock API subprocess; skipped
otherwise (this is an example/integration test, not a framework unit test).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from botcircuits.agent.workflow.condition_processor import generate_expressions_and_variables
from botcircuits.agent.workflow.engine.runner import (
    EngineResult,
    SegmentResult,
    run_workflow_engine,
)
from botcircuits.agent.workflow.engine.segments import compute_segments
from botcircuits.agent.workflow.paths import build_dir_for
from botcircuits.agent.workflow.verification import generate_gate, run_gate
from botcircuits.agent.workflow.workflow_defaults import apply_defaults
from botcircuits.agent.workflow.workflow_validator import static_issues

from tests.fakes import ScriptedProvider, text_response

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_DIR = REPO_ROOT / "examples" / "order_fulfillment"
API_PORT = 4200

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="requires `node` to run the example's mock API"
)


# ---------------------------------------------------------------------------
# Mock API subprocess (real HTTP, real `bin/stock_check.py` exec calls)
# ---------------------------------------------------------------------------


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex((host, port)) == 0


@pytest.fixture(scope="module")
def mock_api():
    proc = subprocess.Popen(
        ["node", str(EXAMPLE_DIR / "api" / "server.js")],
        cwd=EXAMPLE_DIR / "api",
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 10
    try:
        while time.time() < deadline and not _port_open(API_PORT):
            time.sleep(0.05)
        assert _port_open(API_PORT), "mock inventory API never came up"
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# Build the example workflow through the REAL pipeline (indexer + gate gen)
# ---------------------------------------------------------------------------

_INDEXER_REPLY = json.dumps({
    "expressions": [
        {"step_id": "decide_lines", "idx": 0, "expCondition": "lookup_failed is true"},
        {"step_id": "decide_lines", "idx": 1, "expCondition": "not_found is true"},
        {"step_id": "decide_lines", "idx": 2, "expCondition": "discontinued is true"},
        {"step_id": "decide_lines", "idx": 3, "expCondition": "in_stock is false"},
        {"step_id": "decide_lines", "idx": 4, "expCondition": "enough_stock is false"},
    ],
    "variables": [],
})

_GATE_REPLY = json.dumps({
    "checks": [
        {
            "id": "totals_match",
            "type": "script",
            "description": "fulfilled_total must equal the sum of fulfilled lines' line_total",
            "severity": "blocking",
            "code": (
                "import json, sys\n"
                "run = json.loads(sys.stdin.read())\n"
                "slots = run.get('slots') or {}\n"
                "results = slots.get('line_results') or []\n"
                "expected = round(sum(\n"
                "    float(r.get('line_total') or 0) for r in results\n"
                "    if r.get('outcome') == 'fulfill'\n"
                "), 2)\n"
                "actual = round(float(slots.get('fulfilled_total') or 0), 2)\n"
                "ok = abs(expected - actual) < 0.01\n"
                "print(json.dumps({\n"
                "    'passed': ok,\n"
                "    'detail': f'expected {expected}, got {actual}',\n"
                "}))\n"
            ),
        },
        {
            "id": "summary_is_plain_english",
            "type": "llm_judge",
            "description": "fulfillment_summary reads as plain customer-facing English",
            "severity": "advisory",
            "rubric": "Pass only if the summary has no SKU codes or HTTP jargon.",
            "inputs": ["slots.fulfillment_summary"],
        },
    ],
})


@pytest.fixture()
def built_flow(tmp_path, monkeypatch):
    """Build examples/order_fulfillment/order_fulfillment.json through the
    real indexer + defaults + segments + gate-generation pipeline, writing
    the gate under an isolated `$BOTCIRCUITS_WORKFLOWS_DIR`. Returns the
    built `flow` dict."""
    monkeypatch.setenv("BOTCIRCUITS_WORKFLOWS_DIR", str(tmp_path))

    source = json.loads((EXAMPLE_DIR / "order_fulfillment.json").read_text())
    assert static_issues(source, base_dir=REPO_ROOT) == []

    flow = source["flow"]
    provider = ScriptedProvider([text_response(_INDEXER_REPLY)])
    asyncio.run(generate_expressions_and_variables(flow, provider))
    apply_defaults(flow)
    flow["segments"] = compute_segments(flow)

    gate_provider = ScriptedProvider([text_response(_GATE_REPLY)])
    manifest = asyncio.run(generate_gate(flow, "order_fulfillment", gate_provider))
    assert len(manifest["checks"]) == 2

    return flow


# ---------------------------------------------------------------------------
# Fake `run_segment` for the two agentAction steps — decide_lines itself
# needs no LLM call (itemFacts exec path).
# ---------------------------------------------------------------------------


def _fake_run_segment_factory(order_path: Path):
    async def run_segment(*, actions, branch_variables, system_notes, slots,
                           item_variables=None, data_variables=None,
                           agent=None, event_sink=None):
        joined = " ".join(actions)
        if "Read the order JSON" in joined:
            order = json.loads(order_path.read_text())
            return SegmentResult(
                text="loaded order", captured_slots={"order_id": order["order_id"]},
            )
        if "fulfillment_summary" in joined or "Sum the fulfill" in joined:
            results = slots.get("line_results") or []
            fulfilled_total = round(
                sum(r.get("line_total") or 0 for r in results
                    if r.get("outcome") == "fulfill"), 2,
            )
            n_fulfill = sum(1 for r in results if r.get("outcome") == "fulfill")
            n_other = len(results) - n_fulfill
            summary = (
                f"We shipped {n_fulfill} item(s) totaling ${fulfilled_total:.2f}. "
                f"{n_other} item(s) are on backorder or could not be filled; "
                "we'll follow up separately on those."
            )
            return SegmentResult(
                text="tallied", captured_slots={
                    "fulfilled_total": fulfilled_total,
                    "fulfillment_summary": summary,
                },
            )
        # write_report — no slots to capture, just acknowledges completion.
        return SegmentResult(text="report written", captured_slots={})

    return run_segment


def _buggy_tally_run_segment_factory(order_path: Path):
    """Same as `_fake_run_segment_factory`, except the tally step reports
    `abs(line_total)` instead of the true (possibly negative) value — the
    class of mistake a real model-driven tally step could make, and exactly
    what the generated `totals_match` check exists to catch."""
    good = _fake_run_segment_factory(order_path)

    async def run_segment(*, actions, branch_variables, system_notes, slots,
                           item_variables=None, data_variables=None,
                           agent=None, event_sink=None):
        joined = " ".join(actions)
        if "fulfillment_summary" in joined or "Sum the fulfill" in joined:
            results = slots.get("line_results") or []
            buggy_total = round(
                sum(abs(r.get("line_total") or 0) for r in results
                    if r.get("outcome") == "fulfill"), 2,
            )
            return SegmentResult(
                text="tallied (buggy)", captured_slots={
                    "fulfilled_total": buggy_total,
                    "fulfillment_summary": "Your order has been processed.",
                },
            )
        return await good(
            actions=actions, branch_variables=branch_variables,
            system_notes=system_notes, slots=slots,
            item_variables=item_variables, data_variables=data_variables,
            agent=agent, event_sink=event_sink,
        )

    return run_segment


async def _resolve_unfilled(**kw):
    return {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _run_workflow(built_flow, order_path: Path) -> EngineResult:
    return asyncio.run(run_workflow_engine(
        built_flow,
        workflow_name="order_fulfillment",
        run_segment=_fake_run_segment_factory(order_path),
        slots={},
        resolve_unfilled=_resolve_unfilled,
    ))


def _run_workflow_with_buggy_tally(built_flow, order_path: Path) -> EngineResult:
    return asyncio.run(run_workflow_engine(
        built_flow,
        workflow_name="order_fulfillment",
        run_segment=_buggy_tally_run_segment_factory(order_path),
        slots={},
        resolve_unfilled=_resolve_unfilled,
    ))


def test_clean_order_passes_the_verification_gate(mock_api, built_flow, tmp_path):
    """The shipped config/order.json resolves every line without a totals
    mismatch — the generated script check should pass, and (with a
    cooperating judge stub) the whole gate should pass."""
    order_path = EXAMPLE_DIR / "config" / "order.json"
    result = _run_workflow(built_flow, order_path)
    assert result.done is True

    line_results = result.slots.get("line_results") or []
    outcomes = {r["sku"]: r.get("outcome") for r in line_results}
    assert outcomes["ok-widget-1"] == "fulfill"
    assert outcomes["low-gadget-2"] == "backorder"     # not enough stock
    assert outcomes["zero-thing-3"] == "backorder"     # zero stock
    assert outcomes["eol-legacy-4"] == "reject"         # discontinued
    assert outcomes["ghost-unknown-5"] == "not_found"   # unknown SKU

    manifest = _rebuild_gate_manifest(built_flow)
    judge_provider = ScriptedProvider([
        text_response(json.dumps({"passed": True, "detail": "reads as plain English"})),
    ])
    run_record = {
        "workflow_name": "order_fulfillment",
        "slots": {k: v for k, v in result.slots.items() if not k.startswith("__")},
        "summary": result.summary,
        "decisions": result.decisions,
        "done": True,
    }
    gate_result = asyncio.run(run_gate(
        manifest, run_record, provider=judge_provider,
        base_dir=build_dir_for("order_fulfillment"),
    ))
    assert gate_result.passed is True
    assert gate_result.blocking_failures() == []


def test_badprice_sku_fails_the_totals_match_check(mock_api, built_flow):
    """A `BADPRICE-…` SKU makes the mock API return a corrupt negative unit
    price. The line still resolves `fulfill` (it has stock), so a fake
    provider that mirrors the real bug (a model that reports the ABSOLUTE
    VALUE of a per-line total instead of the true, negative one — exactly
    the class of mistake a real tally step could make) produces a
    `fulfilled_total` that mismatches the true sum of `line_results` —
    which is precisely the discrepancy the generated `totals_match` script
    check exists to catch. This test drives the SAME `order.json` the
    README documents editing in place (temporarily, restored after), so it
    proves the check catches the failure against the real example's real
    mock API and real `stock_check.py` script, not a synthetic fixture."""
    order_path = EXAMPLE_DIR / "config" / "order.json"
    original = order_path.read_text()
    try:
        order = json.loads(original)
        order["lines"] = [{"sku": "BADPRICE-decoy-1", "qty": 2}]
        order_path.write_text(json.dumps(order))

        result = _run_workflow_with_buggy_tally(built_flow, order_path)
    finally:
        order_path.write_text(original)

    assert result.done is True
    line_results = result.slots.get("line_results") or []
    assert line_results[0]["outcome"] == "fulfill"
    assert line_results[0]["line_total"] < 0  # the corrupt negative price
    # The buggy tally reported the absolute value, so it disagrees with the
    # true (negative) line_total sum — this is the mismatch to catch.
    assert result.slots["fulfilled_total"] != line_results[0]["line_total"]

    manifest = _rebuild_gate_manifest(built_flow)
    # The advisory judge check's own verdict doesn't matter here — even a
    # cooperating "passed" judge must not save the gate from the BLOCKING
    # totals_match failure.
    judge_provider = ScriptedProvider([
        text_response(json.dumps({"passed": True, "detail": "reads fine"})),
    ])
    run_record = {
        "workflow_name": "order_fulfillment",
        "slots": {k: v for k, v in result.slots.items() if not k.startswith("__")},
        "summary": result.summary,
        "decisions": result.decisions,
        "done": True,
    }
    gate_result = asyncio.run(run_gate(
        manifest, run_record, provider=judge_provider,
        base_dir=build_dir_for("order_fulfillment"),
    ))
    assert gate_result.passed is False
    blocking = gate_result.blocking_failures()
    assert len(blocking) == 1
    assert blocking[0].check_id == "totals_match"


def _rebuild_gate_manifest(built_flow) -> dict:
    """Re-read the gate manifest `built_flow`'s fixture already generated
    onto disk (via the `built_flow` fixture's `generate_gate` call)."""
    from botcircuits.agent.workflow.verification.generator import load_gate
    manifest = load_gate("order_fulfillment")
    assert manifest is not None
    return manifest
