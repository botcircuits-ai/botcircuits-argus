"""Unit tests for `_parse_expression` — turning an indexed `expCondition`
string into the `{variable, operator, value}` shape the choice handler reads.

The indexer (and authors) phrase ordered comparisons as `X is less than Y` /
`X is greater than 5000`. A naive alternation matches the shorter `is` operator
first and swallows the real operator into the value (`value="less than qty"`),
so the branch silently never fires — this is what dropped order_fulfillment to
0% oracle accuracy (every backorder/review item fell through to the default).
These pin the longest-operator parse, the optional `is`/`is not` linking verb,
and the bare-identifier-RHS -> `{slot}` reference rule.
"""

from __future__ import annotations

import pytest

from botcircuits.agent.workflow.condition_processor import _parse_expression


@pytest.mark.parametrize("exp,operator,value", [
    # ordered comparisons, with and without the `is` linking verb
    ("stock is less than qty", "less than", "{qty}"),
    ("stock less than qty", "less than", "{qty}"),
    ("line_total is greater than 5000", "greater than", 5000),
    ("score greater than or equal 0.5", "greater than or equal", 0.5),
    ("stock less than or equal 0", "less than or equal", 0),
    # equality / negation keep their literal (unquoted) values
    ("found is false", "is", False),
    ("region is not US", "is not", "US"),
    ("status is blocked", "is", "blocked"),
    ("readme_found is 'yes'", "is", "yes"),
    # emptiness operators take no value
    ("readme_found is not empty", "is not empty", ""),
    ("readme_found is empty", "is empty", ""),
    # text operators
    ("name contains foo", "contains", "foo"),
])
def test_parse_expression(exp, operator, value):
    parsed = _parse_expression(exp)
    assert parsed is not None, exp
    assert parsed["operator"] == operator
    assert parsed["value"] == value


def test_unparseable_returns_none():
    # No supported operator token -> no match.
    assert _parse_expression("stock") is None
    assert _parse_expression("stock equals qty") is None
