"""Unit tests for S3 — the listDecision primitive.

The deterministic heart: given a list of per-item FACTS the model reported, the
engine decides each item via the same `evaluate_choices`. These tests pin that
the engine — not the model — produces the decision word, for every precedence
branch, and that the capture tool extracts the list facts-only.
"""

from __future__ import annotations

from botcircuits.agent.workflow.engine.runner import _decide_list
from botcircuits.agent.workflow.engine.segment_exec import (
    build_record_item_list_tool,
)


def _order_step():
    """An order_fulfillment-shaped listDecision step: facts -> decision word."""
    return {
        "type": "listDecision",
        "itemVariables": [
            {"variableName": "sku", "dataType": "string"},
            {"variableName": "sku_found", "dataType": "boolean"},
            {"variableName": "stock_sufficient", "dataType": "boolean"},
            {"variableName": "line_total", "dataType": "number"},
        ],
        "decisionKey": "decision",
        "emit": ["sku", "decision", "line_total"],
        "collectInto": "decisions",
        "choices": [
            {"operator": "AND", "expressionList": [
                {"variable": "sku_found", "operator": "is", "value": False}],
                "next": "reject"},
            {"operator": "AND", "expressionList": [
                {"variable": "stock_sufficient", "operator": "is", "value": False}],
                "next": "backorder"},
            {"operator": "AND", "expressionList": [
                {"variable": "line_total", "operator": "greater than", "value": 5000}],
                "next": "review"},
        ],
        "next": "fulfill",
    }


def test_engine_decides_each_item_by_precedence():
    step = _order_step()
    items = [
        {"sku": "A", "sku_found": True,  "stock_sufficient": True,  "line_total": 100},   # fulfill
        {"sku": "B", "sku_found": True,  "stock_sufficient": False, "line_total": 200},   # backorder
        {"sku": "C", "sku_found": True,  "stock_sufficient": True,  "line_total": 9000},  # review
        {"sku": "D", "sku_found": False, "stock_sufficient": True,  "line_total": 0},      # reject
    ]
    out = _decide_list("order_fulfillment", step, items)
    assert [d["decision"] for d in out] == ["fulfill", "backorder", "review", "reject"]
    # emit whitelist kept only sku/decision/line_total
    assert set(out[0]) == {"sku", "decision", "line_total"}
    assert out[2]["line_total"] == 9000


def test_default_branch_reads_defaultNext():
    """A listDecision step carries its no-match fallback as `defaultNext`
    (what the builder emits), not `next`. Reading only `next` made every
    default-branch item decide to None — the common fulfill path emitted
    `decision: null` and tanked oracle accuracy. Regression for that."""
    step = _order_step()
    step["defaultNext"] = step.pop("next")  # builder-shaped fallback key
    out = _decide_list("wf", step, [{"sku": "A", "sku_found": True,
                                     "stock_sufficient": True, "line_total": 1}])
    assert out[0]["decision"] == "fulfill"


def test_variable_rhs_comparison_against_slot():
    """A choice can compare a fact against ANOTHER fact via a `{slot}` RHS
    (e.g. `stock < qty`). This is what the order policy's backorder arm needs;
    the indexer now wraps a bare identifier RHS as `{qty}` so it resolves to the
    item's qty slot at eval time instead of comparing against a literal string."""
    step = {
        "type": "listDecision",
        "decisionKey": "decision",
        "collectInto": "decisions",
        "choices": [
            {"operator": "AND", "expressionList": [
                {"variable": "stock", "operator": "less than", "value": "{qty}"}],
                "next": "backorder"},
        ],
        "defaultNext": "fulfill",
    }
    items = [
        {"sku": "A", "stock": 12, "qty": 20},   # 12 < 20 -> backorder
        {"sku": "B", "stock": 500, "qty": 10},  # 500 !< 10 -> fulfill
    ]
    out = _decide_list("wf", step, items)
    assert [d["decision"] for d in out] == ["backorder", "fulfill"]


def test_precedence_order_matters():
    """An unknown SKU that is ALSO over the value limit must reject (first arm
    wins), not review."""
    step = _order_step()
    items = [{"sku": "X", "sku_found": False, "stock_sufficient": True,
              "line_total": 9999}]
    out = _decide_list("wf", step, items)
    assert out[0]["decision"] == "reject"


def test_empty_list_yields_empty():
    assert _decide_list("wf", _order_step(), []) == []


def test_non_dict_items_skipped():
    out = _decide_list("wf", _order_step(),
                       [{"sku": "A", "sku_found": True, "stock_sufficient": True,
                         "line_total": 1}, "garbage", None])
    assert len(out) == 1


def test_keeps_all_fields_when_no_emit_whitelist():
    step = _order_step()
    del step["emit"]
    out = _decide_list("wf", step, [{"sku": "A", "sku_found": True,
                                     "stock_sufficient": True, "line_total": 1}])
    assert "sku_found" in out[0]      # not whitelisted away
    assert out[0]["decision"] == "fulfill"


def test_record_item_list_tool_coerces_stringified_array():
    """Some providers return `items` as a JSON STRING; it must still capture
    (this was a silent decisions:[] failure)."""
    sink: dict = {}
    tool = build_record_item_list_tool(
        [{"variableName": "sku", "dataType": "string"}], sink)
    res = tool.handler({"items": '[{"sku": "A"}, {"sku": "B"}]'})
    assert res["recorded_items"] == 2
    assert sink["items"] == [{"sku": "A"}, {"sku": "B"}]


def test_record_item_list_tool_extracts_facts():
    sink: dict = {}
    tool = build_record_item_list_tool(
        [{"variableName": "sku", "dataType": "string"},
         {"variableName": "sku_found", "dataType": "boolean"}], sink)
    res = tool.handler({"items": [
        {"sku": "A", "sku_found": True},
        {"sku": "B", "sku_found": False},
        "junk",
    ]})
    assert res["recorded_items"] == 2
    assert sink["items"] == [{"sku": "A", "sku_found": True},
                             {"sku": "B", "sku_found": False}]
    # schema advertises an array of objects with the fact fields
    props = tool.input_schema["properties"]["items"]["items"]["properties"]
    assert set(props) == {"sku", "sku_found"}
