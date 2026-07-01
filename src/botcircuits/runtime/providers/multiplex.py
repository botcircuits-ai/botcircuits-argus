"""Multiplex runtime — route a segment to the runtime its pinned agent uses.

A workflow's `agents` map can name agents backed by DIFFERENT host runtimes
(one step on `claude-code`, another on `codex`) as well as different models
within the SAME runtime. Each concrete `AgentRuntimeProvider` (claude-code,
native, …) already resolves a *model* override per call via its own
`agents_config` (see `ClaudeCodeRuntime`/`NativeRuntime`); this class is the
one layer above that picks which RUNTIME INSTANCE handles a given segment in
the first place.

Only one instance per distinct runtime TYPE referenced by a workflow's
`agents` map is built (not one per agent) — agents that share a runtime type
share that instance and differentiate on model internally.
"""

from __future__ import annotations

from typing import Any

from botcircuits.runtime.base import AgentRuntimeProvider, EventSink
from botcircuits.agent.workflow.engine.runner import SegmentResult


class MultiplexRuntime(AgentRuntimeProvider):
    """Dispatch `run_segment` to whichever runtime instance a segment's
    `agent` is bound to; everything else falls through to `default`."""

    name = "multiplex"

    def __init__(
        self,
        *,
        default: AgentRuntimeProvider,
        by_runtime: dict[str, AgentRuntimeProvider],
        agent_runtime: dict[str, str],
    ):
        # The run's default runtime instance — used for steps with no
        # `agent` and for Tier-2 slot resolution (always the default; a
        # branch backfill isn't scoped to a particular agentAction's agent).
        self.default = default
        # runtime name -> its instance, one per DISTINCT runtime type
        # actually referenced by the workflow's `agents` map.
        self.by_runtime = by_runtime
        # agent name -> which runtime type it's routed to.
        self.agent_runtime = agent_runtime

    def _instance_for(self, agent: str | None) -> AgentRuntimeProvider:
        if not agent:
            return self.default
        runtime_name = self.agent_runtime.get(agent)
        if runtime_name is None:
            return self.default
        return self.by_runtime.get(runtime_name, self.default)

    async def run_segment(
        self,
        *,
        actions: list[str],
        branch_variables: list[dict],
        system_notes: list[str],
        slots: dict[str, Any],
        item_variables: list[dict] | None = None,
        data_variables: list[dict] | None = None,
        agent: str | None = None,
        event_sink: EventSink | None = None,
    ) -> SegmentResult:
        inst = self._instance_for(agent)
        return await inst.run_segment(
            actions=actions,
            branch_variables=branch_variables,
            system_notes=system_notes,
            slots=slots,
            item_variables=item_variables,
            data_variables=data_variables,
            agent=agent,
            event_sink=event_sink,
        )

    async def resolve_slots(
        self,
        *,
        flow: dict,
        step_id: str,
        variables: list[dict],
        slots: dict[str, Any],
    ) -> dict[str, Any]:
        # Tier-2 backfill always goes through the default runtime — it's a
        # branch-level fallback, not scoped to a particular agentAction step
        # (and therefore not to that step's agent).
        return await self.default.resolve_slots(
            flow=flow, step_id=step_id, variables=variables, slots=slots,
        )

    async def aclose(self) -> None:
        # Every distinct instance exactly once (by_runtime values may repeat
        # the default when an agent explicitly names the run's own runtime).
        seen: set[int] = set()
        for inst in [self.default, *self.by_runtime.values()]:
            if id(inst) in seen:
                continue
            seen.add(id(inst))
            await inst.aclose()


__all__ = ["MultiplexRuntime"]
