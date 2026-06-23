# Workflow evaluation framework

Quantifies the hypothesis behind the workflow module: **structured,
rule-driven tasks are more accurate and more consistent when an agent
has an executable workflow tool than when the same steps are written
into the agent's system prompt as natural-language instructions.**

The framework drives a real `Agent` end-to-end through each test case
in two modes — same model, same tools, same scripted user replies —
and the only difference is whether the workflow tool is exposed.

| Mode | What runs | What it measures |
|---|---|---|
| `workflow_on`  | `Agent` with the workflow tool registered + `enable_workflows=True` | The production setup: agent decides when to call the workflow, the engine handles branching + slot collection. |
| `workflow_off` | `Agent` with the workflow tool hidden (`enable_workflows=False`) and the dataset's `workflow_spec` injected into the system prompt | The "no workflow module" baseline: same agent, same other tools (read_file, write_file, shell, …), but the procedure is prose the model has to follow on its own. |

The legacy engine-only / prompt-only runners
(`runner_workflow`, `runner_prompt`) are still importable from the
package for callers who want to test the STM engine in isolation, but
they're no longer the default path. The headline comparison is the
agent-driven one above.

## Dataset

Datasets live under `.botcircuits/evaluation/` (override with
`$BOTCIRCUITS_EVAL_DIR`). One file = one dataset = one workflow under
test + one or more cases that exercise it.

Three ways to point a dataset at a workflow:

- **Referenced** — set `workflow: "<name>"` at the root. The named
  workflow must already be built on disk under
  `.botcircuits/workflows/.build/`. The `workflow_off` baseline has
  no procedure prompt in this mode (just the case's user replies +
  the agent's other tools).
- **Inline** — set `workflow_spec: "<natural-language description>"`
  at the root. Once per dataset, before any case runs, the harness:
    1. Asks the configured LLM to translate the spec into a
       structured `build_workflow` payload.
    2. Writes the raw source + runs the indexer (same code path as
       `/workflow add`, just with the y/N gate auto-skipped).
    3. Every case in the dataset runs against the SAME generated
       workflow in both modes.
    4. After all cases finish, the generated files are LEFT ON DISK
       by default so you can inspect what the LLM authored. Pass
       `--cleanup-inline-workflow` to remove them automatically.
- **Both** — set `workflow` AND `workflow_spec`. The on-disk workflow
  is used for `workflow_on`; the spec is used for the `workflow_off`
  baseline's system prompt. No inline build runs (the workflow
  already exists). This is the right shape once an inline-built
  workflow has been "promoted" to a regular project workflow you
  want to keep.

### Schema

```json
{
  "name": "loan_triage_v1",
  "description": "What this dataset measures.",
  "workflow": "loan_triage",
  "workflow_spec": "Build a workflow that ... step 1 ... step 2 ...",
  "cases": [
    {
      "id": "loan.reject_under_age",
      "description": "17-year-old applicant; expect DENIED.",
      "initial_args": {},
      "initial_user_text": "Hi, I'd like to apply for a loan. I'm Sam Reed.",
      "turns": [
        {"args": {}, "user_text": "I just turned 17 last month."},
        {"args": {}, "user_text": ""}
      ],
      "expected": {
        "must_contain": ["DENIED", "18"]
      }
    }
  ]
}
```

Per-case fields:

- `initial_args` — slot values the agent would have pre-extracted
  before the first workflow-tool call. Empty `{}` is the common case
  when the agent has to read the user's natural-language reply first.
- `initial_user_text` — the user's opening chat message, passed to
  `Agent.chat()` as the first turn.
- `turns[i].user_text` — the user's reply for the (i+1)th chat turn.
  Each non-empty `user_text` becomes one `Agent.chat()` call; the
  agent's tool loop runs to completion (workflow invocations + other
  tool calls) before the next user message is fed in. Empty
  `user_text` entries are skipped in agent mode (they were a
  workflow-engine artifact for the legacy runner where the engine
  needed an extra empty re-entry to advance past non-branching
  states).
- `turns[i].args` — pre-extracted slot values for that turn. Mostly
  unused in agent mode because the agent does its own extraction;
  retained for backwards compatibility with the legacy runners.
- `expected.must_contain` — substrings that must appear in the
  agent's final assistant reply for the case to score 1.0 on that
  signal. Both modes are scored against the same rubric.

## Metrics

Both columns are scored from the agent's final assistant text on each
case:

- **Accuracy** — average of declared signals per case
  (`must_contain`, `final_state`, `trace`). In agent mode only
  `must_contain` is meaningful; `trace` / `final_state` apply when
  you're using the legacy runners directly.
- **Consistency** — each case is run `repeats` times (default 3,
  configurable via `--repeats`). Consistency is the fraction of runs
  that produced the same final reply as the modal reply. Both modes
  are non-deterministic because both go through the LLM, so this
  metric distinguishes "the workflow constrains variance" from "the
  LLM happens to be steady on this prompt."

Per-case detail in the text report also surfaces:

- The agent's final reply preview (truncated).
- The list of tool names the agent called during the run.
- `workflow invocations` — how many times the `workflow_on` agent
  called the workflow tool. Should be `> 0` for any case where the
  workflow was supposed to drive the conversation; `0` means the
  agent ignored the tool.

## Running

```sh
# Full comparison against the configured provider.
botcircuits-cli workflow eval --repeats 3 --report eval-report.json

# Just one dataset file.
botcircuits-cli workflow eval --dataset path/to/cases.json

# Skip the workflow_off baseline (workflow_on agent still runs,
# still uses the provider). Useful for verifying the workflow path
# still works without paying for the baseline.
botcircuits-cli workflow eval --skip-prompt-baseline

# Inline-build dataset; delete the generated workflow files after the
# run. Default leaves them on disk for inspection.
botcircuits-cli workflow eval --cleanup-inline-workflow
```

A real LLM provider is required for both modes — the harness drives
real `Agent` instances. The provider is also used for inline-build
when the dataset carries `workflow_spec`.

## Interpreting the report

```
                  workflow_on    workflow_off
accuracy (mean)   0.000          1.000
contains match    0.000          1.000
consistency       1.000          1.000

  - loan.reject_under_age   workflow_on=0.00  workflow_off=1.00  (referenced)
      workflow_on  reply: "…you may not meet the minimum age requirement…"
      workflow_on  tools: ['loan_triage', 'loan_triage']  (workflow invocations: 2)
      workflow_off reply: "…the application is DENIED because you must be 18 or older…"
      workflow_off tools: []
```

That row says:

- `workflow_on` routed correctly — the agent called the workflow
  twice and the engine landed on the right terminal state (the
  `workflow_off` reply with the same model proves the routing logic
  is sound). But the agent **paraphrased** the engine's terminal
  action text into softer prose that dropped the literal "DENIED" /
  "18" keywords the `must_contain` rubric was checking for.
- `workflow_off` followed the spec verbatim because the spec IS the
  system prompt — when the rubric is "include exact terminal wording",
  the prompted procedure beats the engine here.

This is the kind of finding the framework exists to surface: the
workflow gives you correct branching but doesn't constrain the final
user-facing wording; the prompted baseline is more literal but
sacrifices the determinism the engine provides on branching-heavy
flows. The right choice depends on whether your downstream checks
care about exact wording (`workflow_off` wins on this rubric) or about
deterministic state traversal under repeats and edge cases
(`workflow_on` wins).

## Writing good cases

- Use `must_contain` to assert outcome content (error codes, file
  names, decision keywords) rather than full wording. The agent in
  either mode will phrase replies differently across runs; substring
  assertions stay fair while still catching wrong outcomes.
- For `workflow_on`, check `workflow invocations > 0` in the per-case
  detail — that's the sanity signal that the agent actually called
  the workflow tool rather than ignoring it.
- For inline datasets, state IDs are chosen by the LLM at build time
  and not predictable, so `trace` / `final_state` assertions usually
  shouldn't be set. Pin outcome content via `must_contain` only.
- Add cases for every branch of every conditional in the workflow.
  The `workflow_on` advantage is largest where multiple branches
  exist and one of them is rare — that's where the prompted baseline
  is most likely to drift.
- Each `turns[i].user_text` should be a single human-style reply to
  the *previous* assistant message. The harness pumps one per
  `Agent.chat()` turn, letting the agent's tool loop run between
  them; turns whose `user_text` is empty are dropped in agent mode.
