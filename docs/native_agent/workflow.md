# Workflow (`agent/workflow/`)

Workflows are BotCircuits' core primitive: a deterministic state machine
drives multi-step work; the LLM is a subroutine, not the driver.

## Loading & exposure

Workflows live on disk (`$BOTCIRCUITS_WORKFLOWS_DIR` or
`.botcircuits/workflows`). `fetch_workflows` + `register_workflows` turn each
one into a `LocalTool` (marked with `_workflow_state`) the model can trigger
like any other tool.

## The engine (`workflow/engine/`)

Inversion of control: once a workflow tool fires, the ENGINE owns the loop
(`run_workflow_engine` in `engine/runner.py`). It walks the compiled
branch-delimited segments, calls the agent's `run_segment` callback once per
segment (see [segments.md](segments.md)), captures the reported branch slots,
evaluates the branch **deterministically**, and advances itself. The model
can't skip a step, reorder, or imitate stale history.

Control returns to the conversational loop on exactly two events: workflow
end (`done` + summary) and a user-interaction pause (`question`). A paused
workflow is resumed by the loop directly on the user's next message — no
model decision involved.

Supporting pieces: `item_resolver` / `tier0_resolver` / `slot_resolver`
(deterministic → semantic slot filling), `condition_processor` (branch
evaluation), `state` / `segments` (compilation), `result_render` (summaries).

## Authoring & quality

- `generator.py` + `build_workflow` tool — natural language → workflow JSON.
- `workflow_validator.py` — pure, no-LLM lint of a workflow document.
- `action_optimizer.py` / `graph_optimizer.py` / `workflow_defaults.py` —
  normalization passes applied at build time.
- `tracing/` — per-run session trace (steps, slots, memory graph).
- `evaluation/` — eval harness comparing prompt-only vs agent vs
  engine-driven execution (see its own README).

## Runtimes

The same engine can be hosted outside the native agent: the
`botcircuits/runtime` package adapts it to external CLI agents (claude-code,
hermes, …) via `run_workflow` / `step_workflow` entry points. Native runs
don't go through that seam — the in-process `Agent` supplies `run_segment`
directly.
