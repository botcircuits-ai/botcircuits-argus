# Agent evals — DeepEval Task Completion

Outcome-based evaluation of the BotCircuits agent loop using
[DeepEval's Task Completion metric](https://deepeval.com/docs/metrics-task-completion).

Task Completion is an **LLM-as-judge, reference-free** metric: it reads the
agent's *execution trace* (the LLM turns + tool calls + tool results captured
via `@observe`) and scores how well the final outcome aligns with the task. No
golden/expected output is required, which suits this agent's non-deterministic
multi-turn workflows.

## Metrics — what they test and how they work

This suite uses two DeepEval metrics. They answer different questions, so we run
both: Task Completion asks *did the agent achieve the goal?*, Tool Correctness
asks *did the agent take the path it was supposed to?*

### 1. Task Completion — `TaskCompletionMetric`

**What it tests.** Whether the agent actually accomplished the user's task —
judged on the *outcome*, not on a fixed expected answer.

**How it works.**

1. The agent runs under `@observe` instrumentation (see `instrument.py`), which
   records a **trace**: the chat turn(s), every tool call with its arguments,
   and every tool result.
2. DeepEval hands that trace to a **judge LLM** (the "LLM-as-judge"; default
   `gpt-4o`, configurable). The judge extracts two things:
   - **Task** — the goal. Either the explicit `task=` string we pass per seed
     task, or, if omitted, inferred from the trace's input.
   - **Outcome** — what the agent's actions + final output actually produced,
     read from the trace.
3. It scores the alignment between them:

   ```
   Task Completion Score = AlignmentScore(Task, Outcome)   ∈ [0.0, 1.0]
   ```

   The score passes when it's ≥ `threshold` (we use `0.7`). With
   `include_reason=True` the judge also returns a sentence explaining the score.

**Why it fits this agent.** It's **reference-free** — no golden output to
maintain. The agent's workflows are multi-turn and non-deterministic (different
file timestamps, different phrasing each run), so an exact-match metric would be
brittle; an outcome judge is not. The catch: the judge only sees what the trace
captures, which is why `instrument.py` records tool calls/results, not just the
final text.

### 2. Tool Correctness — `ToolCorrectnessMetric`

**What it tests.** Whether the agent called the *right tools* — specifically,
that the **workflow guardrail** holds: when a request matches a workflow, the
model must call that workflow tool as its **first** action instead of
improvising with `shell_exec` / `write_file` (see IMPLEMENTATION.md §5.4, §8.6).

**How it works.** This metric is **reference-based**, the opposite of Task
Completion:

1. We run the task and record the ordered list of tool names actually called
   (by sniffing `ToolRegistry.run` directly — no judge LLM needed for this).
2. We build an `LLMTestCase` with `tools_called` (what happened) and
   `expected_tools` (what should have happened — here, the workflow tool).
3. The metric compares the two lists. The score reflects how well the actual
   tool usage matches the expected set; it passes at ≥ `threshold`.

We additionally assert in plain Python that the workflow tool was `called[0]`
(first), since "called eventually" isn't enough to prove the guardrail.

### When to reach for which

| Question | Metric | Reference needed? | Uses judge LLM? |
|---|---|---|---|
| Did the agent achieve the goal? | Task Completion | No | Yes |
| Did the agent take the right path / call the right tools? | Tool Correctness | Yes (`expected_tools`) | No |

## Layout

| File | Purpose |
|---|---|
| `instrument.py` | Wraps `Agent.chat` and `ToolRegistry.run` with DeepEval `@observe` spans so every run produces a trace the judge can read. Import it **before** building an Agent. |
| `harness.py` | `build_agent()` / `run_task()` — construct a real Agent (Anthropic by default) and run one task end to end, including driving a multi-turn workflow to completion within a single trace. |
| `tasks.py` | Seed tasks (`TASKS`), including one multi-turn workflow task. |
| `test_task_completion.py` | `deepeval test run` entry: `TaskCompletionMetric` over every seed task. |
| `test_tool_correctness.py` | `deepeval test run` entry: `ToolCorrectnessMetric` asserting the workflow guardrail forces the workflow tool as the first action. |

## Install

```bash
uv pip install -e ".[evals]"          # adds deepeval
# or: uv pip install deepeval
```

## Run

DeepEval's judge needs its own model key (defaults to OpenAI). The agent under
test needs its provider key. Both come from the project `.env`.

```bash
# Set the judge model (any OpenAI-compatible model deepeval supports)
export OPENAI_API_KEY=sk-...          # used by the TaskCompletionMetric judge

# Run the suite
.venv/bin/deepeval test run evals/test_task_completion.py
```

Or run a single task ad hoc and print the score + reasoning:

```bash
.venv/bin/python -m evals.harness "Add 17 and 25 and tell me the result."
```

## Notes / gotchas

- **One trace per task.** Multi-turn workflows span several `chat()` calls
  (each emits one `agentAction` then pauses). `run_task()` keeps driving the
  same `session_id` until the workflow's `session_id` clears, all inside one
  `@observe`-rooted trace, so the judge sees the whole run — not a single step.
- **Side effects.** The `workflow_demo` task writes `step_*.md` / `end.md`
  files. Tasks run in a temp cwd (see `harness.run_task(cwd=...)`) so they don't
  litter the repo.
- **Auto mode.** The harness runs with `auto=True` on gated tools (no human at
  the y/N prompt during a non-interactive eval).
- **Judge cost.** Each metric call is an extra LLM round-trip. Keep `TASKS`
  small for CI; expand locally.
