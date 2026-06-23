"""Regression tests for ordered comparisons in the choice handler.

The loan_triage eval exposed `TypeError: '<=' not supported between
'NoneType'/'str' and 'int'` when a branch condition compared a numeric literal
against a slot that arrived unfilled (`None`/`""`) or as a string (`"640"`).
Ordered comparisons must coerce both sides via `to_number` and fall through
(return False) when either side isn't numeric — never raise.
"""

from __future__ import annotations

import pytest

from botcircuits.agent.workflow.engine.handlers.choice import _evaluate_operator
from botcircuits.agent.workflow.engine.utils import to_number


def _cond(operator: str, value):
    return {"operator": operator, "value": value}


# --- the original crashers: must return a bool, never raise ----------------

@pytest.mark.parametrize("operator", [
    "greater than", "greater than or equal", "less than", "less than or equal",
])
@pytest.mark.parametrize("variable_value", [None, "", "not-a-number"])
def test_unfilled_or_nonnumeric_slot_does_not_raise(operator, variable_value):
    # check_value is the typed numeric literal the indexer produced.
    assert _evaluate_operator(_cond(operator, 50), variable_value, {}) is False


# --- string slot vs numeric literal coerces correctly ----------------------

def test_string_slot_compares_numerically():
    # "640" >= 580 must be True, not a lexicographic/string error.
    assert _evaluate_operator(_cond("greater than or equal", 580), "640", {}) is True
    assert _evaluate_operator(_cond("less than", 580), "640", {}) is False


def test_numeric_slot_vs_string_literal_coerces():
    # Authored string-typed value `'500'` against a numeric slot.
    assert _evaluate_operator(_cond("greater than", "500"), 640, {}) is True
    assert _evaluate_operator(_cond("less than", "500"), 640, {}) is False


# --- correctness of each ordered operator on plain numbers -----------------

@pytest.mark.parametrize("operator,a,b,expected", [
    ("greater than", 51, 50, True),
    ("greater than", 50, 50, False),
    ("greater than or equal", 50, 50, True),
    ("less than", 49, 50, True),
    ("less than or equal", 50, 50, True),
    ("less than or equal", 51, 50, False),
])
def test_ordered_operators(operator, a, b, expected):
    assert _evaluate_operator(_cond(operator, b), a, {}) is expected


# --- to_number helper edge cases -------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None, None),
    ("", None),
    ("abc", None),
    (True, None),          # bools excluded so True/1 don't conflate
    (False, None),
    (5, 5.0),
    (5.5, 5.5),
    ("640", 640.0),
    ("  640  ", 640.0),    # whitespace tolerated
])
def test_to_number(value, expected):
    assert to_number(value) == expected


def test_is_coerces_bool_string_and_number():
    """`is`/`is not` tolerate bool<->string and number<->string mismatches that
    authors/generators commonly produce (e.g. `found is 'false'`)."""
    from botcircuits.agent.workflow.engine.handlers.choice import evaluate_choices

    def _route(var, op, val, slots):
        choices = [{"operator": "AND", "expressionList": [
            {"variable": var, "operator": op, "value": val}], "next": "hit"}]
        msg = {"data": {"sessionContext": {"slots": slots}}}
        return evaluate_choices(choices, msg, "miss")

    assert _route("found", "is", "false", {"found": False}) == "hit"
    assert _route("found", "is", "true", {"found": False}) == "miss"
    assert _route("ok", "is not", "false", {"ok": True}) == "hit"
    assert _route("total", "is", "5000", {"total": 5000}) == "hit"
    assert _route("status", "is", "Clear", {"status": "clear"}) == "hit"  # case-insens
