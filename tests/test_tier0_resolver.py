"""Unit tests for S4 — Tier-0 deterministic slot resolution."""

from __future__ import annotations

import json
from pathlib import Path

from botcircuits.agent.workflow.engine.tier0_resolver import resolve_tier0


def _order(tmp_path: Path, **fields):
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "current_order.json").write_text(json.dumps(fields))


def test_enum_check_valid(tmp_path: Path):
    _order(tmp_path, customer_id="C1", region="EU", payment_method="card")
    vars_ = [{"variableName": "header_status", "resolver": {
        "kind": "enum_check",
        "source": {"file": "data/current_order.json", "path": "region"},
        "allowed": ["US", "EU", "APAC"], "true": "valid", "false": "invalid"}}]
    out = resolve_tier0(vars_, {}, base_dir=tmp_path)
    assert out == {"header_status": "valid"}


def test_enum_check_invalid(tmp_path: Path):
    _order(tmp_path, customer_id="C1", region="ZZ", payment_method="card")
    vars_ = [{"variableName": "header_status", "resolver": {
        "kind": "enum_check",
        "source": {"file": "data/current_order.json", "path": "region"},
        "allowed": ["US", "EU", "APAC"], "true": "valid", "false": "invalid"}}]
    assert resolve_tier0(vars_, {}, base_dir=tmp_path) == {"header_status": "invalid"}


def test_file_membership_blocked(tmp_path: Path):
    _order(tmp_path, customer_id="CUST-666")
    (tmp_path / "data" / "fraud_blocklist.txt").write_text(
        "# blocked ids\nCUST-666\nCUST-999\n")
    vars_ = [{"variableName": "fraud_status", "resolver": {
        "kind": "file_membership", "file": "data/fraud_blocklist.txt",
        "value_source": {"file": "data/current_order.json", "path": "customer_id"},
        "true": "blocked", "false": "clear", "ignore_comments": True}}]
    assert resolve_tier0(vars_, {}, base_dir=tmp_path) == {"fraud_status": "blocked"}


def test_file_membership_clear(tmp_path: Path):
    _order(tmp_path, customer_id="CUST-100")
    (tmp_path / "data" / "fraud_blocklist.txt").write_text("# ids\nCUST-666\n")
    vars_ = [{"variableName": "fraud_status", "resolver": {
        "kind": "file_membership", "file": "data/fraud_blocklist.txt",
        "value_source": {"file": "data/current_order.json", "path": "customer_id"},
        "true": "blocked", "false": "clear", "ignore_comments": True}}]
    assert resolve_tier0(vars_, {}, base_dir=tmp_path) == {"fraud_status": "clear"}


def test_range_check(tmp_path: Path):
    vars_ = [{"variableName": "in_range", "resolver": {
        "kind": "range", "source": {"slot": "total"}, "max": 5000,
        "true": "in", "false": "out"}}]
    assert resolve_tier0(vars_, {"total": 4000}, base_dir=tmp_path) == {"in_range": "in"}
    assert resolve_tier0(vars_, {"total": 6000}, base_dir=tmp_path) == {"in_range": "out"}


def test_all_or_nothing_one_unresolved_returns_none(tmp_path: Path):
    _order(tmp_path, region="EU")
    vars_ = [
        {"variableName": "header_status", "resolver": {
            "kind": "enum_check",
            "source": {"file": "data/current_order.json", "path": "region"},
            "allowed": ["US", "EU"], "true": "valid", "false": "invalid"}},
        # second var has NO resolver -> whole resolve must return None
        {"variableName": "fraud_status"},
    ]
    assert resolve_tier0(vars_, {}, base_dir=tmp_path) is None


def test_missing_file_degrades_to_none(tmp_path: Path):
    vars_ = [{"variableName": "x", "resolver": {
        "kind": "jsonpath", "file": "data/nope.json", "path": "a"}}]
    assert resolve_tier0(vars_, {}, base_dir=tmp_path) is None


def test_empty_variables_returns_none():
    assert resolve_tier0([], {}) is None
