"""Unit tests for S4-exec — engine-side deterministic per-item fact gathering."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from botcircuits.agent.workflow.engine.item_resolver import resolve_item_facts
from botcircuits.agent.workflow.engine.runner import _decide_list


def _pricer(tmp_path: Path):
    """A tiny stand-in for bin/price.py: prints the same JSON shape."""
    (tmp_path / "bin").mkdir(exist_ok=True)
    script = tmp_path / "bin" / "price.py"
    script.write_text(
        "import sys, json\n"
        "inv={'SKU-A':{'price':10,'stock':100},'SKU-B':{'price':9000,'stock':100}}\n"
        "sku=sys.argv[1]; qty=int(sys.argv[2])\n"
        "rec=inv.get(sku)\n"
        "print(json.dumps({'found':False}) if not rec else "
        "json.dumps({'found':True,'stock':rec['stock'],"
        "'line_total':rec['price']*qty}))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _step():
    return {
        "type": "listDecision",
        "itemSource": {"file": "data/order.json", "path": "items"},
        "itemFacts": {
            "kind": "exec",
            "command": ["python3", "bin/price.py", "{sku}", "{qty}"],
            "parse": "json",
            "derive": {
                "sku": {"from_item": "sku"},
                "sku_found": {"from_output": "found"},
                "line_total": {"from_output": "line_total", "default": 0},
                "stock_sufficient": {"ge": ["output.stock", "item.qty"]},
            },
        },
        "decisionKey": "decision",
        "emit": ["sku", "decision", "line_total"],
        "nullOn": {"line_total": ["reject"]},
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


def test_engine_gathers_facts_and_decides_end_to_end(tmp_path: Path):
    _pricer(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "order.json").write_text(json.dumps({"items": [
        {"sku": "SKU-A", "qty": 5},    # in stock, cheap -> fulfill
        {"sku": "SKU-B", "qty": 1},    # line_total 9000 > 5000 -> review
        {"sku": "SKU-A", "qty": 200},  # qty 200 > stock 100 -> backorder
        {"sku": "SKU-X", "qty": 1},    # unknown -> reject
    ]}))
    facts = resolve_item_facts(_step(), base_dir=tmp_path)
    assert facts is not None
    decided = _decide_list("wf", _step(), facts)
    assert [d["decision"] for d in decided] == \
        ["fulfill", "review", "backorder", "reject"]
    # reject line_total nulled
    assert decided[3]["line_total"] is None
    # line_total carried for non-reject
    assert decided[1]["line_total"] == 9000


def test_no_itemfacts_no_source_returns_none(tmp_path: Path):
    """With neither an exec `itemFacts` nor a readable `itemSource`, there's
    nothing to resolve deterministically -> None (caller runs the model)."""
    step = _step()
    del step["itemFacts"]
    del step["itemSource"]
    assert resolve_item_facts(step, base_dir=tmp_path) is None


def test_itemsource_without_itemfacts_projects_fields(tmp_path: Path):
    """A listDecision step with an `itemSource` but NO exec `itemFacts` (e.g. the
    fraud-reject path, which rejects every line regardless of price) must still
    read the items from the file and project their declared `itemVariables` —
    NOT fall back to the model, which hallucinated a single `UNKNOWN` item."""
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "order.json").write_text(json.dumps({"items": [
        {"sku": "SKU-CABLE", "qty": 5},
        {"sku": "SKU-PHONE", "qty": 2},
    ]}))
    step = {
        "type": "listDecision",
        "itemSource": {"file": "data/order.json", "path": "items"},
        "itemVariables": [
            {"variableName": "sku"},
            {"variableName": "line_total"},
        ],
        "decisionKey": "decision",
        "collectInto": "decisions",
        "emit": ["sku", "line_total", "decision"],
        "nullOn": {"line_total": ["reject"]},
        "choices": [],            # no condition fires -> defaultNext
        "defaultNext": "reject",
    }
    facts = resolve_item_facts(step, base_dir=tmp_path)
    assert facts == [
        {"sku": "SKU-CABLE", "line_total": None},
        {"sku": "SKU-PHONE", "line_total": None},
    ]
    decided = _decide_list("wf", step, facts)
    assert [(d["sku"], d["decision"], d["line_total"]) for d in decided] == [
        ("SKU-CABLE", "reject", None),
        ("SKU-PHONE", "reject", None),
    ]


def test_plain_text_item_source_one_per_line(tmp_path: Path):
    """A non-JSON `itemSource` file is read as one item per line (the SKILL's
    documented `path: ""` plain-text source). Each line becomes an item dict
    under `value`, so an exec `command` can interpolate `{value}` — without this
    a .txt source returned None and the listDecision fell back to the model."""
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "look.py").write_text(
        "import sys, json\n"
        "print(json.dumps({'status': 'delivered' if sys.argv[1].startswith('DLV') "
        "else 'in transit'}))\n"
    )
    (tmp_path / "ids.txt").write_text("DLV0001\n  \nTRN0003\n")  # blank line ignored
    step = {
        "type": "listDecision",
        "itemSource": {"file": "ids.txt", "path": ""},
        "itemFacts": {
            "kind": "exec",
            "command": ["python3", "bin/look.py", "{value}"],
            "parse": "json",
            "derive": {
                "tracking_number": {"from_item": "value"},
                "status": {"from_output": "status"},
            },
        },
        "decisionKey": "decision",
    }
    facts = resolve_item_facts(step, base_dir=tmp_path)
    assert facts == [
        {"tracking_number": "DLV0001", "status": "delivered"},
        {"tracking_number": "TRN0003", "status": "in transit"},
    ]


def test_missing_order_file_returns_none(tmp_path: Path):
    _pricer(tmp_path)
    assert resolve_item_facts(_step(), base_dir=tmp_path) is None


def test_on_exec_reports_each_subprocess(tmp_path: Path):
    """`on_exec` fires once per priced item with (argv, stdout, rc, is_error) —
    this is what lets the engine surface the pricer runs as tool calls so Tool
    Correctness isn't blind to engine-driven execs."""
    _pricer(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "order.json").write_text(json.dumps({"items": [
        {"sku": "SKU-A", "qty": 5},
        {"sku": "SKU-B", "qty": 1},
    ]}))
    seen: list[tuple] = []
    facts = resolve_item_facts(
        _step(), base_dir=tmp_path,
        on_exec=lambda argv, out, rc, err: seen.append((argv, out, rc, err)),
    )
    assert facts is not None and len(seen) == 2
    # argv interpolated per item; output is the pricer's JSON; clean exit.
    assert seen[0][0] == ["python3", "bin/price.py", "SKU-A", "5"]
    assert "found" in seen[0][1]          # captured stdout
    assert seen[0][2] == 0 and seen[0][3] is False
