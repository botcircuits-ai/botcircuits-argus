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

## Initial inputs — args resolution & entity recognition, before the first segment

Variables marked `"input": true` in `flow.variables` are the values the
USER must supply. On EVERY fresh run the engine settles them before any
segment executes — the same pipeline handles both phrasings:

```
 "run deep_research_assistant                "run deep_research_assistant"
  on AI in finance, 3 pages"                  (values missing)
        │                                           │
        ▼                                           ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │ 1 match_workflow_trigger      run-verb + registered workflow name?  │
 │   (deterministic — no model)  the loop invokes the tool itself      │
 └───────────────────────────────────┬─────────────────────────────────┘
                                     ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │ 2 strip_workflow_trigger      command phrase removed — only INPUT   │
 │   "on AI in finance, 3 pages"  survives; a bare "run <name>" → ""   │
 │   (the command itself can never become a variable value)            │
 └───────────────────────────────────┬─────────────────────────────────┘
                                     ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │ 3 seed slots from call args   accepted ONLY for `input: true` vars  │
 │   (a model-issued call padding produced vars with "N/A" is dropped) │
 └───────────────────────────────────┬─────────────────────────────────┘
                                     ▼
 ┌── 4 resolve each unfilled input variable ───────────────────────────┐
 │                                                                     │
 │   Tier-0 — deterministic, zero tokens                               │
 │     raw args · authored choice-value match · typed extraction       │
 │     (number / yes-no) · saved session slots                         │
 │        │ still missing                                              │
 │        ▼                                                            │
 │   Tier-2 — cheap-model entity extraction (when a provider is set)   │
 │     "on AI in finance, 3 pages" ─► topic = "AI in finance"          │
 │                                    research_depth = "3 pages"       │
 └───────────────────────────────────┬─────────────────────────────────┘
                                     │
                all inputs filled? ── yes ──► first segment runs
                                     │        (no questions asked)
                                     │ no
                                     ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │ 5 remembered inputs — OFFER, never silent reuse                     │
 │   the last completed run's values (.last_inputs/<wf>.json) cover    │
 │   what's missing? pause and ASK (offered at most once per run):     │
 │     I have these values from the last run of deep_research_…:       │
 │     - topic: AI in finance                                          │
 │     - research_depth: 3 pages                                       │
 │     Reuse them? (yes / no / change <name> …)                        │
 │                                                                     │
 │   the pause carries selector options (yes · no · change <name>):    │
 │   the CLI prompt renders them as an ↑/↓ + Enter / numbered picker,  │
 │   so picking returns the canonical answer verbatim — typing a       │
 │   free-form reply instead is always possible                        │
 │                                                                     │
 │   reply, interpreted deterministically:                             │
 │     "yes" · "yes do same" · "ok" ► adopt all ──► first segment runs │
 │     "no" ──────────────────────► adopt none, ask fresh (step 6)     │
 │     "i want to change pages" ──► adopt the others; the mentioned    │
 │        variable (matched by name or description words) re-resolves  │
 │        from the reply itself ("change depth to 5 pages" needs no    │
 │        further ask), else is asked in step 6                        │
 │     anything else ─────────────► LLM classification: does the reply │
 │        pick one of the options anyway ("sounds good, run it again"  │
 │        ► yes)? if not, it's fresh values: the reply runs through    │
 │        step 4 extraction; nothing remembered adopted                │
 └───────────────────────────────────┬─────────────────────────────────┘
                                     │ still missing
                                     ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │ 6 PAUSE with ONE authored question (no LLM — built from the         │
 │   variable descriptions):                                           │
 │     To run deep_research_assistant, please provide:                 │
 │     - topic — The topic to research.                                │
 │     - research_depth — Desired depth/length, e.g. '3-5 pages'.      │
 └───────────────────────────────────┬─────────────────────────────────┘
                                     │ user replies "AI in finance, 3 pages"
                                     ▼
 ┌─────────────────────────────────────────────────────────────────────┐
 │ 7 AUTO-RESUME (no model decision) — the reply runs through the      │
 │   SAME Tier-0 → Tier-2 pipeline (step 4); collected values persist  │
 │   across pauses; anything still missing re-asks only for the rest   │
 └───────────────────────────────────┬─────────────────────────────────┘
                                     ▼
                first segment runs (Tier-1 record_slots capture
                continues inside the segments as before)
```

On completion, the run's input values are saved to
`<workflows dir>/.last_inputs/<workflow>.json` — that file is what powers
the next run's reuse offer. The offer state itself rides the ordinary
slots persistence (a reserved `__pending_reuse__` key), so the same
behavior holds on the CLI-runtime path with its on-disk run state.

So:

- `run deep_research_assistant on AI in finance, 3 pages` — steps 1-4 fill
  both inputs from the message; the workflow starts immediately.
- `run deep_research_assistant` (first ever run) — step 2 strips the
  message to "" (nothing to extract), step 6 asks for topic +
  research_depth in one question, and step 7 resolves the reply.
- `run deep_research_assistant` (ran before) — step 5 offers the previous
  values; "yes" starts immediately, "no" asks fresh, "change pages to 5"
  keeps the topic and re-resolves the depth from the reply.

Without the `input: true` marker there is no pre-start collection — and
without this stage, a segment model facing empty inputs improvises its own
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
