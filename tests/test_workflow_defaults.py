"""Unit tests for build-time defaults inference (intent-only authoring)."""

from __future__ import annotations

from botcircuits.agent.workflow.workflow_defaults import apply_defaults


def test_deterministic_flag_when_all_branch_vars_resolve():
    flow = {
        "variables": [
            {"variableName": "header_status", "resolver": {"kind": "enum_check"}},
        ],
        "steps": {
            "validate": {
                "type": "agentAction",
                "choices": [{"expressionList": [
                    {"variable": "header_status", "operator": "is", "value": "invalid"}]}],
                "next": "x",
            }
        },
    }
    apply_defaults(flow)
    assert flow["steps"]["validate"]["deterministic"] is True


def test_no_deterministic_when_a_branch_var_lacks_resolver():
    flow = {
        "variables": [{"variableName": "status"}],  # no resolver
        "steps": {"s": {"type": "agentAction", "choices": [{"expressionList": [
            {"variable": "status", "operator": "is", "value": "x"}]}]}},
    }
    apply_defaults(flow)
    assert "deterministic" not in flow["steps"]["s"]


def test_author_deterministic_flag_is_respected():
    flow = {
        "variables": [{"variableName": "a", "resolver": {"kind": "enum_check"}}],
        "steps": {"s": {"type": "agentAction", "deterministic": False,
                        "choices": [{"expressionList": [
                            {"variable": "a", "operator": "is", "value": "x"}]}]}},
    }
    apply_defaults(flow)
    assert flow["steps"]["s"]["deterministic"] is False  # author wins


def test_listdecision_defaults_filled():
    flow = {
        "variables": [],
        "steps": {"items": {
            "type": "listDecision",
            "itemVariables": [
                {"variableName": "sku"}, {"variableName": "sku_found"}],
            "choices": [{"expressionList": [
                {"variable": "sku_found", "operator": "is", "value": False}],
                "next": "reject"}],
            "next": "fulfill",
        }},
    }
    apply_defaults(flow)
    s = flow["steps"]["items"]
    assert s["decisionKey"] == "decision"
    assert s["collectInto"] == "decisions"
    assert s["emit"] == ["sku", "sku_found", "decision"]
    assert s["nullOn"] == {}


def test_listdecision_author_overrides_kept():
    flow = {"variables": [], "steps": {"items": {
        "type": "listDecision", "itemVariables": [{"variableName": "sku"}],
        "decisionKey": "outcome", "collectInto": "results",
        "emit": ["sku"], "nullOn": {"price": ["reject"]},
        "choices": [], "next": "x"}}}
    apply_defaults(flow)
    s = flow["steps"]["items"]
    assert s["decisionKey"] == "outcome"
    assert s["collectInto"] == "results"
    assert s["emit"] == ["sku"]
    assert s["nullOn"] == {"price": ["reject"]}


def test_result_default_from_collected_list_and_customer_id():
    flow = {
        "variables": [{"variableName": "customer_id"}],
        "steps": {"items": {"type": "listDecision",
                            "itemVariables": [{"variableName": "sku"}],
                            "collectInto": "decisions",
                            "choices": [], "next": "x"}},
    }
    apply_defaults(flow)
    assert flow["result"] == {"kind": "template", "value": {
        "customer": "{customer_id}", "decisions": "{decisions}"}}


def test_author_result_is_respected():
    flow = {"variables": [], "result": {"kind": "from_file", "path": "x.json"},
            "steps": {"items": {"type": "listDecision",
                                "itemVariables": [], "collectInto": "decisions",
                                "choices": [], "next": "x"}}}
    apply_defaults(flow)
    assert flow["result"]["kind"] == "from_file"


def test_dtype_upgrade_from_resolver():
    flow = {"variables": [
        {"variableName": "s", "dataType": "string",
         "resolver": {"kind": "enum_check"}}], "steps": {}}
    apply_defaults(flow)
    assert flow["variables"][0]["dataType"] == "string"  # enum stays string
