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
from botcircuits.providers import make_provider
from botcircuits.providers.base import LLMProvider


class NativeRuntime(AgentRuntimeProvider):
    """Wrap a live `Agent` so the workflow engine can drive it as a provider."""

    name = "native"

    def __init__(
        self,
        agent,
        *,
        normalize_enabled: bool = True,
        agents_config: dict[str, dict] | None = None,
    ):
        # `agent` is a started `agent.core.Agent`. We hold it (not a copy) so
        # `_run_segment` reuses its tools / skills / MCP wiring.
        self._agent = agent
        # Agent name -> {"provider": "openai", "model": "..."} for agents
        # routed to THIS runtime (native). A segment pinned to one of these
        # gets its own `LLMProvider` instead of the agent's default. Built
        # lazily and cached by (provider, model) so two agent names sharing
        # a binding reuse one client instance.
        self._agents_config = agents_config or {}
        self._provider_cache: dict[tuple[str, str | None], LLMProvider] = {}
        # Build the Tier-0/Tier-2 backfill closure once, bound to the agent's
        # provider. Same factory the workflow tool used in-process, so slot
        # resolution behavior is identical.
        from botcircuits.agent.workflow import _make_resolve_unfilled

        self._resolve = _make_resolve_unfilled(
            provider=getattr(agent, "provider", None),
            normalize_enabled=normalize_enabled,
        )

    def _resolve_provider(self, agent: str | None) -> LLMProvider | None:
        """The `LLMProvider` a segment pinned to `agent` should use, or None
        to fall back to the wrapped Agent's default provider."""
        cfg = self._agents_config.get(agent) if agent else None
        if not cfg:
            return None
        kind = cfg.get("provider", "anthropic")
        model = cfg.get("model")
        key = (kind, model)
        cached = self._provider_cache.get(key)
        if cached is None:
            cached = make_provider(kind, model)
            self._provider_cache[key] = cached
        return cached

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
        provider_override = self._resolve_provider(agent)
        # Snapshot the ACTUAL provider this call will use (the override when
        # this segment is pinned to a different agent, else the agent's
        # default) so the usage delta below attributes tokens to whichever
        # one really billed them, not always the agent's default.
        active_provider = provider_override or getattr(self._agent, "provider", None)
        before = self._usage_snapshot(active_provider)
        seg = await self._agent._run_segment(
            actions=actions,
            branch_variables=branch_variables,
            system_notes=system_notes,
            slots=slots,
            item_variables=item_variables,
            data_variables=data_variables,
            provider=provider_override,
            event_sink=event_sink,
        )
        seg.usage = self._usage_delta(active_provider, before)
        if seg.usage is not None and agent:
            seg.usage.agent = agent
        return seg

    def _usage_snapshot(self, provider) -> dict[str, int]:
        """Current cumulative token counters on `provider`, or zeros when
        there is no provider / it doesn't track usage."""
        p = provider
        return {
            "input": int(getattr(p, "usage_input_tokens", 0) or 0),
            "output": int(getattr(p, "usage_output_tokens", 0) or 0),
            "cache_read": int(getattr(p, "usage_cache_read_tokens", 0) or 0),
            "cache_write": int(getattr(p, "usage_cache_write_tokens", 0) or 0),
            "calls": int(getattr(p, "usage_llm_calls", 0) or 0),
        }

    def _usage_delta(self, provider, before: dict[str, int]):
        """ActionUsage for the tokens billed since `before`, or None when the
        segment made no LLM call (e.g. a deterministic systemAction)."""
        from botcircuits.usage.run_usage import ActionUsage

        now = self._usage_snapshot(provider)
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
