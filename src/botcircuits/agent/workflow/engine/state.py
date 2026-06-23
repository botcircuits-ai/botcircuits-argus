"""Per-workflow execution state.

Tracks the current step id and the slots collected so far so a workflow
can be resumed across calls when it yields on an `agent_action`.
"""

from __future__ import annotations

from typing import Any


class WorkflowStateContext:
    def __init__(
        self,
        journey_id: str,
        saved_session: dict[str, Any] | None,
        session_context: dict[str, Any] | None,
    ):
        self.current_journey = journey_id
        self.saved_session: dict[str, Any] = saved_session or {
            "slots": {},
            "currentStep": None,
            "runningStep": None,
        }
        self.saved_session.setdefault("slots", {})
        self.session_context: dict[str, Any] = session_context or {
            "slots": {},
            "inputText": "",
        }

    # -- slot tracking ------------------------------------------------------

    def set_slot_to_fill(
        self,
        slot: str,
        possible_answers: list | None = None,
        flow_data: dict | None = None,
    ) -> None:
        """Mark the workflow as paused, waiting for `slot` to be filled.

        The engine doesn't actually validate or wait — the agent loop
        re-enters by calling the workflow tool again — but we still record
        the pause so a future call can pick up where we left off.
        """
        self.saved_session["slotToFill"] = {
            "journeyId": self.current_journey,
            "slot": slot,
            "possibleAnswers": possible_answers or [],
            "flowData": flow_data or {},
        }

    def set_current_step(self, current_step: str | None) -> None:
        self.saved_session["currentStep"] = current_step

    def set_running_step(self, running_step: str, step: dict | None) -> None:
        self.saved_session["runningStep"] = running_step
        if step:
            self.saved_session["runningBlock"] = (
                step.get("settings", {}).get("blockId")
            )
