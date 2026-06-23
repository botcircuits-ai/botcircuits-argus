"""Tracing adapters that observe a run without changing the engine.

The deterministic engine calls two provider methods: ``run_segment`` (perform a
step's action on the sub-agent) and ``resolve_slots`` (Tier-2 extraction). We
wrap a provider so each call emits trace events — capturing the action input,
the sub-agent output, the wall-clock duration, and the slot snapshot — then
delegates to the real provider untouched. The engine and the providers stay
oblivious to tracing; only this thin decorator knows about it.

`step_enter` and `branch` events come from the engine's own ``event_sink``
(see ``runner``); this module owns the action + slot-resolution events, which
are the ones that carry sub-agent I/O and timing.
"""

from __future__ import annotations

from typing import Any

from botcircuits.agent.workflow.tracing import EventType, SessionTrace, timer
from botcircuits.runtime.base import AgentRuntimeProvider, EventSink
from botcircuits.agent.workflow.engine.runner import SegmentResult


def _segment_output(seg: SegmentResult) -> dict[str, Any]:
    """A serializable view of what the sub-agent returned for a segment."""
    out: dict[str, Any] = {
        "text": seg.text,
        "captured_slots": dict(seg.captured_slots or {}),
        "captured_items": list(seg.captured_items or []),
        "paused": bool(seg.paused),
        "question": seg.question or "",
        # Tool(s) a permission-style pause was blocked on — recorded so a
        # trace shows WHY a segment paused (and what a "yes" reply granted),
        # not just the question text.
        "needs_tool": list(seg.needs_tool or []),
    }
    # Real token usage this segment billed, when the runtime reported it.
    # Recorded on the action event so the trace UI can overlay per-step tokens
    # (the same way it overlays per-step duration). Absent when the runtime
    # reports nothing.
    usage = getattr(seg, "usage", None)
    if usage is not None:
        out["usage"] = usage.to_dict()
    return out


class _TracingProvider(AgentRuntimeProvider):
    """Decorator over a real provider that traces action + slot-resolution
    calls into a :class:`SessionTrace`."""

    def __init__(self, inner: AgentRuntimeProvider, trace: SessionTrace):
        self._inner = inner
        self._trace = trace
        self.name = getattr(inner, "name", "provider")

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
        # The action(s) about to run on the sub-agent, plus the slot context
        # at this exact moment — the "input" half of the sub-agent execution.
        self._trace.event(
            EventType.ACTION_BEFORE,
            slots=slots,
            data={
                "actions": list(actions),
                "branch_variables": [v.get("variableName") for v in branch_variables],
                "data_variables": [
                    v.get("variableName") for v in (data_variables or [])
                ],
                "runtime": self.name,
            },
        )
        t = timer()
        seg = await self._inner.run_segment(
            actions=actions,
            branch_variables=branch_variables,
            system_notes=system_notes,
            slots=slots,
            item_variables=item_variables,
            data_variables=data_variables,
            event_sink=event_sink,
        )
        self._trace.event(
            EventType.ACTION_AFTER,
            slots=slots,
            duration_ms=t.ms(),
            data={
                "input": {"actions": list(actions)},
                "output": _segment_output(seg),
                "runtime": self.name,
            },
        )
        return seg

    async def resolve_slots(
        self,
        *,
        flow: dict,
        step_id: str,
        variables: list[dict],
        slots: dict[str, Any],
    ) -> dict[str, Any]:
        t = timer()
        resolved = await self._inner.resolve_slots(
            flow=flow, step_id=step_id, variables=variables, slots=slots,
        )
        self._trace.event(
            EventType.SLOT_RESOLVE,
            step=step_id,
            slots={**slots, **(resolved or {})},
            duration_ms=t.ms(),
            data={
                "requested": [v.get("variableName") for v in variables],
                "resolved": dict(resolved or {}),
                "runtime": self.name,
            },
        )
        return resolved or {}

    async def aclose(self) -> None:
        await self._inner.aclose()


def traced_provider(
    inner: AgentRuntimeProvider, trace: SessionTrace | None,
) -> AgentRuntimeProvider:
    """Wrap `inner` for tracing when `trace` is given; otherwise return it
    unchanged so a no-trace path pays nothing."""
    if trace is None:
        return inner
    return _TracingProvider(inner, trace)


__all__ = ["traced_provider"]
