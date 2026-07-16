# Orchestration (`agent/orchestration.py`)

Plan a task into steps, run them with checkpoints. A single model turn
is not a workflow.

```
 task ──► planner LLM ──► ["step 1", "step 2", …]   (2-4 steps; falls
                               │                     back to [task])
              ┌────────────────┘
              ▼  for each step, in order
        approve(step)? ── no ──► record "[skipped] …"
              │ yes
              ▼
        worker.chat(step) ─── raises? retry once, then record the error
              │                (ONE worker session — later steps see
              ▼                 earlier results)
        record result
              │
              ▼
 OrchestratorResult(plan, results, final)
```

`Orchestrator(provider, tools)` composes the `Agent` without touching it:

1. **Plan** — one planner LLM call splits the task into 2-4 short
   imperative steps (strict JSON array; unparseable output falls back to
   the whole task as one step). Tagged `plan` in the usage breakdown.
2. **Gate** — an optional `approve(step)` callback (sync or async) gates
   each step; a rejected step is recorded as `[skipped] …`, not dropped.
3. **Execute** — steps run in order through one fresh worker agent
   session (later steps see earlier results). The worker shares the
   caller's provider and tools but has workflows and subagent spawning
   disabled — a focused executor.
4. **Retry** — a step that raises is retried once before its error is
   recorded; the run continues.

`run()` returns `OrchestratorResult(plan, results, final)`.

CLI: `/plan <task>` runs the orchestrator with the live agent's provider
and tools and prints the plan and per-step results.

Relation to workflows: this is the *lightweight* end of the spectrum —
an ad-hoc plan for a one-off task. For durable, branching, deterministic
multi-step processes, BotCircuits workflows ([workflow.md](workflow.md))
are the heavyweight sibling: there a state machine, not a plan string,
owns advancement.
