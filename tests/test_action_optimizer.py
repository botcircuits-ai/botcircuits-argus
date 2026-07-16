"""Unit tests for the build-time action optimizer (Pass 1 of `workflow build`).

The LLM call is stubbed — these tests pin the SAFETY logic that protects a
naturally-authored workflow from a bad rewrite: anchor preservation, the
shorter-only guard, per-step fallback to the author's text, and the contract
that only `settings.action` is ever mutated.
"""

from __future__ import annotations

import asyncio


from botcircuits.agent.workflow import action_optimizer as ao


def _flow():
    return {
        "start": "screen",
        "steps": {
            "screen": {
                "type": "agentAction",
                "settings": {"action": (
                    "Read the order. You MUST call read_file on path "
                    "'data/current_order.json' and read_file 'data/fraud_blocklist.txt'. "
                    "Then set header_status to exactly the word 'valid' or 'invalid', "
                    "and set fraud_status to exactly 'blocked' or 'clear'."
                )},
                "choices": [{"operator": "AND", "expressionList": [
                    {"variable": "header_status", "operator": "is", "value": "invalid"}
                ], "next": "reject"}],
                "next": "process",
            },
            "process": {
                "type": "agentAction",
                "settings": {"action": (
                    "Run shell_exec `python3 bin/price.py <SKU> <QTY>` for each item; "
                    "end with a fenced json block of the form {\"customer\":..., "
                    "\"decisions\":[...]}."
                )},
            },
        },
    }


class _StubProvider:
    """Returns a canned optimizer JSON response; records the call."""

    def __init__(self, response: str):
        self._response = response
        self.calls = 0
        self.model = "stub"

    async def complete(self, **kw):
        self.calls += 1
        class _R:  # minimal LLMResponse shape the optimizer reads
            text = self._response
        return _R()


def _run(flow, response):
    prov = _StubProvider(response)
    summary = asyncio.run(ao.optimize_actions(flow, prov))
    return summary, prov


def test_accepts_shorter_meaning_preserving_rewrite():
    flow = _flow()
    resp = (
        '{"actions":['
        '{"step_id":"screen","action":"read_file \'data/current_order.json\' and '
        '\'data/fraud_blocklist.txt\'; set header_status \'valid\'|\'invalid\', '
        'fraud_status \'blocked\'|\'clear\'."}'
        ']}'
    )
    summary, _ = _run(flow, resp)
    assert summary["steps_optimized"] == 1
    new = flow["steps"]["screen"]["settings"]["action"]
    assert len(new) < 400  # got terser
    # anchors preserved
    for anchor in ("data/current_order.json", "data/fraud_blocklist.txt",
                   "valid", "invalid", "blocked", "clear"):
        assert anchor in new


def test_rejects_rewrite_that_drops_a_file_path_anchor():
    flow = _flow()
    before = flow["steps"]["screen"]["settings"]["action"]
    # Drops 'data/fraud_blocklist.txt' — lossy, must be rejected.
    resp = (
        '{"actions":['
        '{"step_id":"screen","action":"read_file \'data/current_order.json\'; set '
        'header_status \'valid\'|\'invalid\', fraud_status \'blocked\'|\'clear\'."}'
        ']}'
    )
    summary, _ = _run(flow, resp)
    assert summary["steps_optimized"] == 0
    assert flow["steps"]["screen"]["settings"]["action"] == before


def test_rejects_longer_rewrite():
    flow = _flow()
    before = flow["steps"]["process"]["settings"]["action"]
    longer = before + " Also explain your reasoning in detail for every item."
    resp = '{"actions":[{"step_id":"process","action":%s}]}' % (
        __import__("json").dumps(longer)
    )
    summary, _ = _run(flow, resp)
    # process got longer -> rejected; screen had no rewrite -> unchanged
    assert flow["steps"]["process"]["settings"]["action"] == before


def test_optimizer_failure_leaves_flow_untouched():
    flow = _flow()
    before = {k: v["settings"]["action"] for k, v in flow["steps"].items()}
    summary, _ = _run(flow, "this is not json at all")
    assert summary["steps_optimized"] == 0
    after = {k: v["settings"]["action"] for k, v in flow["steps"].items()}
    assert before == after


def test_only_action_is_mutated_never_structure():
    flow = _flow()
    choices_before = flow["steps"]["screen"]["choices"]
    next_before = flow["steps"]["screen"]["next"]
    resp = (
        '{"actions":['
        '{"step_id":"screen","action":"read_file \'data/current_order.json\' and '
        '\'data/fraud_blocklist.txt\'; set header_status \'valid\'|\'invalid\', '
        'fraud_status \'blocked\'|\'clear\'."}'
        ']}'
    )
    _run(flow, resp)
    assert flow["steps"]["screen"]["choices"] is choices_before
    assert flow["steps"]["screen"]["next"] == next_before
    assert flow["start"] == "screen"


def test_no_action_steps_is_noop():
    flow = {"start": "a", "steps": {"a": {"type": "systemAction", "settings": {}}}}
    summary, prov = _run(flow, '{"actions":[]}')
    assert summary["steps_optimized"] == 0
    assert prov.calls == 0  # short-circuits before calling the LLM


def test_anchors_extracts_paths_and_quoted_literals():
    a = ao._anchors(
        "read_file 'data/x.json'; set s to 'blocked'; run bin/price.py; \"clear\""
    )
    assert "data/x.json" in a
    assert "blocked" in a
    assert "clear" in a
    assert "bin/price.py" in a
