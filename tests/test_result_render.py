"""Unit tests for S2 — engine-rendered final answer (result_render)."""

from __future__ import annotations

import json
from pathlib import Path

from botcircuits.agent.workflow.engine.result_render import (
    render_result,
    result_summary_line,
)


def test_no_result_spec_returns_none():
    assert render_result({}, {"a": 1}) is None
    assert render_result({"result": "notadict"}, {}) is None


def test_from_file(tmp_path: Path):
    payload = {"customer": "C1", "decisions": [{"sku": "X", "decision": "fulfill"}]}
    (tmp_path / "decisions.json").write_text(json.dumps(payload))
    flow = {"result": {"kind": "from_file", "path": "decisions.json"}}
    assert render_result(flow, {}, base_dir=tmp_path) == payload


def test_from_file_missing_degrades_to_none(tmp_path: Path):
    flow = {"result": {"kind": "from_file", "path": "nope.json"}}
    assert render_result(flow, {}, base_dir=tmp_path) is None


def test_template_interpolates_slots():
    flow = {"result": {"kind": "template",
                       "value": {"customer": "{customer_id}", "ok": True,
                                 "items": ["{region}"]}}}
    out = render_result(flow, {"customer_id": "C9", "region": "EU"})
    assert out == {"customer": "C9", "ok": True, "items": ["EU"]}


def test_slots_shorthand():
    flow = {"result": {"kind": "slots", "keys": ["a", "b"]}}
    assert render_result(flow, {"a": 1, "b": 2, "c": 3}) == {"a": 1, "b": 2}


def test_summary_line_carries_compact_json():
    line = result_summary_line("order_fulfillment",
                               {"customer": "C1", "decisions": []})
    assert "order_fulfillment completed:" in line
    assert '"customer":"C1"' in line  # compact, parseable
