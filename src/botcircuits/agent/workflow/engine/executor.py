"""Flow executor — walks the workflow definition step-by-step.

The step-kind discriminator is the top-level `step.type` field. Four
values are recognized:

  - `start`        → no-op, fall through to `next`.
  - `agentAction`  → emit the action payload; the workflow pauses. If
                     the step carries `conditions`/`choices`, the
                     branch is evaluated on RE-ENTRY (the next time the
                     workflow tool is called, after the LLM has had a
                     chance to fill variables via tool args).
  - `question`     → like `agentAction`, but the emitted payload is
                     tagged `kind: "question"` so the tool wrapper forces
                     a `human_feedback` call (which pauses the loop until
                     the user replies).
  - `systemAction` → NON-pausing bookkeeping. The engine records the
                     (slot-interpolated) action text as an audit note
                     and keeps walking — no LLM round-trip. Branching,
                     if any, is evaluated IMMEDIATELY against current
                     slots: by the time the walk reaches a systemAction,
                     slot values were already filled when the previous
                     pausing step re-entered, so there is nothing to
                     wait for. Accumulated notes ride on the next
                     pausing step's payload so the model (and the
                     transcript) still see the audit trail.

Branching lives on the step itself — there is no separate `choice`
step type. `conditions` and `choices` sit at the step root next to
`type` and `next` because they describe control flow, not step payload.
The executor records a `pendingBranch` marker on the saved session when
it pauses on a step with conditions, then resolves it on re-entry by
evaluating the choices against current slot values.

Anything else (`message`, `prompt`, `aiTask`, `choice`, …) raises so
unsupported steps don't silently do nothing.
"""

from __future__ import annotations

from typing import Any

from botcircuits.agent.workflow.engine.handlers.action import handle_action
from botcircuits.agent.workflow.engine.handlers.choice import evaluate_choices
from botcircuits.agent.workflow.engine.handlers.question import handle_question
from botcircuits.agent.workflow.engine.state import WorkflowStateContext
from botcircuits.agent.workflow.engine.utils import fill_text_with_slots

#: Upper bound on consecutive non-pausing steps walked in one call. A
#: systemAction cycle (a → b → a) would otherwise spin forever — there's
#: no LLM in the loop to break it.
_MAX_SYSTEM_CHAIN = 100


def _invoke_step(
    current_step_id: str,
    step: dict,
    event: dict,
) -> dict:
    next_step = step.get("next")
    settings = dict(step.get("settings") or {})
    # Thread the next-step id through so handlers can build qualified ids.
    settings["nextStepId"] = next_step

    event["stepId"] = current_step_id
    event["step"] = step
    event["settings"] = settings

    step_type = step.get("type")
    data: dict | None = None

    if step_type == "start":
        pass
    elif step_type in ("agentAction", "question"):
        handler = handle_question if step_type == "question" else handle_action
        response = handler(event)
        if response and response.get("fallbackStep"):
            next_step = response["fallbackStep"]
        if response and response.get("message"):
            data = response
    elif step_type == "systemAction":
        # Non-pausing: record the (slot-interpolated) action text as an
        # audit note and keep walking — no payload, no LLM round-trip.
        # Branching is evaluated IMMEDIATELY against current slots (filled
        # at the previous pausing step's re-entry); no pendingBranch dance.
        session_context = event["message"]["data"]["sessionContext"]
        note = fill_text_with_slots(settings.get("action") or "", session_context)
        if note.strip():
            event.setdefault("systemNotes", []).append(note.strip())
        if step.get("choices"):
            next_step = evaluate_choices(
                step["choices"], event["message"], next_step
            )
    else:
        raise ValueError(
            f"Local workflow engine does not support step type {step_type!r} "
            f"(step {current_step_id!r}). Supported types: 'start', "
            f"'agentAction', 'question', 'systemAction'. To branch, put "
            f"`conditions` on an agentAction. 'listDecision' and 'parallel' "
            f"require the segment-based runner (run_workflow_engine)."
        )

    return {"nextStep": next_step, "data": data}


def _resolve_pending_branch(
    saved_session: dict | None,
    flow: dict,
    message: dict,
) -> str | None:
    """If the prior turn paused on an agentAction with conditions, evaluate
    those conditions against the current slots and return the chosen
    next-step id. Returns None if there's no pending branch.
    """
    if not saved_session:
        return None
    pending = saved_session.get("pendingBranch")
    if not isinstance(pending, dict):
        return None
    step_id = pending.get("stepId")
    if not step_id:
        return None

    step = (flow.get("steps") or {}).get(step_id) or {}
    choices = step.get("choices") or []
    default_next = pending.get("defaultNext") or step.get("next")
    return evaluate_choices(choices, message, default_next)


async def run_flow(
    flow: dict,
    message: dict,
    start_step_id: str | None,
    journey_id: str,
) -> dict:
    """Execute the flow until a step yields a message (an
    `agentAction` pause) or the graph runs out of steps.

    Returns `{currentStepId, data, savedSession}`. `data` is the action
    payload to surface to the agent, or `None` if the workflow ended
    without producing one.
    """
    saved_session = message["data"].get("savedSession")

    # Re-entry: if the previous turn parked on an agentAction with
    # conditions, evaluate them now against the freshly-merged slots and
    # let the chosen branch override the recorded `currentStep`.
    branched = _resolve_pending_branch(saved_session, flow, message)
    if branched is not None:
        current_step_id = branched
    else:
        current_step_id = start_step_id or flow["start"]

    state_context = WorkflowStateContext(
        journey_id,
        saved_session,
        message["data"].get("sessionContext"),
    )
    # Clear any consumed branch marker so it doesn't fire again next turn.
    state_context.saved_session.pop("pendingBranch", None)

    event: dict[str, Any] = {
        "message": message,
        "step": None,
        "settings": None,
        "flow": flow,
        "workflowStateContext": state_context,
        # Audit notes accumulated by non-pausing systemAction steps; they
        # ride on the next pause's return so the model sees what the
        # engine recorded on its behalf.
        "systemNotes": [],
    }

    steps = flow.get("steps", {})
    walked = 0
    while current_step_id:
        walked += 1
        if walked > _MAX_SYSTEM_CHAIN:
            raise ValueError(
                f"Workflow walked {_MAX_SYSTEM_CHAIN} steps without pausing "
                f"— a systemAction cycle? (last step {current_step_id!r})"
            )
        step = steps.get(current_step_id)
        if step is None:
            raise ValueError(
                f"Workflow references unknown step id {current_step_id!r}"
            )
        state_context.set_running_step(current_step_id, step)
        result = _invoke_step(current_step_id, step, event)

        # If this step has conditions, defer branching to re-entry —
        # record the pending choice and walk to the static `next` for now.
        # (systemAction never lands here: it yields no data and resolved
        # its choices inline.)
        if result["data"]:
            if step.get("choices") or step.get("conditions"):
                state_context.saved_session["pendingBranch"] = {
                    "stepId": current_step_id,
                    "defaultNext": result["nextStep"],
                }

        current_step_id = result["nextStep"]
        state_context.set_current_step(current_step_id)

        if result["data"]:
            return {
                "currentStepId": current_step_id,
                "data": result["data"],
                "savedSession": state_context.saved_session,
                "systemNotes": event["systemNotes"],
            }

    return {
        "currentStepId": None,
        "data": None,
        "savedSession": state_context.saved_session,
        "systemNotes": event["systemNotes"],
    }
