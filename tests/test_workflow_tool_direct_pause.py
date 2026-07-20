"""The workflow tool answers input-collection pauses DIRECTLY on the CLI
pause channel — question + selector options render deterministically and
the reply re-enters the engine, with no model relay in between (which can
rephrase the question and lose the options entirely)."""

from __future__ import annotations

import asyncio
import json

import botcircuits.agent.workflow.local as wf_local
from botcircuits.agent.workflow import workflow_tool
from botcircuits.agent.workflow.engine.runner import SegmentResult
from botcircuits.agent.workflow.engine.segments import compute_segments


def _record() -> dict:
    flow = {
        "start": "start",
        "variables": [
            {"variableName": "topic", "description": "The topic to research.",
             "input": True},
            {"variableName": "depth", "description": "Desired depth.",
             "input": True},
        ],
        "steps": {
            "start": {"type": "start", "next": "research"},
            "research": {"type": "agentAction",
                         "settings": {"action": "Research `topic`."}},
        },
    }
    flow["segments"] = compute_segments(flow)
    return {"name": "wf_inputs", "description": "test", "flow": flow}


def _setup(tmp_path, monkeypatch, remembered: dict | None = None) -> None:
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    build = tmp_path / ".build"
    build.mkdir(parents=True, exist_ok=True)
    (build / "wf_inputs.json").write_text(json.dumps(_record()))
    if remembered:
        li = tmp_path / ".last_inputs"
        li.mkdir(parents=True, exist_ok=True)
        (li / "wf_inputs.json").write_text(json.dumps(remembered))


async def _noop_segment(**kw):
    return SegmentResult(text="ok", captured_slots={})


class _Channel:
    """Fake pause channel (quacks like WorkflowTask/TUISession.pause)."""

    def __init__(self, answers: list[str]):
        self._answers = list(answers)
        self.pauses: list[tuple[str, list[str] | None]] = []

    async def pause(self, question, options=None, default_index=0):
        self.pauses.append((question, options))
        return self._answers.pop(0)


class _StubProvider:
    """Tier-2 extraction stub: always "extracts" depth = 5 pages (the
    hallucination guard passes because the reply contains it verbatim)."""

    async def complete(self, **kw):
        class R:
            text = '{"normalized": {"depth": "5 pages"}}'

        return R()


def _invoke(channel, provider=None, last_user_message="") -> str:
    tool = workflow_tool(_record(), provider=provider)
    ctx = {"run_segment": _noop_segment}
    if last_user_message:
        ctx["last_user_message"] = last_user_message
    if channel is not None:
        ctx["_workflow_bg"] = channel
    return asyncio.run(tool.handler({}, ctx))


def test_reuse_offer_is_answered_on_the_channel(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, remembered={"topic": "AI", "depth": "3"})
    ch = _Channel(["yes"])
    out = _invoke(ch)
    assert len(ch.pauses) == 1
    question, options = ch.pauses[0]
    assert "from the last run" in question
    assert options == ["yes", "no", "change topic", "change depth"]
    # The run completed within the single tool call — the model only ever
    # sees the final summary, never the reuse question.
    assert "from the last run" not in out


def test_change_pick_reasks_only_that_variable(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, remembered={"topic": "AI", "depth": "3"})
    ch = _Channel(["change depth", "5 pages"])
    out = _invoke(ch, provider=_StubProvider())
    # Second pause: the authored ask for `depth` only, no options — and
    # never a SECOND reuse offer (offered at most once per run, across
    # engine re-entries).
    assert len(ch.pauses) == 2
    question2, options2 = ch.pauses[1]
    assert "depth" in question2 and "topic" not in question2
    assert "from the last run" not in question2
    assert options2 is None
    assert "from the last run" not in out


def test_without_channel_question_returns_to_the_model(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch, remembered={"topic": "AI", "depth": "3"})
    out = _invoke(None)  # no _workflow_bg and no TUI session in tests
    assert "from the last run" in out          # old relay behavior intact


def test_typoed_trigger_message_still_reaches_the_offer(tmp_path, monkeypatch):
    """A model-routed call carries the raw user message ("run wf imputs" —
    typo'd, so the loop's deterministic trigger missed it). The command
    phrase must be stripped before extraction, so the reuse offer still
    pauses instead of the workflow silently starting on garbage input."""
    _setup(tmp_path, monkeypatch, remembered={"topic": "AI", "depth": "3"})
    ch = _Channel(["yes"])
    out = _invoke(ch, last_user_message="run wf imputs please")
    assert len(ch.pauses) == 1
    assert "from the last run" in ch.pauses[0][0]
    assert "from the last run" not in out


def test_pause_loop_is_capped(tmp_path, monkeypatch):
    """A channel that never provides usable input can't spin forever."""
    from botcircuits.agent.workflow import _MAX_INPUT_PAUSES
    _setup(tmp_path, monkeypatch)              # nothing remembered
    ch = _Channel([""] * (_MAX_INPUT_PAUSES + 5))
    out = _invoke(ch)
    assert len(ch.pauses) == _MAX_INPUT_PAUSES
    assert "please provide" in out             # parked back to the relay path
