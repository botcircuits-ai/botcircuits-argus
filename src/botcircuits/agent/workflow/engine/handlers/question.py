"""Question step handler.

A `question` step asks the user for input before the workflow can
advance. It behaves like an `agentAction` for control-flow purposes
(it can carry `next` / `conditions` evaluated on re-entry), but the
emitted payload is tagged `kind: "question"` so the workflow tool can
instruct the model to route it through the `human_feedback` tool rather
than acting on it directly. That forced routing is what pauses the
agent loop until the user replies.
"""

from __future__ import annotations

from botcircuits.agent.workflow.engine.utils import get_next_step_for_prompt_action


def handle_question(event: dict) -> dict:
    message = event["message"]
    step_id = event["stepId"]
    step = event["step"]
    settings = event["settings"]
    workflow_state_context = event["workflowStateContext"]

    session_context = message["data"]["sessionContext"]
    journey_config = message["data"]["journeyConfig"]
    journey_id = session_context.get("journeyId")
    slots = session_context.get("slots", {})

    # The question text is authored on `settings.action` (same field as an
    # agentAction) so the build/index pipeline treats both uniformly.
    action = settings.get("action")
    next_step_id = get_next_step_for_prompt_action(journey_id, settings)

    variables = journey_config.get("flow", {}).get("variables", [])

    conditions = [c for c in step.get("conditions", []) if c.get("condition")]
    choices = step.get("choices") or []
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
        # Distinguishes this from a plain agentAction so the tool wrapper
        # can force a `human_feedback` call.
        "kind": "question",
    }

    return {
        "message": {
            "type": "AGENT_ACTION",
            "content": payload,
            "end": is_end,
            "conditions": conditions,
            "choices": choices,
            "variables": variables,
            "kind": "question",
        },
        "waitForPrompt": True,
    }
