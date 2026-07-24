"""Engine-driven runner + build-time segment computation.

These cover the inversion-of-control core directly (no provider): the
engine owns the loop, batches non-branching steps into segments, evaluates
branches deterministically against captured slots, records per-branch audit
structs, and routes a REQUIRED-but-unfillable branch variable to a
clarification pause instead of a silent default-branch fallthrough.
"""

from __future__ import annotations

import asyncio

import pytest

from botcircuits.agent.workflow.engine.runner import (
    SegmentResult,
    run_workflow_engine,
)
from botcircuits.agent.workflow.engine.segments import compute_segments


@pytest.fixture(autouse=True)
def _isolated_workflows_dir(tmp_path, monkeypatch):
    """Point the workflows dir at a temp location for EVERY test here: the
    engine persists last-run inputs (`.last_inputs/`) on completion, and
    that must never land in — or be read from — the developer's real
    `.botcircuits/workflows`."""
    import botcircuits.agent.workflow.local as wf_local
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path / "wfdir"))


def _built(flow: dict) -> dict:
    """Attach build-time segments, mirroring what the workflow builder
    emits into `.build/`. The runner reads `flow['segments']`; without it
    it falls back to one-step-per-segment (covered separately)."""
    flow["segments"] = compute_segments(flow)
    return flow


def _linear_flow() -> dict:
    """start → a → b → c (no branches): one segment of three actions."""
    return {
        "start": "start",
        "variables": [],
        "steps": {
            "start": {"type": "start", "next": "a"},
            "a": {"type": "agentAction", "settings": {"action": "do a"}, "next": "b"},
            "b": {"type": "agentAction", "settings": {"action": "do b"}, "next": "c"},
            "c": {"type": "agentAction", "settings": {"action": "do c"}},
        },
    }


def _branch_flow(required: bool = False) -> dict:
    """start → s1 (branch on color) → red | blue."""
    var = {"variableName": "color", "dataType": "string", "description": "the color"}
    if required:
        var["required"] = True
    return {
        "start": "start",
        "variables": [var],
        "steps": {
            "start": {"type": "start", "next": "s1"},
            "s1": {
                "type": "agentAction",
                "settings": {"action": "pick a color"},
                "next": "blue",
                "choices": [{
                    "operator": "OR",
                    "expressionList": [
                        {"variable": "color", "operator": "is", "value": "red"}
                    ],
                    "next": "red",
                }],
            },
            "red": {"type": "agentAction", "settings": {"action": "go red"}},
            "blue": {"type": "agentAction", "settings": {"action": "go blue"}},
        },
    }


# -- segment computation ----------------------------------------------------


def test_compute_segments_batches_linear_run():
    segs = compute_segments(_linear_flow())
    assert len(segs) == 1
    assert segs[0]["steps"] == ["a", "b", "c"]
    assert segs[0]["branchStep"] is None


def test_compute_segments_splits_on_branch():
    segs = compute_segments(_branch_flow())
    by_id = {s["id"]: s for s in segs}
    # s1 segment ends on the branch; red and blue are their own segments.
    assert by_id["start"]["branchStep"] == "s1"
    assert "red" in by_id and "blue" in by_id


def test_compute_segments_splits_on_agent_change():
    """Two consecutive non-branching steps pinned to different agents must
    NOT be merged into one segment — a segment is one LLM call, and that
    call can only go to one agent/model."""
    flow = {
        "start": "start",
        "variables": [],
        "steps": {
            "start": {"type": "start", "next": "a"},
            "a": {"type": "agentAction", "agent": "researcher",
                  "settings": {"action": "do a"}, "next": "b"},
            "b": {"type": "agentAction", "agent": "researcher",
                  "settings": {"action": "do b"}, "next": "c"},
            "c": {"type": "agentAction", "settings": {"action": "do c"}},
        },
    }
    segs = compute_segments(flow)
    by_id = {s["id"]: s for s in segs}
    # a+b share the "researcher" agent and batch together (segment id is
    # "start", the walk's head — "start" is transparent); c has no agent
    # (the run default) and gets re-queued as its own segment.
    assert by_id["start"]["steps"] == ["a", "b"]
    assert by_id["start"]["agent"] == "researcher"
    assert by_id["c"]["steps"] == ["c"]
    assert by_id["c"]["agent"] is None


def test_compute_segments_default_agent_is_none():
    segs = compute_segments(_linear_flow())
    assert segs[0]["agent"] is None


def test_question_step_is_isolated_into_its_own_segment():
    """A `question` must never be bundled with a preceding action step.
    Bundling breaks pause/resume — the resumed segment replays the earlier
    action and re-asks, so the reply is never consumed and a branching retry
    question never evaluates its choices (the stuck-retry-loop bug)."""
    flow = {
        "start": "start",
        "variables": [],
        "steps": {
            "start": {"type": "start", "next": "inform"},
            # An info action that statically flows into a branching question.
            "inform": {
                "type": "agentAction",
                "settings": {"action": "tell the user something"},
                "next": "ask_retry",
            },
            "ask_retry": {
                "type": "question",
                "settings": {"action": "check another? (yes/no)"},
                "next": "end",
                "choices": [{
                    "operator": "OR",
                    "expressionList": [
                        {"variable": "again", "operator": "is", "value": "yes"}
                    ],
                    "next": "inform",
                }],
            },
            "end": {"type": "agentAction", "settings": {"action": "bye"}},
        },
    }
    by_id = {s["id"]: s for s in compute_segments(flow)}
    # `inform` and `ask_retry` are SEPARATE segments — not bundled.
    assert by_id["inform"]["steps"] == ["inform"]
    assert by_id["inform"]["branchStep"] is None
    assert by_id["ask_retry"]["steps"] == ["ask_retry"]
    assert by_id["ask_retry"]["branchStep"] == "ask_retry"


# -- engine loop ------------------------------------------------------------


def _collect_runner():
    seen: list[list[str]] = []

    async def run(*, actions, branch_variables, system_notes, slots):
        seen.append(list(actions))
        return SegmentResult(text="ok", captured_slots={})

    return run, seen


def test_linear_runs_as_single_segment_call():
    run, seen = _collect_runner()
    res = asyncio.run(run_workflow_engine(
        _built(_linear_flow()), workflow_name="lin", run_segment=run))
    assert res.done and not res.paused
    # One LLM call for the whole 3-step linear run — cost scales with
    # branches (zero here → one segment), not steps.
    assert len(seen) == 1
    assert seen[0] == ["do a", "do b", "do c"]


def test_branch_takes_matching_path_from_captured_slot():
    order: list[list[str]] = []

    async def run(*, actions, branch_variables, system_notes, slots):
        order.append(list(actions))
        cap = {"color": "red"} if any(
            v["variableName"] == "color" for v in branch_variables) else {}
        return SegmentResult(text="ok", captured_slots=cap)

    res = asyncio.run(run_workflow_engine(
        _built(_branch_flow()), workflow_name="br", run_segment=run))
    assert res.done
    flat = [a for tup in order for a in tup]
    assert "go red" in flat and "go blue" not in flat
    # A decision record was persisted for the branch.
    assert any(d.get("variable") == "color" for d in res.decisions)


def test_branch_defaults_when_optional_var_empty():
    async def run(*, actions, branch_variables, system_notes, slots):
        return SegmentResult(text="ok", captured_slots={})  # never reports color

    res = asyncio.run(run_workflow_engine(
        _built(_branch_flow(required=False)), workflow_name="br", run_segment=run))
    # Optional empty → default branch (blue), NOT a clarification pause.
    assert res.done and not res.paused


def test_required_unfilled_var_routes_to_clarification():
    async def run(*, actions, branch_variables, system_notes, slots):
        return SegmentResult(text="ok", captured_slots={})  # never reports color

    res = asyncio.run(run_workflow_engine(
        _built(_branch_flow(required=True)), workflow_name="br", run_segment=run))
    assert res.paused and not res.done
    assert res.paused_step == "start"
    assert "color" in res.question.lower() or "information" in res.question.lower()


def test_user_pause_yields_with_resume_cursor():
    async def run(*, actions, branch_variables, system_notes, slots):
        return SegmentResult(paused=True, question="What color?")

    res = asyncio.run(run_workflow_engine(
        _built(_branch_flow()), workflow_name="br", run_segment=run))
    assert res.paused
    assert res.question == "What color?"
    assert res.paused_step == "start"


def test_resolve_unfilled_backfills_before_branch():
    async def run(*, actions, branch_variables, system_notes, slots):
        return SegmentResult(text="ok", captured_slots={})

    async def resolver(*, flow, step_id, variables, slots):
        return {"color": "red"}  # Tier-0/2 supplies what record_slots didn't

    res = asyncio.run(run_workflow_engine(
        _built(_branch_flow(required=True)), workflow_name="br",
        run_segment=run, resolve_unfilled=resolver))
    # Backfilled → branch resolves, no clarification.
    assert res.done and not res.paused
    assert res.slots.get("color") == "red"


def _retry_loop_flow() -> dict:
    """start → q1 (question) → q2 (branching question; "again" loops back to q1).
    Models the order-status retry loop: two question steps, one looping back."""
    return {
        "start": "start",
        "variables": [
            {"variableName": "again", "dataType": "string", "description": "loop?"},
        ],
        "steps": {
            "start": {"type": "start", "next": "q1"},
            "q1": {"type": "question", "settings": {"action": "Ask: value?"},
                   "next": "q2"},
            "q2": {
                "type": "question",
                "settings": {"action": "Ask: again? (yes/no)"},
                "next": "end",
                "choices": [{
                    "operator": "OR",
                    "expressionList": [
                        {"variable": "again", "operator": "is", "value": "yes"}
                    ],
                    "next": "q1",
                }],
            },
            "end": {"type": "agentAction", "settings": {"action": "done"}},
        },
    }


def test_stale_reply_cleared_so_loopback_question_pauses():
    """The resume reply (`__last_user_message__`) must be consumed ONCE. When a
    branching question loops back to an earlier question in the same in-process
    walk, the earlier question must PAUSE for fresh input — not re-consume the
    stale reply and spin the loop forever (the stuck-retry-loop bug)."""

    async def run(*, actions, branch_variables, system_notes, slots):
        reply = slots.get("__last_user_message__")
        bvars = [v["variableName"] for v in branch_variables]
        # A question with no fresh reply pauses; with a reply it consumes it.
        if reply is None:
            return SegmentResult(paused=True, question=" ".join(actions))
        if bvars:  # q2: map reply to the branch var
            return SegmentResult(captured_slots={bvars[0]: reply})
        return SegmentResult(captured_slots={})  # q1: just consumes the reply

    flow = _built(_retry_loop_flow())

    # First leg: pauses at q1.
    r = asyncio.run(run_workflow_engine(flow, workflow_name="loop", run_segment=run))
    assert r.paused and r.paused_step == "start"

    # Reply to q1 → engine advances to q2 and pauses there. Crucially it does
    # NOT carry "v1" forward into q2.
    s = {**r.slots, "__last_user_message__": "v1"}
    r = asyncio.run(run_workflow_engine(
        flow, workflow_name="loop", run_segment=run, start_step_id=r.paused_step,
        slots=s))
    assert r.paused and r.paused_step == "q2"

    # Reply "yes" to q2 → loops back to q1, which MUST pause again (the stale
    # "yes" was cleared, so q1 sees no reply). Before the fix this looped
    # forever, re-deciding "yes" without ever pausing.
    s = {**r.slots, "__last_user_message__": "yes"}
    r = asyncio.run(run_workflow_engine(
        flow, workflow_name="loop", run_segment=run, start_step_id=r.paused_step,
        slots=s))
    assert r.paused and r.paused_step == "q1"
    assert "__last_user_message__" not in r.slots


# -- data variables (carried key-value memory) ------------------------------


def _scrape_save_flow() -> dict:
    """start → scrape (branch on count>0) → save | none.

    `scraped_jobs` is a DATA variable (non-branch): the scrape segment
    produces it, the save segment must receive it back via slots.
    """
    return {
        "start": "start",
        "variables": [
            {"variableName": "count", "dataType": "number", "description": "n"},
            {"variableName": "scraped_jobs", "dataType": "string",
             "description": "carried payload"},
        ],
        "steps": {
            "start": {"type": "start", "next": "scrape"},
            "scrape": {
                "type": "agentAction",
                "settings": {"action": "scrape jobs"},
                "next": "none",
                "choices": [{
                    "operator": "AND",
                    "expressionList": [
                        {"variable": "count", "operator": "greater than", "value": 0}
                    ],
                    "next": "save",
                }],
            },
            "save": {"type": "agentAction", "settings": {"action": "save jobs"}},
            "none": {"type": "agentAction", "settings": {"action": "print none"}},
        },
    }


def test_data_variable_carries_from_scrape_to_save():
    seen_data_vars: list[list[str]] = []
    save_slots: dict = {}

    async def run(*, actions, branch_variables, system_notes, slots,
                  data_variables=None, **_):
        # Every segment is offered the data variable in scope.
        seen_data_vars.append(
            [v["variableName"] for v in (data_variables or [])]
        )
        if "scrape jobs" in actions:
            # Scrape produces BOTH the branch var and the data payload.
            return SegmentResult(
                text="scraped",
                captured_slots={"count": 2, "scraped_jobs": '[{"t":"SWE"}]'},
            )
        if "save jobs" in actions:
            # Save must SEE the carried payload in its slots.
            save_slots.update(slots)
        return SegmentResult(text="ok", captured_slots={})

    res = asyncio.run(run_workflow_engine(
        _built(_scrape_save_flow()), workflow_name="js", run_segment=run))

    assert res.done and not res.paused
    # Took the save branch (count>0), and save received the carried payload.
    assert save_slots.get("scraped_jobs") == '[{"t":"SWE"}]'
    assert save_slots.get("count") == 2
    # The data variable was advertised to segments (not the branch-only set).
    assert any("scraped_jobs" in dv for dv in seen_data_vars)


def test_data_variable_absent_means_no_data_kwarg():
    # A flow with ONLY branch variables passes no data_variables — simple
    # runners that don't accept the kwarg still work (back-compat).
    async def run(*, actions, branch_variables, system_notes, slots):
        return SegmentResult(text="ok", captured_slots={"color": "red"})

    res = asyncio.run(run_workflow_engine(
        _built(_branch_flow()), workflow_name="br", run_segment=run))
    assert res.done


# -- initial input collection (input: true variables) --------------------------


def _input_flow() -> dict:
    """start → research (references user-supplied topic/depth)."""
    return _built({
        "start": "start",
        "variables": [
            {"variableName": "topic", "description": "The topic to research.",
             "input": True},
            {"variableName": "depth", "description": "Desired depth.",
             "input": True},
            {"variableName": "report", "description": "Produced report."},
        ],
        "steps": {
            "start": {"type": "start", "next": "research"},
            "research": {"type": "agentAction",
                         "settings": {"action": "Research `topic` at `depth`."}},
        },
    })


def test_missing_inputs_pause_before_first_segment():
    segment_ran = {"n": 0}

    async def run_segment(**kw):
        segment_ran["n"] += 1
        return SegmentResult(text="ok", captured_slots={})

    result = asyncio.run(run_workflow_engine(
        _input_flow(), workflow_name="wf", run_segment=run_segment,
    ))
    assert result.paused and not result.done
    assert result.paused_step is None          # resume restarts collection
    assert segment_ran["n"] == 0               # nothing ran without inputs
    assert "topic — The topic to research." in result.question
    assert "depth" in result.question
    assert "report" not in result.question     # produced vars never asked


def test_inputs_resolved_from_history_skip_the_pause():
    """The resolve hook (Tier-0/Tier-2 over the trigger context) fills the
    inputs — the workflow starts without asking."""
    async def run_segment(**kw):
        return SegmentResult(text="ok", captured_slots={})

    async def resolve(*, flow, step_id, variables, slots):
        assert {v["variableName"] for v in variables} == {"topic", "depth"}
        return {"topic": "AI in finance", "depth": "3 pages"}

    result = asyncio.run(run_workflow_engine(
        _input_flow(), workflow_name="wf", run_segment=run_segment,
        resolve_unfilled=resolve,
        slots={"__last_user_message__": "run wf on AI in finance, 3 pages"},
    ))
    assert result.done and not result.paused
    assert result.slots["topic"] == "AI in finance"


def test_partial_resolution_asks_only_for_the_rest():
    async def run_segment(**kw):
        return SegmentResult(text="ok", captured_slots={})

    async def resolve(*, flow, step_id, variables, slots):
        return {"topic": "AI in finance"}     # depth stays missing

    result = asyncio.run(run_workflow_engine(
        _input_flow(), workflow_name="wf", run_segment=run_segment,
        resolve_unfilled=resolve,
    ))
    assert result.paused
    assert "depth" in result.question and "topic" not in result.question
    assert result.slots["topic"] == "AI in finance"  # kept for the resume


def test_resume_after_collection_runs_the_flow():
    """Second call (paused_step None + previously collected slots + the
    user's reply resolved) proceeds into the segment."""
    async def run_segment(**kw):
        return SegmentResult(text="ok", captured_slots={})

    async def resolve(*, flow, step_id, variables, slots):
        return {"depth": "3 pages"}  # extracted from the reply

    result = asyncio.run(run_workflow_engine(
        _input_flow(), workflow_name="wf", run_segment=run_segment,
        resolve_unfilled=resolve,
        slots={"topic": "AI in finance",
               "__last_user_message__": "3 pages"},
    ))
    assert result.done


def test_unmarked_workflows_never_pre_pause():
    async def run_segment(**kw):
        return SegmentResult(text="ok", captured_slots={})

    result = asyncio.run(run_workflow_engine(
        _built(_linear_flow()), workflow_name="wf", run_segment=run_segment,
    ))
    assert result.done


# -- remembered inputs: offer, never silently reuse -----------------------------


def _remember(tmp_path, monkeypatch, values: dict, name="wf"):
    import botcircuits.agent.workflow.local as wf_local
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    import json as _json
    d = tmp_path / ".last_inputs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(_json.dumps(values))


async def _noop_segment(**kw):
    return SegmentResult(text="ok", captured_slots={})


def _run(flow, slots=None, resolve=None, interpret=None):
    return asyncio.run(run_workflow_engine(
        flow, workflow_name="wf", run_segment=_noop_segment,
        slots=slots or {}, resolve_unfilled=resolve,
        interpret_reply=interpret,
    ))


def test_completed_run_saves_input_values(tmp_path, monkeypatch):
    import botcircuits.agent.workflow.local as wf_local
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    result = _run(_input_flow(), slots={"topic": "AI", "depth": "3 pages"})
    assert result.done
    import json as _json
    saved = _json.loads((tmp_path / ".last_inputs" / "wf.json").read_text())
    assert saved == {"topic": "AI", "depth": "3 pages"}


def test_remembered_values_are_offered_not_reused(tmp_path, monkeypatch):
    from botcircuits.agent.workflow.engine.runner import PENDING_REUSE_KEY
    _remember(tmp_path, monkeypatch, {"topic": "AI", "depth": "3 pages"})
    result = _run(_input_flow())
    assert result.paused
    assert "from the last run" in result.question
    assert "topic: AI" in result.question and "depth: 3 pages" in result.question
    assert result.slots[PENDING_REUSE_KEY] == {"topic": "AI", "depth": "3 pages"}


def test_reuse_reply_yes_variants_adopt_all(tmp_path, monkeypatch):
    _remember(tmp_path, monkeypatch, {"topic": "AI", "depth": "3 pages"})
    for reply in ("yes", "yes use", "y", "ok", "sure", "use them", "go ahead"):
        first = _run(_input_flow())
        resumed = _run(_input_flow(),
                       slots={**first.slots, "__last_user_message__": reply})
        assert resumed.done, f"reply {reply!r} did not adopt the offer"
        assert resumed.slots["topic"] == "AI"


def test_reuse_reply_no_falls_back_to_normal_collection(tmp_path, monkeypatch):
    _remember(tmp_path, monkeypatch, {"topic": "AI", "depth": "3 pages"})
    first = _run(_input_flow())
    resumed = _run(_input_flow(),
                   slots={**first.slots, "__last_user_message__": "no"})
    assert resumed.paused
    assert "please provide" in resumed.question       # the normal ask
    assert "from the last run" not in resumed.question  # offered only once
    assert "topic" in resumed.question and "depth" in resumed.question


def test_reuse_reply_change_one_keeps_the_others(tmp_path, monkeypatch):
    _remember(tmp_path, monkeypatch, {"topic": "AI", "depth": "3 pages"})
    first = _run(_input_flow())
    resumed = _run(_input_flow(),
                   slots={**first.slots,
                          "__last_user_message__": "i want to change depth"})
    assert resumed.paused
    assert resumed.slots["topic"] == "AI"              # kept
    assert "depth" in resumed.question and "topic" not in resumed.question


def test_reuse_reply_change_with_value_extracts_it(tmp_path, monkeypatch):
    """"change depth to 5 pages" — the mentioned variable is dropped from the
    offer AND the reply stays available for extraction, so the new value
    lands without another ask."""
    _remember(tmp_path, monkeypatch, {"topic": "AI", "depth": "3 pages"})

    async def resolve(*, flow, step_id, variables, slots):
        assert [v["variableName"] for v in variables] == ["depth"]
        assert "5 pages" in slots.get("__last_user_message__", "")
        return {"depth": "5 pages"}

    first = _run(_input_flow())
    resumed = _run(_input_flow(), resolve=resolve,
                   slots={**first.slots,
                          "__last_user_message__": "change depth to 5 pages"})
    assert resumed.done
    assert resumed.slots["topic"] == "AI"
    assert resumed.slots["depth"] == "5 pages"


def test_reuse_reply_change_without_value_reasks_not_fabricates(tmp_path, monkeypatch):
    """A bare "change topic" (no replacement value) must re-ask for topic —
    the command phrase must NEVER be extracted as the new topic. Regression:
    "change topic" once saved topic="change topic"."""
    _remember(tmp_path, monkeypatch, {"topic": "AI", "depth": "3 pages"})

    async def resolve(*, flow, step_id, variables, slots):
        # If extraction runs at all here it would fabricate from the command;
        # the fix clears the message so this must see an EMPTY context.
        assert not slots.get("__last_user_message__")
        return {}

    first = _run(_input_flow())
    resumed = _run(_input_flow(), resolve=resolve,
                   slots={**first.slots,
                          "__last_user_message__": "change topic"})
    assert resumed.paused                              # re-asks, not done
    assert resumed.slots.get("depth") == "3 pages"     # kept
    assert resumed.slots.get("topic") in (None, "")    # NOT "change topic"
    assert "topic" in resumed.question


def test_reuse_reply_free_form_is_treated_as_fresh_input(tmp_path, monkeypatch):
    _remember(tmp_path, monkeypatch, {"topic": "AI", "depth": "3 pages"})

    async def resolve(*, flow, step_id, variables, slots):
        return {"topic": "Robotics", "depth": "2 pages"}

    first = _run(_input_flow())
    resumed = _run(_input_flow(), resolve=resolve,
                   slots={**first.slots,
                          "__last_user_message__": "Robotics, 2 pages"})
    assert resumed.done
    assert resumed.slots["topic"] == "Robotics"   # remembered NOT adopted


def test_reuse_reply_same_family_adopts_all(tmp_path, monkeypatch):
    """"yes do same," and friends are acceptance, not a topic (the phrase
    that once got researched verbatim)."""
    _remember(tmp_path, monkeypatch, {"topic": "AI", "depth": "3 pages"})
    for reply in ("yes do same,", "do the same", "same as last time",
                  "same", "ok same", "yes do same as last run"):
        first = _run(_input_flow())
        resumed = _run(_input_flow(),
                       slots={**first.slots, "__last_user_message__": reply})
        assert resumed.done, f"reply {reply!r} did not adopt the offer"
        assert resumed.slots["topic"] == "AI"


def test_reuse_pause_carries_selector_options(tmp_path, monkeypatch):
    _remember(tmp_path, monkeypatch, {"topic": "AI", "depth": "3 pages"})
    result = _run(_input_flow())
    assert result.paused
    assert result.options == ["yes", "no", "change topic", "change depth"]


def test_unclear_reuse_reply_asks_the_llm_hook(tmp_path, monkeypatch):
    """A typed reply the regexes don't understand goes to `interpret_reply`;
    a "yes" classification adopts the offer and consumes the reply."""
    _remember(tmp_path, monkeypatch, {"topic": "AI", "depth": "3 pages"})
    seen = {}

    async def interpret(*, question, options, reply):
        seen.update(question=question, options=options, reply=reply)
        return "yes"

    first = _run(_input_flow())
    resumed = _run(_input_flow(), interpret=interpret,
                   slots={**first.slots,
                          "__last_user_message__": "sounds good, run it again"})
    assert resumed.done
    assert resumed.slots["topic"] == "AI"
    assert seen["reply"] == "sounds good, run it again"
    assert seen["options"] == ["yes", "no", "change topic", "change depth"]


def test_llm_hook_none_keeps_free_form_extraction(tmp_path, monkeypatch):
    """When the hook says the reply is NOT picking an option, it stays
    available to extraction as fresh values — remembered ones dropped."""
    _remember(tmp_path, monkeypatch, {"topic": "AI", "depth": "3 pages"})

    async def interpret(*, question, options, reply):
        return None

    async def resolve(*, flow, step_id, variables, slots):
        return {"topic": "Robotics", "depth": "2 pages"}

    first = _run(_input_flow())
    resumed = _run(_input_flow(), interpret=interpret, resolve=resolve,
                   slots={**first.slots,
                          "__last_user_message__": "Robotics, 2 pages"})
    assert resumed.done
    assert resumed.slots["topic"] == "Robotics"


def test_change_mention_matches_description_words():
    """"change pages" refers to research_depth via its description."""
    from botcircuits.agent.workflow.engine.runner import interpret_reuse_reply
    variables = [
        {"variableName": "topic", "description": "The topic to research."},
        {"variableName": "research_depth",
         "description": "Desired length in pages."},
    ]
    offer = {"topic": "AI", "research_depth": "3 pages"}
    accepted, consume = interpret_reuse_reply(
        "i want to change pages", offer, variables)
    assert accepted == {"topic": "AI"}
    assert consume is False  # reply stays available for value extraction


# -- parallel nodes: true-concurrency fan-out/join --------------------------


def _parallel_flow(on_error: str | None = None) -> dict:
    """start → fanout (parallel: credit | inventory(2 steps) | fraud) → finish."""
    step = {
        "type": "parallel",
        "branches": {
            "credit": ["check_credit"],
            "inventory": ["check_inventory", "reserve_stock"],
            "fraud": ["check_fraud"],
        },
        "next": "finish",
    }
    if on_error:
        step["onError"] = on_error
    steps = {
        "start": {"type": "start", "next": "fanout"},
        "fanout": step,
        "check_credit": {"type": "agentAction",
                          "settings": {"action": "check credit"}},
        "check_inventory": {"type": "agentAction",
                             "settings": {"action": "check inventory"},
                             "next": "reserve_stock"},
        "reserve_stock": {"type": "agentAction",
                           "settings": {"action": "reserve stock"}},
        "check_fraud": {"type": "agentAction",
                         "settings": {"action": "check fraud"}},
        "finish": {"type": "agentAction", "settings": {"action": "finish"}},
    }
    if on_error:
        steps[on_error] = {"type": "agentAction",
                            "settings": {"action": "handle failure"}}
    return {"start": "start", "variables": [], "steps": steps}


def test_compute_segments_isolates_parallel_step():
    segs = compute_segments(_parallel_flow())
    by_id = {s["id"]: s for s in segs}
    fanout = by_id["fanout"]
    assert fanout["steps"] == ["fanout"]
    assert fanout["branchStep"] is None
    parallel = fanout["parallel"]
    assert set(parallel["branches"]) == {"credit", "inventory", "fraud"}
    assert parallel["next"] == "finish"
    # inventory's 2-step chain compiles into one inner segment (no branch).
    inv_segs = parallel["branches"]["inventory"]
    assert len(inv_segs) == 1
    assert inv_segs[0]["steps"] == ["check_inventory", "reserve_stock"]
    # "finish" is reachable as its own segment (queued from the parallel step).
    assert "finish" in by_id


def test_compute_segments_rejects_branch_with_conditions():
    flow = _parallel_flow()
    flow["steps"]["check_credit"]["choices"] = [{
        "operator": "OR",
        "expressionList": [{"variable": "x", "operator": "is", "value": "y"}],
        "next": "finish",
    }]
    with pytest.raises(ValueError, match="credit"):
        compute_segments(flow)


def test_compute_segments_rejects_question_in_branch():
    flow = _parallel_flow()
    flow["steps"]["check_fraud"] = {"type": "question",
                                     "settings": {"action": "fraud ok?"}}
    with pytest.raises(ValueError, match="fraud"):
        compute_segments(flow)


def test_compute_segments_rejects_nested_parallel():
    flow = _parallel_flow()
    flow["steps"]["check_credit"] = {"type": "parallel", "branches": {
        "x": ["check_fraud"]}, "next": "finish"}
    with pytest.raises(ValueError, match="credit"):
        compute_segments(flow)


def _branch_run_segment(by_action: dict):
    """A run_segment stub that looks up each action text a segment is asked
    to perform in `by_action` and merges the matching `SegmentResult`s —
    segments may bundle more than one step's action (e.g. a 2-step branch
    chain with no internal branch point), so every match must contribute."""
    async def run(*, actions, branch_variables, system_notes, slots, **kw):
        merged_slots: dict = {}
        matched = False
        for a in actions:
            result = by_action.get(a)
            if result is None:
                continue
            matched = True
            if result.paused:
                return result
            merged_slots.update(result.captured_slots)
        if not matched:
            return SegmentResult(text="ok", captured_slots={})
        return SegmentResult(text="ok", captured_slots=merged_slots)
    return run


def test_parallel_all_branches_succeed_and_slots_merge():
    run = _branch_run_segment({
        "check credit": SegmentResult(captured_slots={"credit_ok": True}),
        "check inventory": SegmentResult(captured_slots={"inv_seen": True}),
        "reserve stock": SegmentResult(captured_slots={"inventory_ok": True}),
        "check fraud": SegmentResult(captured_slots={"fraud_ok": True}),
    })
    res = asyncio.run(run_workflow_engine(
        _built(_parallel_flow()), workflow_name="po", run_segment=run))
    assert res.done and not res.paused
    assert res.slots["credit_ok"] is True
    assert res.slots["inventory_ok"] is True
    assert res.slots["fraud_ok"] is True
    assert res.slots["inv_seen"] is True
    assert any(d.get("parallel") == "fanout" and d.get("status") == "ok"
               for d in res.decisions)


def test_parallel_branch_pause_fails_node_without_on_error():
    from botcircuits.agent.workflow.engine.runner import WorkflowParallelError
    run = _branch_run_segment({
        "check credit": SegmentResult(captured_slots={"credit_ok": True}),
        "check inventory": SegmentResult(captured_slots={"inv_seen": True}),
        "reserve stock": SegmentResult(captured_slots={"inventory_ok": True}),
        "check fraud": SegmentResult(paused=True, question="really?"),
    })
    with pytest.raises(WorkflowParallelError, match="fraud"):
        asyncio.run(run_workflow_engine(
            _built(_parallel_flow()), workflow_name="po", run_segment=run))


def test_parallel_branch_pause_routes_to_on_error():
    run = _branch_run_segment({
        "check credit": SegmentResult(captured_slots={"credit_ok": True}),
        "check fraud": SegmentResult(paused=True, question="really?"),
    })
    res = asyncio.run(run_workflow_engine(
        _built(_parallel_flow(on_error="fraud_failed")),
        workflow_name="po", run_segment=run))
    assert res.done and not res.paused
    # No partial merge: even the successful branches' slots are discarded.
    assert "credit_ok" not in res.slots
    assert "__parallel_error__" in res.slots


def test_parallel_branch_exception_fails_node():
    from botcircuits.agent.workflow.engine.runner import WorkflowParallelError

    async def run(*, actions, branch_variables, system_notes, slots, **kw):
        if "check inventory" in actions:
            raise RuntimeError("boom")
        return SegmentResult(captured_slots={})

    with pytest.raises(WorkflowParallelError):
        asyncio.run(run_workflow_engine(
            _built(_parallel_flow()), workflow_name="po", run_segment=run))


def test_parallel_slot_collision_across_branches_fails():
    run = _branch_run_segment({
        "check credit": SegmentResult(captured_slots={"status": "ok"}),
        "check fraud": SegmentResult(captured_slots={"status": "flagged"}),
    })
    with pytest.raises(Exception, match="collision"):
        asyncio.run(run_workflow_engine(
            _built(_parallel_flow()), workflow_name="po", run_segment=run))


def test_parallel_branches_run_concurrently():
    """Branches must overlap in wall-clock time, not run one after another —
    the whole point of `parallel` over a plain sequential chain. Only the
    slowest branch's `check_inventory` step sleeps; if branches ran
    sequentially the fast ones would fully finish (entered AND exited)
    before it even entered."""
    import time

    entered_at: dict[str, float] = {}
    exited_at: dict[str, float] = {}

    async def run(*, actions, branch_variables, system_notes, slots, **kw):
        name = actions[0] if actions else "?"
        entered_at[name] = time.monotonic()
        if name == "check inventory":
            await asyncio.sleep(0.05)
        exited_at[name] = time.monotonic()
        return SegmentResult(captured_slots={})

    asyncio.run(run_workflow_engine(
        _built(_parallel_flow()), workflow_name="po", run_segment=run))

    # All three branches must have STARTED before the slow one finished —
    # proof the engine didn't wait for "check credit"/"check fraud" to fully
    # complete before starting "check inventory".
    branch_actions = {"check credit", "check inventory", "check fraud"}
    assert branch_actions <= set(entered_at)
    assert entered_at["check credit"] < exited_at["check inventory"]
    assert entered_at["check fraud"] < exited_at["check inventory"]
