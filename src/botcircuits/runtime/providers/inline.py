"""Inline / self runtime — the host agent performs each segment in-session.

When the agent reading the workflow-running skill IS the runtime (Claude
driving a Claude-hosted workflow, Hermes driving Hermes, …), there is no point
spawning a fresh CLI subprocess per segment: the host is already a capable
agent in an active session. The inline runtime lets the **host itself** perform
each action step, one at a time, while the deterministic engine still owns
every branch decision.

Mechanism — reuse the engine's own pause/resume:

  - The engine runs normally, but `InlineRuntime.run_segment` doesn't perform
    the action. It returns a *paused* `SegmentResult` whose `question` is an
    encoded marker describing the action + the values the host must report.
  - The engine treats that exactly like a user-interaction pause: it stops and
    hands back `paused_step` (the resume cursor) + the accumulated `slots`.
  - The `step_workflow` driver persists that cursor, decodes the marker, and
    prints the action for the host agent to perform in its own session.
  - The host performs the action with its own tools, then re-invokes the
    driver with the observed values. The driver seeds them so the SAME segment
    re-runs returning those values (no pause), the engine consumes them and
    advances to the next segment — which pauses again, or the workflow ends.

Reusing pause/resume means the resume cursor + slot persistence are the
engine's existing, tested machinery — the inline runtime adds no new control
flow. Slot resolution stays deterministic in-process (Tier-0); anything it
can't fill surfaces to the host as the engine's own clarification question.
No LLM subprocess is ever spawned.
"""

from __future__ import annotations

import json
from typing import Any

from botcircuits.runtime.base import AgentRuntimeProvider, EventSink
from botcircuits.agent.workflow.engine.runner import SegmentResult


#: Prefix that marks a paused `SegmentResult.question` as an inline ACTION
#: hand-off (vs. a real user-facing question). The driver strips it and decodes
#: the JSON payload that follows.
ACTION_MARKER = "__BOTCIRCUITS_INLINE_ACTION__:"


def encode_action(payload: dict) -> str:
    """Encode an inline action hand-off into a pause `question` string."""
    return ACTION_MARKER + json.dumps(payload, ensure_ascii=False)


def decode_action(question: str) -> dict | None:
    """Decode an inline action hand-off, or None if `question` is a real
    user-facing question (not an inline marker)."""
    if not isinstance(question, str) or not question.startswith(ACTION_MARKER):
        return None
    try:
        return json.loads(question[len(ACTION_MARKER):])
    except (ValueError, TypeError):
        return None


class InlineRuntime(AgentRuntimeProvider):
    """A runtime whose `run_segment` hands each segment to the host agent.

    Two modes, set per call by the driver:

      - **handoff mode** (default): `run_segment` returns a paused
        `SegmentResult` carrying the action payload, so the engine stops and
        yields the resume cursor.
      - **seed mode**: after the host performs the action, the driver calls
        `seed_result(...)`; the next `run_segment` returns those observed
        values (no pause) so the engine consumes them and advances.
    """

    name = "self"

    def __init__(self) -> None:
        self._seeded: SegmentResult | None = None

    def seed_result(self, result: SegmentResult) -> None:
        """Pre-load the host's observed values for the pending segment so the
        next `run_segment` returns them instead of pausing."""
        self._seeded = result

    async def run_segment(
        self,
        *,
        actions: list[str],
        branch_variables: list[dict],
        system_notes: list[str],
        slots: dict[str, Any],
        item_variables: list[dict] | None = None,
        data_variables: list[dict] | None = None,
        event_sink: EventSink | None = None,
    ) -> SegmentResult:
        # Seed mode: the host already performed THIS segment; return its values
        # once, then clear so the following segment hands off again.
        if self._seeded is not None:
            result = self._seeded
            self._seeded = None
            return result

        # Hand-off mode: pause the engine and surface the action to the host.
        payload = {
            "actions": list(actions),
            "branch_variables": list(branch_variables),
            "item_variables": list(item_variables or []),
            "data_variables": list(data_variables or []),
            "system_notes": list(system_notes),
        }
        return SegmentResult(paused=True, question=encode_action(payload))

    async def resolve_slots(
        self,
        *,
        flow: dict,
        step_id: str,
        variables: list[dict],
        slots: dict[str, Any],
    ) -> dict[str, Any]:
        # Deterministic Tier-0 only — no LLM subprocess. Anything Tier-0 can't
        # fill stays empty; the engine's clarification path surfaces it to the
        # host as a real question.
        from botcircuits.agent.workflow.slot_resolver import resolve_slots as tier0

        last_user = (
            slots.get("__last_user_message__", "")
            if isinstance(slots, dict) else ""
        )
        resolved, _ = tier0(
            flow=flow,
            step_id=step_id,
            variables=variables,
            raw_args={},
            saved_slots=slots,
            last_user_message=last_user,
        )
        return resolved


__all__ = ["InlineRuntime", "ACTION_MARKER", "encode_action", "decode_action"]
