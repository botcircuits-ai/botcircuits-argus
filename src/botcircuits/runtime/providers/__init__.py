"""Concrete agent-runtime providers.

  - `claude_code` — shells out to the host `claude` CLI headlessly; the
                    reference CLI impl (codex/openclaw reuse it via config).
  - `hermes`      — subclasses `claude_code` to drive `hermes -z` (oneshot),
                    overriding only where usage comes from (session store).
  - `inline`      — emits step directives for a host agent that executes
                    actions itself (the skill/`step_workflow` path).
  - `multiplex`   — routes per-agent steps to different runtimes.

The `native` runtime has no provider here: in-process runs are driven by
the agent loop itself (`agent.loop.Agent` + `agent.segments.SegmentRunner`),
which hands the engine its `run_segment` callback directly.

All satisfy `runtime.base.AgentRuntimeProvider`; the workflow engine treats
them identically.
"""
