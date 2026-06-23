"""Workflow flow engine.

Runs inside the agent process. Only two step types are supported:

  - `start`        — no-op pass-through to `next`.
  - `agentAction`  — surfaces an action instruction the LLM must perform,
                     then pauses the workflow. If the step carries
                     `conditions`/`choices`, branching is evaluated on
                     RE-ENTRY (after the LLM has had a chance to fill
                     variables via tool args).

Everything else (`message`, `prompt`, `ai_actions`, `webhook`, `codehook`,
`journey`, `human_support`, `pause`, `choice`, …) is intentionally
omitted. Branching lives inside `agentAction` itself — there is no
separate `choice` step type — so workflows are deterministic
agent-action graphs with inline routing.
"""

from __future__ import annotations

from botcircuits.agent.workflow.engine.executor import run_flow

__all__ = ["run_flow"]
