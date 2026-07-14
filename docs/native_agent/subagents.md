# Subagents (`agent/subagents.py`)

Split work into isolated, parallel sub-loops. A subagent is a fresh
`Agent` with its own context window, spawned for one bounded subtask
(≤ 25 rounds). It returns **only the answer, not its transcript**, so
the parent's window stays clean.

```
 parent Agent (conversation stays clean)
   │
   ├── delegate("summarize src/") ──► ┌ fresh Agent ┐
   │                                  │ own context │──► answer only
   │                                  │ filtered    │    (no transcript)
   │                                  └ tools ──────┘
   │
   └── fan_out(["audit a", "audit b", "audit c"])
             │
             ├──► subagent 1 ─┐        ≤ 4 in parallel,
             ├──► subagent 2 ─┼──►     order preserved:
             └──► subagent 3 ─┘        [subtask 1] … result
                                       [subtask 2] … result
                                       [subtask 3] error: … (isolated)
```

## Isolation contract

A subagent shares the parent's provider and permission rules, and a
*filtered* view of its tools — excluding:

- workflow tools (a subtask must not silently advance a workflow)
- `human_feedback` / `plan_and_confirm` (a subagent can't talk to the user)
- `delegate` / `fan_out` (no recursive spawning)
- `build_workflow` (authoring is the parent's call)

Everything else — file tools, shell, web, MCP tools, skills — passes
through, still gated by the same permission set.

## Surface

- `run_subagent(task, provider=..., tools=...)` — one isolated subtask,
  returns the final answer.
- `fan_out(tasks, ...)` — subtasks in parallel (≤ 4 concurrent), order
  preserved; one subtask's exception becomes its error string instead of
  killing the batch.
- `delegate` / `fan_out` **tools** — registered automatically by
  `Agent.start()` (opt out with `enable_subagents=False`), bound to the
  live parent so subagents see the final merged registry. `fan_out`
  results come back labeled (`[subtask N] …`) and ordered, so the model
  reads them as one block.

Use for work that splits cleanly: reading many files, research sweeps,
independent summaries — anything where the parent needs the conclusion,
not the process.
