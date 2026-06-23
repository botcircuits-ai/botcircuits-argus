"""Unit tests for the build-time graph optimizer (Passes 2 & 3).

These are pure structural transforms, so correctness is everything: a bad fusion
or fold silently reroutes the workflow. Tests pin both the cases that MUST fire
and the cases that must be LEFT ALONE (shared predecessors, dependent branches,
branch-arm emits), plus that segmentation actually drops after fusion.
"""

from __future__ import annotations

from botcircuits.agent.workflow.graph_optimizer import (
    fold_terminal_emit,
    fuse_independent_branches,
    optimize_graph,
)
from botcircuits.agent.workflow.engine.segments import compute_segments


# --------------------------------------------------------------------------- #
# Pass 3 — branch fusion
# --------------------------------------------------------------------------- #

def _validate_fraud_flow():
    """validate (branch on header_status) -> fraud (branch on fraud_status)
    -> process. The two screens are independent and should fuse."""
    return {"start": "validate", "steps": {
        "validate": {"type": "agentAction", "settings": {"action": "check header"},
            "choices": [{"operator": "AND", "expressionList": [
                {"variable": "header_status", "operator": "is", "value": "invalid"}],
                "next": "reject_h"}],
            "next": "fraud"},
        "fraud": {"type": "agentAction", "settings": {"action": "check fraud"},
            "choices": [{"operator": "AND", "expressionList": [
                {"variable": "fraud_status", "operator": "is", "value": "blocked"}],
                "next": "reject_f"}],
            "next": "process"},
        "reject_h": {"type": "agentAction", "settings": {"action": "rej"}},
        "reject_f": {"type": "agentAction", "settings": {"action": "rej"}},
        "process": {"type": "agentAction", "settings": {"action": "process"}},
    }}


def test_fuses_independent_adjacent_branches():
    flow = _validate_fraud_flow()
    n = fuse_independent_branches(flow)
    assert n == 1
    assert "fraud" not in flow["steps"]          # fraud folded into validate
    v = flow["steps"]["validate"]
    # both choice arms survive, in order (header first, then fraud)
    targets = [c["next"] for c in v["choices"]]
    assert targets == ["reject_h", "reject_f"]
    assert v["next"] == "process"                # default now points past fraud
    assert "check header" in v["settings"]["action"]
    assert "check fraud" in v["settings"]["action"]


def test_fusion_cuts_segment_count():
    flow = _validate_fraud_flow()
    before = len(compute_segments(flow))
    fuse_independent_branches(flow)
    after = len(compute_segments(flow))
    assert after < before  # one fewer branch step => one fewer segment


def test_does_not_fuse_dependent_branches():
    # fraud tests header_status (what validate sets) -> NOT independent.
    flow = _validate_fraud_flow()
    flow["steps"]["fraud"]["choices"][0]["expressionList"][0]["variable"] = "header_status"
    n = fuse_independent_branches(flow)
    assert n == 0
    assert "fraud" in flow["steps"]


def test_does_not_fuse_when_B_has_other_predecessor():
    flow = _validate_fraud_flow()
    # reject_h also flows into fraud -> fraud has 2 predecessors, unsafe.
    flow["steps"]["reject_h"]["next"] = "fraud"
    n = fuse_independent_branches(flow)
    assert n == 0


def test_does_not_fuse_when_B_is_a_choice_arm_of_A():
    flow = _validate_fraud_flow()
    # validate early-exits TO fraud via a choice -> not a clean default chain.
    flow["steps"]["validate"]["choices"][0]["next"] = "fraud"
    n = fuse_independent_branches(flow)
    assert n == 0


def test_does_not_fuse_a_loop_select_into_its_body():
    """select_item (loop join, reached by every mark_*) -> lookup_item (loop
    body that runs price.py then branches). Disjoint branch vars would pass the
    naive check, but lookup depends on select's side effects and is a loop
    member — fusing collapses a sequential per-iteration step into the selector.
    Must NOT fuse."""
    flow = {"start": "select", "steps": {
        "select": {"type": "agentAction", "settings": {"action": "pick next item"},
            "choices": [{"operator": "AND", "expressionList": [
                {"variable": "items_remaining", "operator": "is", "value": "no"}],
                "next": "emit"}],
            "next": "lookup"},
        "lookup": {"type": "agentAction", "settings": {"action": "price.py, record facts"},
            "choices": [{"operator": "AND", "expressionList": [
                {"variable": "sku_found", "operator": "is", "value": False}],
                "next": "mark_reject"}],
            "next": "mark_fulfill"},
        "mark_reject": {"type": "agentAction", "settings": {"action": "rec"}, "next": "select"},
        "mark_fulfill": {"type": "agentAction", "settings": {"action": "rec"}, "next": "select"},
        "emit": {"type": "agentAction", "settings": {"action": "emit"}},
    }}
    n = fuse_independent_branches(flow)
    assert n == 0
    assert "lookup" in flow["steps"]          # loop body preserved
    assert flow["steps"]["select"]["next"] == "lookup"


# --------------------------------------------------------------------------- #
# Pass 2 — terminal-emit fold
# --------------------------------------------------------------------------- #

def _process_emit_flow():
    return {"start": "process", "steps": {
        "process": {"type": "agentAction",
            "settings": {"action": "run price.py and write_file data/decisions.json"},
            "next": "emit"},
        "emit": {"type": "agentAction", "settings": {"action":
            "read_file data/decisions.json and end your message with a fenced json block"}},
    }}


def test_folds_terminal_restatement_into_producer():
    flow = _process_emit_flow()
    n = fold_terminal_emit(flow)
    assert n == 1
    assert "emit" not in flow["steps"]
    p = flow["steps"]["process"]
    assert p.get("next") in (None,)               # process is now terminal
    act = p["settings"]["action"].lower()
    assert "json block" in act                    # a terse emit directive added
    assert "do not re-read" in act                # read-back-free (no redundant read)
    assert "write_file" in p["settings"]["action"]  # original work kept


def test_does_not_fold_non_restatement_terminal():
    flow = _process_emit_flow()
    flow["steps"]["emit"]["settings"]["action"] = "send an email to the manager"
    n = fold_terminal_emit(flow)
    assert n == 0
    assert "emit" in flow["steps"]


def test_does_not_fold_emit_reached_via_branch_arm():
    # emit is a CHOICE target -> folding into the branch's default would drop
    # the emit on that arm. Must be left alone.
    flow = {"start": "screen", "steps": {
        "screen": {"type": "agentAction", "settings": {"action": "screen"},
            "choices": [{"operator": "AND", "expressionList": [
                {"variable": "x", "operator": "is", "value": "y"}], "next": "emit"}],
            "next": "process"},
        "process": {"type": "agentAction", "settings": {"action": "work"}, "next": "emit"},
        "emit": {"type": "agentAction", "settings": {"action":
            "read_file result and emit a fenced json block as the final answer"}},
    }}
    n = fold_terminal_emit(flow)
    assert n == 0
    assert "emit" in flow["steps"]


def test_does_not_fold_the_start_step():
    flow = {"start": "only", "steps": {
        "only": {"type": "agentAction", "settings": {"action":
            "read_file x and emit a fenced json block, the final answer"}}}}
    assert fold_terminal_emit(flow) == 0


# --------------------------------------------------------------------------- #
# combined
# --------------------------------------------------------------------------- #

def test_optimize_graph_runs_both_and_summarizes():
    flow = _validate_fraud_flow()
    # give process a trailing emit so both passes have work
    flow["steps"]["process"]["next"] = "emit"
    flow["steps"]["emit"] = {"type": "agentAction", "settings": {"action":
        "read_file data/decisions.json and end with a fenced json block"}}
    s = optimize_graph(flow)
    assert s["branches_fused"] == 1
    assert s["emits_folded"] == 1
