# Segments (`agent/segments.py`)

Engine-driven workflow segment execution — the harness side of the
inversion of control described in [workflow.md](workflow.md).

Once a workflow starts, the *engine* owns the loop; the LLM is a subroutine
invoked once per branch-delimited segment. `SegmentRunner` (mixed into
`Agent`) implements that subroutine.

```
 workflow engine ──(actions, branch vars, slots)──► _run_segment
                                                        │
        ┌───────────────────────────────────────────────┘
        ▼
   ENGINE_SYSTEM_PROMPT (constant, cache-stable) + segment payload
        │
        ▼
   ┌─► provider call                                    (≤ 25 rounds)
   │        │
   │        ├── real tool calls ──► agent's tools ──► results ──┐
   │        │   (workflow tools excluded — the engine advances) │
   │        │                                                   │
   │        ├── record_slots / record_item_list ──► captured ───┤ terminal
   │        │                                                   │
   │        ├── human_feedback ──► paused=True, question ───────┤ terminal
   │        │                                                   │
   │        └── no tool calls ── terminal ──────────────────────┤
   │                                                            │
   └─────────────────────────────────◄──────────────────────────┘
                                                        │
                                                        ▼
   SegmentResult(text, captured_slots, captured_items, paused?)
        │
        ▼
 engine evaluates the branch DETERMINISTICALLY and advances itself
```

## `_run_segment(...)`

Runs ONE segment: a constant-size, cache-stable system prompt
(`ENGINE_SYSTEM_PROMPT`) + the segment payload, looped over provider calls
(≤ `MAX_SEGMENT_TURNS` = 25) until the model stops asking for tools. Returns
a `SegmentResult` with the final text, captured branch slots, captured
per-item facts, and a pause flag.

Tools exposed to a segment = the agent's real tools *minus* workflow tools
(the engine owns advancement; the model must not re-enter a workflow) *minus*
`plan_and_confirm` (the workflow IS the plan — a segment never re-plans or
re-gates the run behind another approval prompt) *plus* synthetic capture
tools:

- `record_slots` — the model reports branch/data variable values. Recording
  is terminal only for a BRANCHING segment (the engine is waiting on the
  values to decide); a data-only record captures and keeps looping, so
  actions after the record (e.g. writing the recorded report to disk)
  still run.
- `record_item_list` — for a `listDecision` segment, per-item fact-sets the
  engine then decides deterministically.

`human_feedback` inside a segment pauses the whole workflow.

## Per-agent model routing

A workflow step pinned to a named agent (`agents: {fast: {provider: openai,
model: gpt-4.1}}`) runs its segment on that agent's own in-process provider.
`_resolve_segment_provider` builds it via `make_provider`, cached by
(provider, model). Bindings that only pin a CLI runtime (e.g. claude-code),
or that can't be built, fall back to the run's default provider — the native
path never spawns an external CLI.

## `_make_segment_runner(event_sink, workflow_bg)`

Produces the `run_segment` callable placed on the tool context. The streaming
path passes an `event_sink` so segment events reach the UI live;
`workflow_bg` lets `human_feedback` block a backgrounded workflow coroutine
on the user's reply instead of unwinding the stack.
