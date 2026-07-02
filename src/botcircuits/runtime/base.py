"""`AgentRuntimeProvider` — the abstract seam the workflow engine talks to.

A provider supplies the two capabilities the engine needs from an "agent":
running a segment's actions and resolving non-deterministic slot values. The
engine (`run_workflow_engine`) is unchanged; it receives a provider's bound
methods as the `run_segment` / `resolve_unfilled` callbacks it already
accepts.

The return type of `run_segment` is the engine's existing `SegmentResult`
(text, captured_slots, captured_items, paused, question) — every provider,
native or CLI, speaks that one shape so the engine can't tell them apart.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from botcircuits.agent.workflow.engine.runner import SegmentResult


@dataclass
class RuntimeConfig:
    """Resolved configuration for a runtime provider.

    `name` is the selected provider id (`native`, `claude-code`, …).
    `command` is the argv template for CLI providers — a list where the
    token ``{prompt}`` is replaced with the actual segment/extraction prompt
    (e.g. ``["claude", "-p", "{prompt}", "--output-format", "json"]``).
    `timeout` bounds a single CLI invocation.
    `cwd` is the working directory each CLI segment runs in. When set to the
    main agent's working directory, the spawned CLI inherits that project's
    `.claude/settings.json` permission rules (the policy the user already
    approved). When ``None``, each segment gets a fresh isolated temp dir.
    `allowed_tools` are extra tool names to grant the spawned CLI for this run
    (appended as ``--allowedTools …``). The runner fills this from "yes, allow
    it" replies to a permission-style pause, so a granted tool sticks for the
    rest of the run without the user editing settings.
    """

    name: str
    command: list[str] = field(default_factory=list)
    timeout: float = 600.0
    cwd: str | None = None
    allowed_tools: list[str] = field(default_factory=list)


#: The event-sink callable the streaming path threads through so a segment's
#: text/tool events still reach the UI. CLI providers in headless one-shot
#: mode have nothing incremental to emit and simply ignore it.
EventSink = Callable[[str, Any], Awaitable[None]]


class AgentRuntimeProvider(ABC):
    """One source of agent intelligence for the workflow engine."""

    #: Selected provider id, surfaced in logs and the running-skill summary.
    name: str = "base"

    @abstractmethod
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
        """Perform one segment's actions and report what it observed.

        Mirrors the engine's `SegmentRunner` protocol exactly so a bound
        method drops straight in as the engine's `run_segment` callback.

        `agent`, when given, is the named agent (`agents.<name>` in the
        workflow doc) this segment is pinned to — a different model/runtime
        than the run's default. `None` means the run's default agent/model.
        A provider that doesn't support per-agent overrides may ignore it.
        """
        raise NotImplementedError

    @abstractmethod
    async def resolve_slots(
        self,
        *,
        flow: dict,
        step_id: str,
        variables: list[dict],
        slots: dict[str, Any],
    ) -> dict[str, Any]:
        """Backfill branch `variables` still empty after Tier-1 capture.

        Mirrors the engine's `resolve_unfilled` hook signature. Returns a
        ``{variableName: value}`` dict of whatever it could satisfy; the
        engine merges it before evaluating the branch.
        """
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release any provider-held resources. No-op by default."""
        return None
