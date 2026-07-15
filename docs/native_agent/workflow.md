# Workflow (`agent/workflow/`)

Workflows are BotCircuits' core primitive: a deterministic state machine
drives multi-step work; the LLM is a subroutine, not the driver.

```
 .botcircuits/workflows/*.json ──► one LocalTool per workflow
                                          │ model triggers it
                                          ▼
                            run_workflow_engine — THE ENGINE OWNS THE LOOP
                                          │
        ┌─────────────────────────────────┤ walk compiled segments
        ▼                                 │
   run_segment (LLM, once per segment)    │
        │ captured slots / item facts     │
        ▼                                 │
   evaluate branch DETERMINISTICALLY ─────┘ advance to the next segment
        │
        ├── question step ──► EngineResult(paused, question)
        │                          │ surfaced as the reply; the user's
        │                          │ next message AUTO-RESUMES (no model
        │                          ▼ decision needed)
        │                     conversational loop
        │
        └── end ──► EngineResult(done, summary) ──► model relays it
```

## Loading & exposure

Workflows live on disk (`$BOTCIRCUITS_WORKFLOWS_DIR` or
`.botcircuits/workflows`). `fetch_workflows` + `register_workflows` turn each
one into a `LocalTool` (marked with `_workflow_state`) the model can trigger
like any other tool.

Entry is defense-in-depth, ending deterministic: the tool description and a
`## Workflows` system-prompt block (`workflows_system_prompt`) tell the model
to call the tool immediately, and — regardless of what the model does — the
loop itself matches explicit "run <workflow>" requests
(`match_workflow_trigger`) and invokes the tool before any provider call.

## Initial inputs — collected deterministically, before the first segment

Variables marked `"input": true` in `flow.variables` are the values the
USER must supply. On a fresh run the engine settles them before any
segment executes:

1. **Resolve from the conversation at hand** — the trigger args and the
   last user message go through the same Tier-0/Tier-2 hook branch
   variables use ("run deep_research on AI in finance, 3 pages" fills
   `topic` and `research_depth` without asking).
2. **Ask once for the rest** — still-missing inputs pause the run with ONE
   question built from the authored descriptions (no LLM involved). The
   user's reply resolves through the same hook on auto-resume; collected
   values persist across the pause.

Without the marker there is no pre-start collection — and without this
stage, a segment model facing empty inputs improvises its own
`human_feedback` ask whose answer never lands in the slots (the re-ask
loop). The workflow builder is instructed to mark user-supplied variables
and never author steps that ask for them.

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
