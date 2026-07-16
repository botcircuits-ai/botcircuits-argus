"""Agent-runtime providers — the seam between the workflow engine and the
"intelligence" that performs an action step or resolves a non-deterministic
slot value.

The workflow engine (`agent.workflow.engine.runner.run_workflow_engine`) is
already decoupled from any specific LLM loop: it drives the deterministic
state machine and calls back out for exactly two capabilities —

  1. `run_segment` — perform one branch-delimited segment's action(s) and
     report the branch slots / per-item facts it observed (or pause for the
     user).
  2. `resolve_slots` — backfill branch variables a segment left empty
     (Tier-0 deterministic, then Tier-2 semantic extraction).

An `AgentRuntimeProvider` packages those two capabilities behind one
interface so the engine doesn't care WHERE the intelligence comes from:

  - `native`      — the in-process BotCircuits agent loop (`agent.loop.Agent`),
                    which hands the engine its callbacks directly (no
                    provider class needed). Kept as the default fallback.
  - `claude-code` — shell out to the host `claude` CLI headlessly, one
                    process per segment, capturing JSON on stdout. No SDK
                    binding; the host's own tools/MCP do the real work.
  - (codex, openclaw, … — added later behind the same base.)

`select_runtime()` is the single entry point: explicit config first
(`.botcircuits/settings.json` `runtime`, or `$BOTCIRCUITS_RUNTIME`), then
env-marker + binary-probe auto-detection, then `native` as the default.
"""

from __future__ import annotations

from botcircuits.runtime.base import AgentRuntimeProvider, RuntimeConfig
from botcircuits.runtime.detect import detect_runtime_name, select_runtime

__all__ = [
    "AgentRuntimeProvider",
    "RuntimeConfig",
    "detect_runtime_name",
    "select_runtime",
]
