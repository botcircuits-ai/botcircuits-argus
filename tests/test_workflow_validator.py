"""Unit tests for the generated-workflow static validator."""

from __future__ import annotations

import json
from pathlib import Path

from botcircuits.agent.workflow.workflow_validator import static_issues


def _ws(tmp_path: Path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "order.json").write_text(json.dumps(
        {"customer_id": "C1", "items": [{"sku": "A", "qty": 1}]}))
    return tmp_path


def _good(tmp_path):
    return {
        "name": "wf", "description": "Process an order.",
        "flow": {
            "start": "process",
            "variables": [],
            "steps": {
                "process": {
                    "type": "listDecision",
                    "settings": {"action": "price items"},
                    "itemSource": {"file": "data/order.json", "path": "items"},
                    "itemVariables": [{"variableName": "sku"}],
                    "conditions": [{"condition": "unknown sku", "next": "reject"}],
                    "next": "fulfill",
                }
            },
        },
    }


def test_clean_workflow_has_no_issues(tmp_path):
    _ws(tmp_path)
    assert static_issues(_good(tmp_path), base_dir=tmp_path) == []


def test_missing_description_flagged(tmp_path):
    _ws(tmp_path)
    doc = _good(tmp_path); doc["description"] = ""
    assert any("description" in i for i in static_issues(doc, base_dir=tmp_path))


def test_dict_itemvariables_flagged(tmp_path):
    _ws(tmp_path)
    doc = _good(tmp_path)
    doc["flow"]["steps"]["process"]["itemVariables"] = {"sku": {"from_item": "sku"}}
    assert any("must be a LIST" in i for i in static_issues(doc, base_dir=tmp_path))


def test_bad_itemsource_path_flagged(tmp_path):
    _ws(tmp_path)
    doc = _good(tmp_path)
    doc["flow"]["steps"]["process"]["itemSource"]["path"] = "line_items"  # wrong key
    assert any("not a list" in i for i in static_issues(doc, base_dir=tmp_path))


def test_missing_itemsource_flagged(tmp_path):
    _ws(tmp_path)
    doc = _good(tmp_path)
    del doc["flow"]["steps"]["process"]["itemSource"]
    assert any("itemSource" in i for i in static_issues(doc, base_dir=tmp_path))


def test_question_step_flagged(tmp_path):
    _ws(tmp_path)
    doc = _good(tmp_path)
    doc["flow"]["steps"]["ask"] = {"type": "question",
                                   "settings": {"action": "ask the user"}}
    assert any("question" in i and "ask" in i
               for i in static_issues(doc, base_dir=tmp_path))


def test_resolver_missing_file_flagged(tmp_path):
    _ws(tmp_path)
    doc = _good(tmp_path)
    doc["flow"]["variables"] = [{
        "variableName": "blocked",
        "resolver": {"kind": "file_membership", "file": "data/nope.txt",
                     "value_source": {"file": "data/order.json", "path": "customer_id"}},
    }]
    assert any("does not exist" in i for i in static_issues(doc, base_dir=tmp_path))


def test_step_name_as_outcome_label_flagged(tmp_path):
    _ws(tmp_path)
    doc = _good(tmp_path)
    doc["flow"]["steps"]["emit"] = {"type": "agentAction", "settings": {"action": "x"}}
    doc["flow"]["steps"]["process"]["next"] = "emit"  # step name as default outcome
    assert any("STEP" in i for i in static_issues(doc, base_dir=tmp_path))


def test_malformed_is_comparison_flagged(tmp_path):
    _ws(tmp_path)
    doc = _good(tmp_path)
    doc["flow"]["steps"]["process"]["choices"] = [
        {"expressionList": [{"variable": "stock", "operator": "is",
                             "value": "less than qty"}], "next": "backorder"}]
    assert any("never" in i.lower() and "match" in i.lower()
               for i in static_issues(doc, base_dir=tmp_path))


def test_or_condition_flagged(tmp_path):
    """A condition joining alternatives with 'or' compiles to a single branch
    and silently drops the rest — flag it so the author splits it."""
    _ws(tmp_path)
    doc = _good(tmp_path)
    doc["flow"]["steps"]["process"]["conditions"] = [
        {"condition": "status is exception or returned or lost", "next": "escalate"},
    ]
    issues = static_issues(doc, base_dir=tmp_path)
    assert any("'or'" in i and "drops" in i for i in issues)


def test_split_conditions_not_flagged(tmp_path):
    """The correct split form (one alternative per entry) raises no OR warning."""
    _ws(tmp_path)
    doc = _good(tmp_path)
    doc["flow"]["steps"]["process"]["conditions"] = [
        {"condition": "status is exception", "next": "escalate"},
        {"condition": "status is returned", "next": "escalate"},
        {"condition": "status is lost", "next": "escalate"},
    ]
    assert not any("drops" in i for i in static_issues(doc, base_dir=tmp_path))


def test_no_basedir_skips_file_checks(tmp_path):
    # Without base_dir, path-existence checks are skipped (no false positives).
    doc = _good(tmp_path)
    doc["flow"]["steps"]["process"]["itemSource"]["path"] = "line_items"
    issues = static_issues(doc, base_dir=None)
    assert not any("not a list" in i for i in issues)


def test_unknown_agent_reference_flagged(tmp_path):
    _ws(tmp_path)
    doc = _good(tmp_path)
    doc["flow"]["steps"]["process"]["agent"] = "researcher"
    issues = static_issues(doc, base_dir=tmp_path)
    assert any("unknown agent 'researcher'" in i for i in issues)


def test_declared_agent_reference_not_flagged(tmp_path):
    _ws(tmp_path)
    doc = _good(tmp_path)
    doc["agents"] = {"researcher": {"model": "claude-haiku-4-5"}}
    doc["flow"]["steps"]["process"]["agent"] = "researcher"
    issues = static_issues(doc, base_dir=tmp_path)
    assert not any("unknown agent" in i for i in issues)


def test_unknown_agent_runtime_flagged(tmp_path):
    _ws(tmp_path)
    doc = _good(tmp_path)
    doc["agents"] = {"researcher": {"runtime": "claude-cod"}}  # typo
    issues = static_issues(doc, base_dir=tmp_path)
    assert any("runtime" in i and "claude-cod" in i for i in issues)


def test_unknown_agent_provider_flagged(tmp_path):
    _ws(tmp_path)
    doc = _good(tmp_path)
    doc["agents"] = {"researcher": {"provider": "openia"}}  # typo
    issues = static_issues(doc, base_dir=tmp_path)
    assert any("provider" in i and "openia" in i for i in issues)


def test_valid_agent_runtime_and_provider_not_flagged(tmp_path):
    _ws(tmp_path)
    doc = _good(tmp_path)
    doc["agents"] = {"researcher": {"runtime": "codex", "provider": "openai",
                                     "model": "gpt-4.1"}}
    issues = static_issues(doc, base_dir=tmp_path)
    assert not any("runtime" in i or "provider" in i for i in issues)


def test_openrouter_provider_not_flagged(tmp_path):
    _ws(tmp_path)
    doc = _good(tmp_path)
    doc["agents"] = {"researcher": {"provider": "openrouter",
                                     "model": "anthropic/claude-3.7-sonnet"}}
    issues = static_issues(doc, base_dir=tmp_path)
    assert not any("provider" in i for i in issues)


def test_agent_pinned_to_self_runtime_flagged(tmp_path):
    _ws(tmp_path)
    doc = _good(tmp_path)
    doc["agents"] = {"researcher": {"runtime": "self"}}
    issues = static_issues(doc, base_dir=tmp_path)
    assert any("runtime is 'self'" in i for i in issues)
