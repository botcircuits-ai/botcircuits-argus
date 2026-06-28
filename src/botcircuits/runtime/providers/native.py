"""Native runtime provider — the in-process BotCircuits agent loop.

A thin adapter, NOT a rewrite. It forwards the two engine callbacks to the
exact methods the engine used to receive directly:

  - `run_segment`    → `Agent._run_segment` (the cache-stable segment loop in
                       `agent/core.py`).
  - `resolve_slots`  → the Tier-0/Tier-2 closure built by
                       `agent.workflow._make_resolve_unfilled`.

Because it delegates to the existing code unchanged, the native path is
behavior-preserving: wiring the engine through this provider must produce the
same results as before. This is what keeps the refactor zero-regression and
lets `native` stay as the offline / CI fallback.
"""

from __future__ import annotations

from typing import Any

from botcircuits.runtime.base import AgentRuntimeProvider, EventSink
from botcircuits.agent.workflow.engine.runner import SegmentResult


class NativeRuntime(AgentRuntimeProvider):
    """Wrap a live `Agent` so the workflow engine can drive it as a provider."""

    name = "botcircuits"

    def __init__(self, agent, *, normalize_enabled: bool = True):
        # `agent` is a started `agent.core.Agent`. We hold it (not a copy) so
        # `_run_segment` reuses its tools / skills / MCP wiring.
        self._agent = agent
        # Build the Tier-0/Tier-2 backfill closure once, bound to the agent's
        # provider. Same factory the workflow tool used in-process, so slot
        # resolution behavior is identical.
        from botcircuits.agent.workflow import _make_resolve_unfilled

        self._resolve = _make_resolve_unfilled(
            provider=getattr(agent, "provider", None),
            normalize_enabled=normalize_enabled,
        )

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
        # Snapshot the provider's cumulative usage before the segment so we can
        # attribute exactly the tokens THIS segment billed (the agent loop's
        # `record_usage` only tracks a session total). The delta becomes the
        # SegmentResult's per-step usage the engine folds into the run total.
        before = self._usage_snapshot()
        seg = await self._agent._run_segment(
            actions=actions,
            branch_variables=branch_variables,
            system_notes=system_notes,
            slots=slots,
            item_variables=item_variables,
            data_variables=data_variables,
            event_sink=event_sink,
        )
        seg.usage = self._usage_delta(before)
        return seg

    def _usage_snapshot(self) -> dict[str, int]:
        """Current cumulative token counters on the agent's provider, or zeros
        when there is no provider / it doesn't track usage."""
        p = getattr(self._agent, "provider", None)
        return {
            "input": int(getattr(p, "usage_input_tokens", 0) or 0),
            "output": int(getattr(p, "usage_output_tokens", 0) or 0),
            "cache_read": int(getattr(p, "usage_cache_read_tokens", 0) or 0),
            "cache_write": int(getattr(p, "usage_cache_write_tokens", 0) or 0),
            "calls": int(getattr(p, "usage_llm_calls", 0) or 0),
        }

    def _usage_delta(self, before: dict[str, int]):
        """ActionUsage for the tokens billed since `before`, or None when the
        segment made no LLM call (e.g. a deterministic systemAction)."""
        from botcircuits.usage.run_usage import ActionUsage

        now = self._usage_snapshot()
        d = {k: max(0, now[k] - before[k]) for k in now}
        if not (d["input"] or d["output"] or d["calls"]):
            return None
        return ActionUsage(
            runtime=self.name,
            input_tokens=d["input"],
            output_tokens=d["output"],
            cache_read_tokens=d["cache_read"],
            cache_write_tokens=d["cache_write"],
            calls=d["calls"],
        )

    async def resolve_slots(
        self,
        *,
        flow: dict,
        step_id: str,
        variables: list[dict],
        slots: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._resolve(
            flow=flow, step_id=step_id, variables=variables, slots=slots,
        )


__all__ = ["NativeRuntime"]
