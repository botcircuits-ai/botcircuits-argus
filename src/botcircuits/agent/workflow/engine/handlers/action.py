"""Agent-action step handler.

The executor returns the action payload directly and the workflow tool
feeds it back to the LLM. We support only one action sub-kind, so the
schema is a single top-level `step.type: "agentAction"`. The executor
routes by that value and this handler doesn't need to re-check it.
"""

from __future__ import annotations

from botcircuits.agent.workflow.engine.utils import get_next_step_for_prompt_action


def handle_action(event: dict) -> dict:
    message = event["message"]
    step_id = event["stepId"]
    step = event["step"]
    settings = event["settings"]
    workflow_state_context = event["workflowStateContext"]

    session_context = message["data"]["sessionContext"]
    journey_config = message["data"]["journeyConfig"]
    journey_id = session_context.get("journeyId")
    slots = session_context.get("slots", {})

    action = settings.get("action")
    next_step_id = get_next_step_for_prompt_action(journey_id, settings)

    variables = (
        journey_config.get("flow", {})
        .get("variables", [])
    )

    # NL `conditions` are author-facing; `choices` are the runtime form the
    # executor evaluates on re-entry. Both live at the step root alongside
    # `type` and `next` because they describe control flow, not step payload.
    conditions = [c for c in step.get("conditions", []) if c.get("condition")]
    choices = step.get("choices") or []
    # If there's no next step and no branchable conditions/choices, this
    # action ends the workflow once the LLM completes it.
    is_end = (
        not settings.get("nextStepId")
        and not conditions
        and not choices
    )

    slot_to_fill = "sys_agent_action_result"
    workflow_state_context.set_slot_to_fill(
        slot_to_fill, [], {"stepId": step_id}
    )

    payload = {
        "sessionId": message.get("sessionId"),
        "agentId": journey_id,
        "action": action,
        "stepId": step_id,
        "inputText": session_context.get("inputText"),
        "slots": slots,
        "slotToFill": slot_to_fill,
        "conditions": conditions,
        "choices": choices,
        "variables": variables,
        "end": is_end,
        "nextStep": next_step_id,
    }

    return {
        "message": {
            "type": "AGENT_ACTION",
            "content": payload,
            "end": is_end,
            "conditions": conditions,
            "choices": choices,
            "variables": variables,
        },
        "waitForPrompt": True,
    }
