"""The engine's OWN deterministic execs must surface on the stream.

A listDecision step that prices items inside the engine (itemSource +
itemFacts exec) runs the script directly — no LLM tool call. Those execs were
invisible to the stream, so Tool Correctness saw an empty tool sequence and
scored 0 even though the workflow really invoked the pricer. The engine now
emits a `tool_call`/`tool_result` pair per exec through the same sink the
segment runner uses; these tests pin that.
"""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

from botcircuits.agent.workflow.engine.runner import (
    SegmentResult,
    run_workflow_engine,
)
from botcircuits.agent.workflow.engine.segments import compute_segments


def _pricer(tmp_path: Path):
    (tmp_path / "bin").mkdir(exist_ok=True)
    script = tmp_path / "bin" / "price.py"
    script.write_text(
        "import sys, json\n"
        "inv={'SKU-A':{'price':10,'stock':100}}\n"
        "sku=sys.argv[1]; qty=int(sys.argv[2])\n"
        "rec=inv.get(sku)\n"
        "print(json.dumps({'found':False}) if not rec else "
        "json.dumps({'found':True,'stock':rec['stock'],"
        "'line_total':rec['price']*qty}))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _flow() -> dict:
    flow = {
        "start": "start",
        "variables": [],
        "steps": {
            "start": {"type": "start", "next": "decide"},
            "decide": {
                "type": "listDecision",
                "itemSource": {"file": "data/order.json", "path": "items"},
                "itemFacts": {
                    "kind": "exec",
                    "command": ["python3", "bin/price.py", "{sku}", "{qty}"],
                    "parse": "json",
                    "derive": {
                        "sku": {"from_item": "sku"},
                        "line_total": {"from_output": "line_total", "default": 0},
                    },
                },
                "decisionKey": "decision",
                "collectInto": "decisions",
                "emit": ["sku", "decision", "line_total"],
                "choices": [
                    {"operator": "AND", "expressionList": [
                        {"variable": "line_total", "operator": "greater than",
                         "value": 5000}],
                        "next": "review"},
                ],
                "defaultNext": "fulfill",
            },
        },
    }
    flow["segments"] = compute_segments(flow)
    return flow


def _run(tmp_path: Path, *, with_sink: bool):
    _pricer(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "order.json").write_text(json.dumps({"items": [
        {"sku": "SKU-A", "qty": 5},
        {"sku": "SKU-A", "qty": 2},
    ]}))

    events: list[tuple] = []

    async def sink(kind, payload):
        events.append((kind, payload))

    async def run_segment(**_kw):
        # The exec listDecision resolves items itself; any other segment is a
        # no-op here — we only care that the pricer execs surfaced as events.
        return SegmentResult(text="ok", captured_slots={})

    import os
    cwd = os.getcwd()
    os.chdir(tmp_path)  # engine's _BASE_DIR() is the process cwd
    try:
        asyncio.run(run_workflow_engine(
            _flow(), workflow_name="wf", run_segment=run_segment,
            event_sink=sink if with_sink else None,
        ))
    finally:
        os.chdir(cwd)
    return events


def test_engine_execs_emit_tool_events(tmp_path: Path):
    events = _run(tmp_path, with_sink=True)
    calls = [p for k, p in events if k == "tool_call"]
    results = [p for k, p in events if k == "tool_result"]
    # one pair per priced item
    assert len(calls) == 2 and len(results) == 2
    # tool name is the executed program, so Tool Correctness can match it
    assert all(c.name == "price.py" for c in calls)
    assert calls[0].arguments["argv"] == ["python3", "bin/price.py", "SKU-A", "5"]
    # result carries the pricer stdout, not an error
    tc, out, is_error = results[0]
    assert tc is calls[0] and not is_error and "line_total" in out


def test_no_sink_no_events_and_still_runs(tmp_path: Path):
    # Without a sink the run still completes (events simply aren't emitted).
    events = _run(tmp_path, with_sink=False)
    assert events == []
