"""Inline step driver for the workflow-running skill (self / host-agent mode).

Used when the host agent itself performs each workflow action in its current
session — no nested CLI subprocess. The host agent calls this once to get the
NEXT action, performs it, then calls again with what it observed, until the
workflow ends.

It drives the deterministic engine with the `InlineRuntime`: each call runs the
engine forward until the next action hand-off (or a real user question, or the
end), persisting the resume cursor + slots to
`.botcircuits/workflows/.runs/<name>.json` between calls.

Usage (the host agent runs these; it is NOT a user-facing UX):

    # Start (or restart) a run; prints the first action to perform.
    python -m botcircuits.runtime.step_workflow --name <wf> \\
        [--initial-args '{"k": "v"}']

    # After performing the action, report observed values; prints the next.
    python -m botcircuits.runtime.step_workflow --name <wf> \\
        --observed '{"slots": {"approved": true}}'

    # If a step asked the user a question, answer it the same way the host
    # would relay any reply, then continue:
    python -m botcircuits.runtime.step_workflow --name <wf> --reply "<answer>"

Output (stdout, one JSON object):
    {"status": "action",   "actions": [...], "report": {...}, "name": "<wf>"}
        → perform `actions`, then re-invoke with --observed matching `report`.
    {"status": "question", "question": "...", "name": "<wf>"}
        → ask the user, then re-invoke with --reply "<answer>".
    {"status": "done",     "summary": "...", "slots": {...}}
    {"status": "error",    "error": "..."}

`report` describes what to put in --observed: `{"slots": [<schema>],
"items": [<schema>]}` — slot fields for a branch step, item fields for a
list-decision step. Report only values you genuinely observed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from botcircuits.runtime.providers.inline import InlineRuntime, decode_action
from botcircuits.runtime.run_workflow import (
    _clear_state,
    _load_state,
    _save_state,
)
from botcircuits.agent.workflow.engine.runner import SegmentResult, run_workflow_engine
from botcircuits.agent.workflow.local import (
    LocalWorkflowError,
    _load_workflow_record,
)


def _schema_of(variables: list[dict]) -> list[dict]:
    out: list[dict] = []
    for v in variables:
        name = v.get("variableName")
        if not isinstance(name, str) or not name:
            continue
        out.append({
            "name": name,
            "type": v.get("dataType") or "string",
            "description": v.get("description") or "",
        })
    return out


async def _step(
    name: str,
    *,
    initial_args: dict,
    observed: dict | None,
    reply: str | None,
    restart: bool,
) -> dict:
    record = _load_workflow_record(name)
    flow = record.get("flow")
    if not isinstance(flow, dict):
        raise LocalWorkflowError(f"workflow {name!r} is missing flow")

    saved = {} if restart else _load_state(name)
    resume_step = saved.get("engine_paused_step")
    slots: dict[str, Any] = dict(saved.get("engine_slots") or {})

    # Fresh start: seed the trigger args.
    if resume_step is None and not saved:
        slots.update({k: v for k, v in initial_args.items() if v not in (None, "")})

    # A user's reply to a real question rides the reserved key the resolver and
    # the next segment hand-off read.
    if reply:
        slots["__last_user_message__"] = reply

    runtime = InlineRuntime()

    # If the host reported observed values for the pending segment, seed them so
    # the engine consumes that segment and advances to the next hand-off.
    if observed is not None:
        runtime.seed_result(SegmentResult(
            captured_slots=(observed.get("slots") or {})
                if isinstance(observed.get("slots"), dict) else {},
            captured_items=(observed.get("items") or [])
                if isinstance(observed.get("items"), list) else [],
            text=str(observed.get("text") or ""),
        ))

    result = await run_workflow_engine(
        flow,
        workflow_name=name,
        run_segment=lambda **kw: runtime.run_segment(**kw),
        start_step_id=resume_step,
        slots=slots,
        resolve_unfilled=lambda **kw: runtime.resolve_slots(**kw),
    )

    if result.paused:
        # Persist the resume cursor + slots for the next invocation.
        _save_state(name, {
            "engine_paused_step": result.paused_step or resume_step,
            "engine_slots": result.slots,
        })
        action = decode_action(result.question)
        if action is not None:
            # Inline action hand-off — tell the host what to do + report.
            return {
                "status": "action",
                "name": name,
                "actions": action.get("actions") or [],
                "report": {
                    "slots": _schema_of(action.get("branch_variables") or []),
                    "items": _schema_of(action.get("item_variables") or []),
                },
                "system_notes": action.get("system_notes") or [],
            }
        # Real user-facing question (a `question` step or clarification).
        return {"status": "question", "question": result.question, "name": name}

    _clear_state(name)
    clean = {k: v for k, v in (result.slots or {}).items()
             if not k.startswith("__")}
    return {"status": "done", "summary": result.summary, "slots": clean}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="botcircuits.runtime.step_workflow")
    parser.add_argument("--name", required=True, help="built workflow name")
    parser.add_argument("--initial-args", default="",
                        help="JSON object of initial slot values (start only)")
    parser.add_argument("--observed", default="",
                        help='JSON {"slots": {...}, "items": [...]} the host '
                             "observed for the pending action step")
    parser.add_argument("--reply", default=None,
                        help="user's answer to a prior question")
    parser.add_argument("--restart", action="store_true",
                        help="discard any saved run state and start fresh")
    args = parser.parse_args(argv)

    def _parse_obj(raw: str, flag: str) -> dict | None:
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            print(json.dumps({"status": "error",
                              "error": f"{flag} not valid JSON: {e}"}))
            raise SystemExit(2)
        if not isinstance(parsed, dict):
            print(json.dumps({"status": "error",
                              "error": f"{flag} must be a JSON object"}))
            raise SystemExit(2)
        return parsed

    try:
        initial_args = _parse_obj(args.initial_args, "--initial-args") or {}
        observed = _parse_obj(args.observed, "--observed")
    except SystemExit as e:
        return int(e.code or 2)

    try:
        out = asyncio.run(_step(
            args.name,
            initial_args=initial_args,
            observed=observed,
            reply=args.reply,
            restart=args.restart,
        ))
    except LocalWorkflowError as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        return 1
    except Exception as e:  # pragma: no cover - defensive top-level guard
        print(json.dumps({"status": "error",
                          "error": f"{type(e).__name__}: {e}"}))
        return 1

    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
