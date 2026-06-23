"""S2 — engine-rendered final answer.

The model should never spend output tokens re-emitting the workflow's result:
the engine already holds the state, so the engine renders the answer. A workflow
declares `flow.result`; on completion the engine renders it from its own state
(slots, collected decision lists, or a file the workflow wrote) and returns it as
the summary. The terminal `emit_result` LLM step is then unnecessary.

`flow.result` forms (all optional — absent means fall back to the legacy
`<outcome>, slots {...}` summary line):

    {"kind": "from_file", "path": "data/decisions.json"}
        Read a JSON file the workflow produced (relative to the run cwd /
        workspace) and use its parsed content as the result. The most direct way
        to surface a result a step persisted, with zero model output.

    {"kind": "template", "value": {"customer": "{customer_id}", ...}}
        A JSON structure whose string leaves are slot-interpolated with
        `{slot_name}` placeholders. For results assembled purely from slots.

    {"kind": "slots", "keys": ["a", "b"]}
        Shorthand: a flat object of the named slot values.

Rendering never calls the LLM and never raises into the run: a malformed/missing
source degrades to the legacy summary so a result-render bug can't fail a
workflow.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from botcircuits.agent.workflow.engine.utils import fill_text_with_slots


import re

_WHOLE_SLOT_RE = re.compile(r"^\{(\w+)\}$")


def _interp(value: Any, slots: dict) -> Any:
    """Recursively render a template value against slots.

    A string that is EXACTLY a single `{slot}` reference injects that slot's
    raw value (preserving lists/numbers/objects), so a template like
    `{"decisions": "{decisions}"}` yields the actual decisions list, not its
    stringified form. Any other string is text-interpolated as usual.
    """
    if isinstance(value, str):
        m = _WHOLE_SLOT_RE.match(value)
        if m:
            return slots.get(m.group(1))
        return fill_text_with_slots(value, {"slots": slots})
    if isinstance(value, list):
        return [_interp(v, slots) for v in value]
    if isinstance(value, dict):
        return {k: _interp(v, slots) for k, v in value.items()}
    return value


def render_result(
    flow: dict,
    slots: dict,
    *,
    base_dir: Path | None = None,
) -> Any | None:
    """Render `flow.result` from engine state. Returns the result object, or
    None when no `result` is declared or it can't be rendered (caller then uses
    the legacy summary). Never raises."""
    spec = flow.get("result")
    if not isinstance(spec, dict):
        return None
    kind = spec.get("kind")
    try:
        if kind == "from_file":
            rel = spec.get("path")
            if not isinstance(rel, str):
                return None
            p = (base_dir / rel) if base_dir else Path(rel)
            return json.loads(p.read_text())
        if kind == "template":
            return _interp(spec.get("value"), slots)
        if kind == "slots":
            keys = spec.get("keys") or []
            return {k: slots.get(k) for k in keys if isinstance(k, str)}
    except Exception:
        return None
    return None


def persist_result(flow: dict, result: Any, *, base_dir: Path | None = None) -> None:
    """Write the engine-rendered result to `flow.result.persist` (a path), if
    declared. Lets an out-of-process consumer read the FULL answer from a file
    instead of a possibly display-truncated summary line. Best-effort; never
    raises into the run."""
    spec = flow.get("result")
    if not isinstance(spec, dict):
        return
    rel = spec.get("persist")
    if not isinstance(rel, str):
        return
    try:
        p = (base_dir / rel) if base_dir else Path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False))
    except Exception:
        pass


def result_summary_line(workflow_name: str, result: Any) -> str:
    """The conversational-history line carrying an engine-rendered result.

    The full result rides as a compact JSON payload so the conversational agent
    (and the eval harness, which prefers a written decisions file but also scans
    text) can read the finished answer without the model having produced it."""
    try:
        payload = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        payload = str(result)
    return f"workflow {workflow_name} completed: {payload}"
