# Workflows

A **workflow** is BotCircuits' deterministic-process primitive: a state
machine you describe once, in natural language, that then runs the same
way every time — no re-reasoning about routing, no drift between runs.
This document covers the whole feature end-to-end: what a workflow is,
how it's authored and built, how it runs, how it verifies its own
output and repairs itself on failure, and every CLI command and file
this touches.

> For a terse, code-level tour of how the engine is implemented, see
> [`docs/native_agent/workflow.md`](native_agent/workflow.md). This
> document is the user-facing reference; that one is for reading the
> source.

## Contents

- [Concepts](#concepts)
- [Lifecycle](#lifecycle)
- [Authoring](#authoring)
- [Building](#building)
- [Running](#running)
- [Verification gates](#verification-gates)
- [Self-repair](#self-repair)
- [File layout](#file-layout)
- [CLI reference](#cli-reference)
- [Manager (web UI) API](#manager-web-ui-api)
- [Evaluation](#evaluation)
- [FAQ / troubleshooting](#faq--troubleshooting)

## Concepts

A workflow is one JSON document with two layers:

- **Source** — the human-editable file you (or an authoring agent)
  write, under `.botcircuits/workflows/<name>.json`. Branches are
  expressed as natural language (`"condition": "all items are in
  stock"`).
- **Build** — a compiled, runnable copy under
  `.botcircuits/workflows/.build/<name>/<name>.json`. Natural-language
  conditions are compiled into deterministic rule expressions
  (`choices[]`); the AI is no longer involved in deciding which branch
  to take at run time — only in performing the action text of the step
  it's dispatched to.

The **engine owns the loop** at run time. It walks the compiled state
machine step by step, calling the AI only to execute one step's action
(or a batch of adjacent non-branching steps, called a *segment*), and
evaluates every branch itself from the values the AI reported. This is
why runs are traceable and repeatable: the same inputs always follow
the same path, and every branch decision is on the record.

Two more pieces sit on top of this core loop, both introduced to make
workflows correct by construction rather than correct by luck:

- A **verification gate**, generated automatically at build time,
  checks a run's outcome once it finishes.
- **Self-repair**, wired into `workflow run`, retries a run that fails
  its gate — automatically, bounded, and never silently.

## Lifecycle

```
 author (NL) ──► .botcircuits/workflows/<name>.json         [source, hand-editable]
      │
      │  botcircuits workflow build --name <name>
      ▼
 build ──► .botcircuits/workflows/.build/<name>/<name>.json  [runnable]
      │         + .build/<name>/verifications/gate.json       [auto-generated]
      │         + .build/<name>/verifications/checks/*.py     [auto-generated]
      │
      │  botcircuits workflow run --name <name>
      ▼
 run ──► engine walks the built state machine, dispatching
         one segment at a time to the AI, evaluating every
         branch deterministically
      │
      ├─ paused  (needs user input) ──► --reply "<answer>" ──► resume
      │
      └─ done ──► verification gate checks the outcome
                     │
                     ├─ passes ──► status: "success"
                     │
                     └─ fails ──► self-repair retries the WHOLE
                                  workflow (capped), then either
                                  passes or reports a genuine
                                  status: "failure"
```

Only `.build/` is loaded at run time. A source file with no build
counterpart is not runnable — the loader skips it with a warning
telling you to build it.

## Authoring

Authoring is turning a natural-language process description into the
workflow JSON shape. In an interactive agent session, this is normally
done for you by the **botcircuits-workflow-authoring** skill — you
describe the process in plain language and the agent writes the file.
This section documents the shape it produces, for when you want to
read, hand-edit, or understand a workflow file directly.

### Shape

```json
{
  "name": "order_fulfillment",
  "description": "when to run this workflow",
  "flow": {
    "start": "start",
    "steps": {
      "start": { "type": "start", "next": "check_stock" },
      "check_stock": {
        "type": "agentAction",
        "settings": { "action": "Check stock for the order items." },
        "next": "backorder",
        "conditions": [
          { "condition": "all items are in stock", "next": "ship" }
        ]
      },
      "ship":      { "type": "agentAction", "settings": { "action": "Ship the order." } },
      "backorder": { "type": "agentAction", "settings": { "action": "Create a backorder and notify the customer." } }
    }
  }
}
```

- `name` is slug-safe (`^[a-zA-Z0-9_-]+$`) — it doubles as the filename
  and as the tool name surfaced to the model.
- `start`/`steps` live under a `flow` wrapper — a file with them at the
  top level fails to build.
- Branching lives at the **step root**, under `conditions` (sibling of
  `type`/`next`/`settings`, never nested inside `settings`). Each entry
  is `{"condition": "<natural language>", "next": "<step id>"}`. The
  step's own `next` is the default/"otherwise" branch.

### Step types

| Type | What it does |
|---|---|
| `start` | Entry point. No action, just a `next` pointer. |
| `agentAction` | The AI performs `settings.action` (a natural-language instruction). May branch via `conditions`. |
| `question` | The AI asks the user `settings.action` and waits for a reply — the only step type that pauses a run for human input. |
| `systemAction` | Engine-side bookkeeping with the same shape as `agentAction`, but never dispatched to the AI — no LLM round-trip. |
| `listDecision` | Decide the same outcome for **every item in a list**, deterministically, in one step (see below). |
| `parallel` | Run independent chains of steps **concurrently**, rejoining once all finish (see below). |

### Variables and `input` variables

`flow.variables` is an aggregated catalogue of every variable the
workflow reads or writes, built automatically at build time from the
`conditions` you wrote. A variable marked `"input": true` is one the
**user** must supply — before the first segment runs, the engine
resolves every `input` variable deterministically from the trigger
message, falls back to a cheap-model extraction pass, offers to reuse
the previous run's values, and only as a last resort pauses with one
authored question asking for whatever's still missing. You never write
a step to "ask for the order id" — mark the variable `input: true` and
the engine handles collection before your first step even runs.

### `flow.result` — the declared final answer

A workflow can declare `flow.result` so the engine renders the final
answer itself, from its own state, with zero model output at the end:

```json
"result": { "kind": "template", "value": { "tracking_results": "{tracking_results}" } }
```

Three forms:

- `{"kind": "from_file", "path": "data/decisions.json"}` — read a JSON
  file a step wrote, use its content as the result.
- `{"kind": "template", "value": {...}}` — a JSON structure whose
  string leaves are slot-interpolated (`{slot_name}`).
- `{"kind": "slots", "keys": ["a", "b"]}` — a flat object of the named
  slot values.

Absent `flow.result`, the run falls back to a generic `<outcome>,
slots {...}` summary line. `flow.result` (and `flow.variables`) are
what a generated verification gate reads to figure out what a
"correct" run of this workflow even looks like — see
[Verification gates](#verification-gates).

### `listDecision` — deciding many items, not looping

When a process applies the **same decision to every item in a
collection** ("check every parcel", "price each line item"), don't
hand-build a loop (`next_item → decide → record → next_item` with a
model-maintained "done" flag) — that makes the model drive iteration
instead of the engine, and only a handful of segments end up traced.

Use one `listDecision` step instead. The engine fans its `conditions`
across every item in a list and decides each deterministically:

```json
"decide_line_items": {
  "type": "listDecision",
  "settings": { "action": "Decide each order line item against available stock." },
  "itemSource": { "file": "data/current_order.json", "path": "items" },
  "itemVariables": [
    { "variableName": "in_stock", "description": "whether the sku is in stock" },
    { "variableName": "enough", "description": "stock covers the requested qty" }
  ],
  "decisionKey": "decision",
  "collectInto": "line_results",
  "conditions": [
    { "condition": "the sku is not in stock", "next": "reject" },
    { "condition": "in stock but not enough for qty", "next": "backorder" }
  ],
  "defaultNext": "fulfill",
  "next": "save_results"
}
```

Key points:

- `conditions[].next` and `defaultNext` are **decision words** for the
  item (`reject`, `backorder`, `fulfill`), not step ids. The one place
  `next` IS a step id is the step-level `next` — the real step the flow
  continues to once the whole list is decided.
- The first matching condition wins, top to bottom — order failure/error
  checks first.
- One condition = one comparison. `"status is exception or returned"`
  compiles to a single branch and silently drops the rest — write
  separate entries pointing at the same decision word instead.
- `itemSource` `{file, path}` points at the list (`path` is a JSON
  path, or `""` for a plain one-item-per-line text file).
- Optional `itemFacts` (`kind: "exec"`) lets the ENGINE gather each
  item's facts deterministically by running a script — no LLM call per
  item. Omit it to have the model report facts once for the batch.
- `collectInto` names the slot receiving the decided records;
  `decisionKey` names the per-record decision field; optional `nullOn`
  blanks a field for specific outcomes.

### `parallel` — running independent work concurrently

When several checks or lookups can run **at the same time** and don't
depend on each other's output:

```json
"fanout_checks": {
  "type": "parallel",
  "branches": {
    "credit":    ["check_credit"],
    "inventory": ["check_inventory", "reserve_stock"],
    "fraud":     ["check_fraud"]
  },
  "next": "decide_outcome"
}
```

- `branches` maps `name -> [step id, ...]` — each list is an ordinary
  chain of already-defined steps, run start to finish like a normal
  walk.
- A branch step must not branch, pause, or nest another `parallel` —
  `workflow build` rejects a branch that does.
- All branches finish before the single `next` on the `parallel` step
  runs — there's no per-branch `next`.
- Branches must write to distinct slot names; two branches disagreeing
  on the same variable fails the step.
- Optional `onError`: a step id to route to if any branch fails.
  Omitted, the failure propagates and surfaces as a run error.

### Pinning a step to a different model

Most workflows don't need this. When a step genuinely benefits from a
different model (a cheap model for trivial extraction, a stronger one
for hard judgment, or a different CLI runtime entirely):

```json
{
  "agents": {
    "researcher": { "runtime": "codex", "model": "o3" },
    "writer":     { "model": "claude-opus-4-7" }
  },
  "flow": {
    "steps": {
      "fetch_trends": { "type": "agentAction", "agent": "researcher", "settings": {...} },
      "write_report": { "type": "agentAction", "agent": "writer", "settings": {...} }
    }
  }
}
```

Every step's `agent` value must match an entry in the top-level
`agents` map — the build flags an unknown reference.

## Building

```bash
botcircuits workflow build --name <name>
```

This is where the AI does real work up front, so runs don't have to:

1. Compiles every natural-language `condition` into a deterministic
   rule expression (`choices[]`).
2. Aggregates `flow.variables` across the whole graph.
3. Runs normalization passes (`workflow_defaults`, `graph_optimizer`,
   `action_optimizer` unless `--no-optimize`) that fill mechanical
   fields and rewrite verbose step actions into terse, tool-directed
   instructions — cutting per-run tokens without changing behavior.
4. Computes branch-delimited **segments** — the unit the engine
   batches into one AI call at run time.
5. **Generates a verification gate** (see below) from the now-compiled
   `flow.result`/`flow.variables` shape.
6. Writes the runnable copy to `.build/<name>/<name>.json`.

Gate generation is automatic and best-effort: a failure there is
reported but never fails the build itself — the workflow is still
written and runnable, just without a gate.

The build step also runs a pure, no-LLM structural lint
(`workflow_validator.py`) and prints any warnings — these never block
the build, they're for you to fix in the source.

## Running

```bash
botcircuits workflow run --name <name> [--initial-args '{"order_id": "1024"}']
```

The engine drives the built state machine end to end:

- It calls the AI once per **segment** (a batch of consecutive
  non-branching steps), never once per raw step — cutting round-trips.
- It evaluates every branch itself, from the values the AI reported,
  against the compiled `choices[]` — the AI never decides routing.
- Any variable marked `input: true` is resolved before the first
  segment runs (deterministically from the trigger message where
  possible, then a cheap-model extraction pass, then an offer to reuse
  the last run's values, then — only as a last resort — one authored
  question).

The command prints exactly one JSON outcome:

```
{"status": "success", "message": "<summary>"}
{"status": "failure", "message": "<reason>"}
{"status": "paused",  "question": "<ask the user>", "options": [...]}
```

- `success` — the workflow finished (and, if it has a verification
  gate, the gate passed — possibly after an automatic repair; see
  below).
- `failure` — **terminal**. If a gate exists, self-repair already
  retried internally before giving up; do not retry again yourself.
- `paused` — the workflow needs input only a human can give. Resume
  with:

  ```bash
  botcircuits workflow run --name <name> --reply "<their answer>"
  ```

  Repeat until the outcome is `success` or `failure`. `options`, when
  present, is a fixed answer set (e.g. a yes/no/change-a-value reuse
  offer) you can render as a picker — typing free text is always
  accepted too.

Pause/resume state persists across process invocations under
`.botcircuits/workflows/.runs/<name>.json`, removed on completion.

## Verification gates

A verification gate is a small, generated set of checks that answer
one question: **did this run actually produce the right outcome?**
Finishing without an engine error is not the same as being correct —
a workflow can complete "successfully" while its output is wrong,
incomplete, or off-policy. The gate is what catches that.

### Two kinds of checks, one framework

There are exactly two check types, and the executor that runs them
(`agent/workflow/verification/executor.py::run_gate`) never changes —
it's a fixed framework. The only thing that varies per workflow is the
gate's **content** (the check list), generated once at build time:

| Type | For | How it runs |
|---|---|---|
| `script` | Objective, computable invariants — totals that must sum, a required field that must be non-empty, a count that must match an input list's length. | A generated Python script, run as a sandboxed subprocess (no shell, hard timeout). Reads the run record as JSON on stdin, prints `{"passed": bool, "detail": str}` on stdout. |
| `llm_judge` | Subjective or use-case-specific criteria — tone, policy compliance, whether free-text output actually addresses the request. | One strict-JSON `provider.complete()` call against a natural-language rubric, scoped to an explicit allow-list of the run's fields (`inputs`). |

Every check also has a `severity`:

- `blocking` — a failure fails the whole gate and triggers self-repair.
- `advisory` — recorded in the gate's report, but never fails the gate
  or triggers a retry.

A check that fails to *execute* (a script crash/timeout, a judge call
or parse failure) is always treated as blocking, regardless of its
declared severity — a broken check must never silently pass.

### How `run_gate` evaluates one check

`run_gate` is the one fixed dispatcher every gate runs through — it
never changes; only the `checks` list it's given (generated per
workflow) does:

```
                              run_gate(gate_spec, run_record)
                                        │
                     any check type == "llm_judge"
                     AND no provider supplied? ──── yes ──► raise
                                        │                   VerificationError
                                        │ no                (fail fast, before
                                        ▼                    the loop starts)
                        for each check in gate_spec["checks"]:
                                        │
                        ┌───────────────┼────────────────────┐
                        ▼               ▼                    ▼
                  type: "script"   type: "llm_judge"    unknown type
                        │               │                    │
                        ▼               ▼                    ▼
              script_check.run_one  judge_check.run_one  CheckResult(
              ─ spawn subprocess,   ─ provider.complete()  passed=False,
                no shell            strict-JSON prompt,    error="unknown
              ─ stdin: run_record   scoped to `inputs`      check type")
                as JSON             allow-list only
              ─ stdout must be     ─ never raises: a
                one line of          call/parse failure
                {"passed": bool,     becomes CheckResult
                 "detail": str}      (passed=False,
              ─ nonzero exit /       error="...")
                bad timeout /
                bad stdout ──►
                CheckResult(error=...)
                        │               │                    │
                        └───────────────┴────────────────────┘
                                        ▼
                     CheckResult(check_id, passed, severity, detail, error)
                                        │
                                        ▼
                gate passed  =  NOT any(check.is_blocking_failure)
                                  where is_blocking_failure means:
                                    error is not None                    (always blocking)
                                    OR (not passed AND severity=="blocking")
                                        │
                                        ▼
                              GateResult(passed, checks=[...])
```

### How a gate is generated

At build time, `verification/generator.py::generate_gate` sends the
workflow's compiled `flow` — its steps, `flow.result` shape, and
`flow.variables` schema — to an LLM and asks for 2-5 checks. It's
biased toward `script` checks wherever the declared output shape makes
an invariant expressible in code, falling back to `llm_judge` only for
criteria that are inherently about phrasing, tone, or policy rather
than a checkable fact.

### Manifest shape

```json
{
  "workflow_name": "order_fulfillment",
  "version": 1,
  "checks": [
    {
      "id": "totals_match",
      "type": "script",
      "description": "line_total sums must equal the recorded order total",
      "script": "verifications/checks/totals_match.py",
      "severity": "blocking"
    },
    {
      "id": "tone_is_professional",
      "type": "llm_judge",
      "description": "Final summary reads as professional, non-apologetic",
      "rubric": "Score pass only if the summary avoids hedging/internal jargon.",
      "inputs": ["slots.final_summary"],
      "severity": "advisory"
    }
  ]
}
```

`run_gate` reads this manifest, runs each check against the completed
run's `{workflow_name, slots, summary, decisions, done}` record, and
returns a pass/fail per check plus an overall gate verdict.

## Self-repair

Self-repair is what happens when a gate fails — wired into
`runtime/run_workflow.py::_run`, the same function `workflow run` uses,
so it happens automatically and needs no separate command.

```
                        engine finishes a run: EngineResult(done)
                                        │
                     .build/<name>/verifications/gate.json
                     exists? ──────────────────────── no ──► status: "success"
                                        │                    (unchanged from before
                                        │ yes                 this feature existed)
                                        ▼
                     ┌──────────────────────────────────────────┐
                     │  repair_count = 0                        │
                     │  loop:                                   │
                     │                                          │
                     │    run_record = {slots, summary,         │
                     │                  decisions, done: true}  │
                     │                    │                     │
                     │                    ▼                     │
                     │    gate_result = run_gate(gate_spec,     │
                     │                    run_record)           │
                     │    gate_attempts.append(gate_result)     │
                     │                    │                     │
                     │        passed? ────┴──── yes ──► break   │
                     │           │ no                            │
                     │           ▼                                │
                     │  repair_count >= MAX_REPAIR_ATTEMPTS (2)?  │
                     │           │ yes ─────────────────► break   │
                     │           │ no                              │
                     │           ▼                                │
                     │  repair_count += 1                          │
                     │  slots["__repair_feedback__"] =             │
                     │    [f"{check_id}: {error or detail}"        │
                     │     for each BLOCKING failure]              │
                     │           │                                │
                     │           ▼                                │
                     │  result = run_workflow_engine(              │
                     │             flow, start_step_id=None,       │
                     │             slots)   ── retries from `start`,│
                     │           │            same run, new context│
                     │           ▼                                │
                     │  result.paused? ── yes ──► return           │
                     │           │              status: "paused"   │
                     │           │              + gate.attempts     │
                     │           │ no  (loop back to top)          │
                     └───────────┴──────────────────────────────────┘
                                        │
                     final gate_attempts[-1].passed?
                                        │
                          yes ──────────┴────────── no
                           │                          │
                           ▼                          ▼
                 status: "success"           status: "failure"
                 "gate": {attempts: [...]}   "gate": {attempts: [...]}
                 (every attempt kept,        (never silently "success" —
                  not just the last)          the budget ran out)
```

- On completion, `workflow run` auto-detects
  `.build/<name>/verifications/gate.json`. **No gate file → zero
  overhead, zero behavior change** — this is fully backward compatible
  with every workflow that hasn't had a gate generated for it.
- If a gate exists, it runs against the finished run's outcome.
- On a **blocking** failure, the whole workflow is retried from
  `start` — not a single segment, and not a patch to the built JSON —
  with the failure's check details folded into the run's context
  (`__repair_feedback__`) so the AI can see what went wrong and avoid
  repeating it.
- This repeats up to a small **fixed** cap (2 repair attempts — 3 total
  runs) before giving up.
- If the gate still hasn't passed once the budget is exhausted, the
  run is reported as a genuine `status: "failure"` — never silently
  `"done"`. The JSON output's `"gate"` key carries every attempt's
  verdict, so you can see the whole trajectory, not just the final one.

Why retry the *whole workflow* rather than a single step: the gate
only sees the run's *finished* state — there's no mid-run checkpoint to
resume into once the engine has already unwound. And why not patch the
built workflow JSON automatically: that would mutate a shared,
versioned artifact based on one noisy run, which is a materially
riskier operation than re-running — that direction (patching a
*draft*, before it's ever run for real) exists separately in `workflow
generate --validate-loop`.

## File layout

```
.botcircuits/workflows/
  <name>.json                                # source — hand-editable
  .build/
    <name>/
      <name>.json                            # built, runnable copy
      verifications/
        gate.json                            # verification-gate manifest
        checks/
          <check_id>.py                      # generated deterministic checks
  .runs/
    <name>.json                              # pause/resume cursor for an in-progress run
  .last_inputs/
    <name>.json                              # remembered input values, offered on the next run
```

Override the workflows directory with `$BOTCIRCUITS_WORKFLOWS_DIR`
(default `.botcircuits/workflows`). Everything under `.build/` is
generated — never hand-edit it; it's replaced whole by the next build.

## CLI reference

```
botcircuits workflow build --name <name> [--no-optimize]
```
Compile the source into a runnable build (+ verification gate).
`--no-optimize` keeps authored step-action text verbatim, skipping the
terse-rewrite pass.

```
botcircuits workflow run --name <name> [--initial-args '<json>'] [--runtime <name>] [--reply '<text>']
```
Run (or resume) a built workflow. `--initial-args` seeds slot values as
a JSON object. `--runtime` forces a specific host CLI agent
(`claude-code`, `codex`, …) instead of auto-detecting one. `--reply`
answers a prior `paused` outcome and resumes the same run.

```
botcircuits workflow generate --from <file> --name <name> [--resources <file>] [--validate-loop N] [--dry-run-samples <file>] [--build]
```
Author a workflow SOURCE file from a natural-language description read
from `--from`. `--validate-loop N` re-validates the draft and feeds
problems back to the model for up to N repair rounds; `--dry-run-samples`
adds value-level wiring checks against sample inputs. `--build` also
runs `workflow build` immediately on the result.

```
botcircuits workflow eval [--dataset <file>] [--repeats N] [--report <file>] [--skip-prompt-baseline] [--cleanup-inline-workflow]
```
Run the evaluation framework — see [Evaluation](#evaluation).

## Manager (web UI) API

The manager backend (`botcircuits manager start`) exposes workflow
source management as a thin JSON API the `manager_web` frontend calls:

| Method | Path | What it does |
|---|---|---|
| `GET` | `/api/workflows` | List every workflow source, with `built`/`has_gate`/`step_count` summaries. |
| `GET` | `/api/workflows/{name}` | The full raw source document. |
| `PUT` | `/api/workflows/{name}` | Create or overwrite a source document. |
| `DELETE` | `/api/workflows/{name}` | Delete the source and its whole build folder (including any gate). |
| `POST` | `/api/workflows/{name}/build` | Build the source (shells out to `workflow build`). |
| `GET` | `/api/workflows/author/stream` | SSE stream for interactive authoring. |
| `GET` | `/api/workflows/run/stream` | SSE stream for interactively running a workflow. |

The manager web UI shows a `gate` badge next to `built`/`not built` for
any workflow whose build carries a verification gate.

## Evaluation

`botcircuits workflow eval` quantifies whether the engine-driven
approach actually beats a prompt-only baseline on the same task: it
drives a real agent through each dataset case in two modes — with the
workflow tool exposed (`workflow_on`) and with the same procedure
injected into the system prompt as prose instead
(`workflow_off`) — and scores accuracy and run-to-run consistency.
Datasets live under `.botcircuits/evaluation/` (`$BOTCIRCUITS_EVAL_DIR`
to override); see `src/botcircuits/agent/workflow/evaluation/README.md`
for the dataset format and scoring detail.

## FAQ / troubleshooting

**"no built workflow found with name '...'"** — the source exists but
hasn't been built (or was built before this repo's `.build/` layout
changed to nested `<name>/<name>.json` folders). Run `botcircuits
workflow build --name <name>`.

**A workflow I built before has no gate.** Gates are generated at
build time, automatically — rebuild it (`workflow build --name
<name>`) to get one. Workflows built before the verification-gate
feature existed simply have no gate until then; they still run exactly
as before.

**`workflow run` seems slower than before / ran more than once.** If
the workflow has a gate and the first attempt failed it, self-repair
retried automatically. Check the `"gate"` key in the JSON output — its
`attempts` list shows why.

**I want to skip verification for one run.** There's no per-run flag
for this today — verification is automatic whenever a gate file
exists. Removing `.build/<name>/verifications/gate.json` (or
rebuilding without one being regenerated) disables it for that
workflow entirely.

**Can I hand-write a gate?** Yes — `verifications/gate.json` and its
`checks/*.py` scripts are plain files; nothing stops you from editing
or writing them by hand following the [manifest shape](#verification-gates)
above. Just note that gate generation runs on **every** `workflow
build`, unconditionally, and always overwrites the manifest and check
scripts — a hand edit survives until the next rebuild, then is
replaced by a freshly generated gate.
