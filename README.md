<h1 align="center">Argus</h1>

<p align="center">
  <strong>Enforce structure where it must hold. Reason only where it's needed.</strong>
</p>

---

An agent skill (Claude, Hermes, etc.) that runs your repetitive workflows **predictably**, **traceably**, and **cost-efficiently** — cutting **~90%** of tokens usage while keeping full accuracy.

![botcircuits-agent-solution](docs/solution.png)

```
claude > "create an order fulfillment workflow with stock check, ship, and backorder branches"
claude > "run order fulfillment"
```


Why let your LLM agent reason through every routing decision — even when the path is repetitive and predictable?

## Structured flow control 
**Replace guesswork with deterministic workflows**. Argus controls navigation, ensuring AI agents follow secure, approved, and repeatable execution paths instead of reasoning through every step.

## Stateful memory context
**Provide only the context that matters**. Argus tracks state changes and supplies the agent with the exact memory needed for the current step, improving reliability while reducing token usage.

---

## How it works

Argus ships **two skills** your agent loads:

| Skill | The user says… | The agent does… |
|---|---|---|
| **botcircuits-workflow-authoring** | _"create an order fulfillment workflow with …"_ | Writes the workflow JSON and **builds** it into a runnable state machine. |
| **botcircuits-workflow-running** | _"run order fulfillment"_ | Runs the workflow — the **deterministic engine** drives navigation in the background and dispatches each action to the AI agent. |

---

## Installation

Argus runs inside a host agent. Install the agent you want, then install Argus.

### 1. Prerequisite : host agent

Argus is driven by a host AI coding agent. Install whichever you prefer, for example:
- **Claude Code**
- **Hermes**

### 2. Install BotCircuits Argus

```bash
curl -fsSL https://raw.githubusercontent.com/botcircuits-ai/botcircuits-argus/main/scripts/install.sh | bash
```

### 3. Initialize project settings

Create an initial `.botcircuits/settings.json` in the folder you want to run Argus from. This also installs the workflow skills for the selected runtime's host agent (defaults to `~/.claude/skills`):

```bash
botcircuits init
botcircuits init --dir <path>               # or target another folder
botcircuits init --runtime <host-agent>      # set settings.runtime + install its skills
```

- `--runtime` — seed `settings.runtime` with a currently supported host agent runtime: `claude-code`, `hermes`. Default use **claude-code**. Also installs that runtime's workflow skills.
- `--link` — symlink the skills instead of copying, so updates to Argus are picked up automatically.

Argus dispatches work to an **agent runtime** — the host that actually carries out the work, both when **authoring/building** a workflow and when **running** one.

### 4. Install the skills (optional)

`botcircuits init --runtime <host-agent>` already installs the skills for that runtime. Use this directly if you want to (re)install into another agent, or without touching `settings.json`:

```bash
botcircuits skills install [--agent claude|hermes] [--link]
```

- `--agent` — target host agent (default: `claude`).
- `--link` — symlink the skills instead of copying, so updates to Argus are picked up automatically.

### 5. Override the runtime command (optional)

If a host's CLI isn't on your `PATH`, or it needs different flags, override its launch command (and optionally `timeout` / `cwd`) under `runtimes`:

```json
{
  "runtime": "hermes",
  "runtimes": {
    "hermes": {
      "command": ["hermes", "-z", "{prompt}", "--yolo"],
      "timeout": 600
    }
  }
}
```

`{prompt}` is the placeholder Argus substitutes with each step's segment prompt.

## Workflows

A workflow goes through two phases: you **author** it (describe the process and write the flow json), then it is **built** into a deterministic, runnable form, which the engine then **runs**. Authoring and building happen together. running is a separate step you trigger later.

### 1. Authoring
Converse with your AI agent to generate the structure naturally — describe the process in plain business language and the authoring skill writes the workflow JSON for you. If you prefer a visual approach, you can map out the logic in the [Argus Web Manager](#argus-web-manager) flow editor.

```
claude > "create an order fulfillment workflow: check stock; if all items are in stock, ship. otherwise create a backorder and notify the customer"
```

This writes your source file to `.botcircuits/workflows/<name>.json` and then builds it automatically (see [Building](#2-building) below).

### 2. Building

**The raw JSON you author is _not_ what runs.** A build step turns the human-readable source into a deterministic state machine that the engine can execute without guessing. This is what makes runs predictable and traceable.

When you build a workflow, **workflow-builder**:

- **Compiles each natural-language `condition`** (e.g. `"all items are in stock"`) into a deterministic `choices[]` entry — a concrete rule the engine evaluates the same way every time, with no model interpretation at navigation time.
- **Aggregates a `flow.variables` list** across all steps, so the engine knows exactly which variables the workflow reads and writes and can supply the agent only the context needed for the next step.
- **Writes the runnable copy** to `.botcircuits/workflows/.build/<name>.json`.

#### Where files live

- `.botcircuits/workflows/*.json` — your authored sources, the editable source of truth (override the dir with `BOTCIRCUITS_WORKFLOWS_DIR`).
- `.botcircuits/workflows/.build/` — built, runnable copies. **This is the only thing the runtime loads.**
- `.botcircuits/workflows/.runs/` — transient pause/resume cursors for in-progress runs.

### 3. Running

```
claude > "run order fulfillment for order #1024"
```

> Same thing inside Hermes (`hermes "run order fulfillment …"`). The host agent follows the skills and shells out to the `botcircuits` CLI for you.

**What the build buys you at run time:** because navigation was compiled ahead of time, the **deterministic engine** — not the AI — decides which step comes next. The engine loads the built workflow from `.build/`, walks the state machine step by step, evaluates the compiled `choices[]` to pick each branch, and dispatches only the current action to the AI agent along with just the variables that step needs. The result: the same inputs always follow the same path, every step is traceable, and the agent never burns tokens reasoning about routing.

You can also run directly via the CLI:

```bash
botcircuits workflow run --name order_fulfillment --initial-args '{"order_id": "1024"}'
```

#### Workflow Shape

A workflow is one JSON file under `.botcircuits/workflows/`:

```json
{
  "name": "order_fulfillment",
  "description": "Check stock, then ship or backorder.",
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
- Step types are `start`, `agentAction`, and `question`. 
- To branch, attach a `conditions` list at the **step root** (a sibling of `type` and `next`)

## Argus Web Manager

Use the BotCircuits Manager to author workflows, edit them via the visual Flow UI, and monitor execution traces.

- username/password override with `BOTCIRCUITS_MANAGER_ADMIN_USERNAME` / `_ADMIN_PASSWORD`, or default `admin`/`admin`

```bash
# Start the manager
botcircuits manager start 

# Restart the manager
botcircuits manager restart 

# Stop the manager: 
botcircuits manager stop
```

---

## [Use cases](examples)

| Example | What it does |
|---|---|
| [deep_research_assistant](examples/deep_research_assistant/TASK.md) | Produces a comprehensive research report on a topic. |
| [ai_trends](examples/ai_trends/TASK.md) | workflow that checks current AI trends and summarizes them. |
| [shipment_tracking](examples/shipment_tracking/TASK.md) | Checks the live status of many parcels at once via a carrier API. |
| [lab_results_triage](examples/lab_results_triage/TASK.md) | Clinical lab-results triage against a lab/EHR API. |
| [ci_pipeline_triage](examples/ci_pipeline_triage/TASK.md) | CI/CD pipeline failure triage, fully unattended. |
| [code_review_gate](examples/code_review_gate/TASK.md) | Code review / PR merge gate decision workflow. |
| [pr_merge_gate](examples/pr_merge_gate) | Decides whether a pull request is safe to merge. |
| [deployment_release_gate](examples/deployment_release_gate/TASK.md) | DevOps release-gate decision workflow. |
| [dependency_vulnerability_patrol](examples/dependency_vulnerability_patrol/TASK.md) | Dependency / CVE vulnerability-patrol workflow. |
| [incident_postmortem_pipeline](examples/incident_postmortem_pipeline/TASK.md) | Production-incident postmortem triage pipeline. |
| [test_suite_flakiness_triage](examples/test_suite_flakiness_triage/TASK.md) | Detects and quarantines flaky tests. |

---

## Benchmark

model: `claude-opus-4-8` · 3 use case(s)

```
Accuracy = per-item decisions vs the deterministic oracle. 

Consistency = fraction of repeats agreeing on the modal answer. 

Cost/tokens/latency are per-run averages.

Usage is the agents' real reported usage.
```

### Summary

| | claude-code (bare) | claude-code + argus | Workflow advantage |
|---|---|---|---|
| Mean accuracy | 100% | 100% | = |
| Mean consistency | 1.00 | 1.00 | = |
| Total tokens (sum) | 431,876 | 11,503 | 38× fewer |
| Total cost (sum) | $1.1996 | $0.8602 | 1.4× cheaper |
| Total latency (sum) | 260.8s | 182.1s | 1.4× faster |

### Per use case

### [deep_research_assistant](examples/deep_research_assistant/TASK.md) 

| Metric | claude-code | claude-code-argus | Δ |
|---|---|---|---|
| Accuracy | 100% | 100% | |
| Consistency | 1.00 | 1.00 | |
| Avg tokens | 191,114 | 5,807 | 33× |
| Avg cost | $0.5652 | $0.0870 | 6.5× |
| Avg latency | 146.0s | 76.4s | 1.9× |

#### [shipment_tracking](examples/shipment_tracking/TASK.md) 

_Batch parcel-status classification (carrier API) · repeats: 3_

| Metric | claude-code | claude-code + argus | Δ |
|---|---|---|---|
| Accuracy | 100% | 100% | |
| Consistency | 1.00 | 1.00 | |
| Avg tokens | 186,560 | 2,499 | 75× |
| Avg cost | $0.4873 | $0.1872 | 2.6× |
| Avg latency | 120.2s | 25.0s | 4.8× |

#### [lab_results_triage](examples/lab_results_triage/TASK.md) 

_Per-order clinical triage (lab/EHR API) · repeats: 3_

| Metric | claude-code | claude-code + argus | Δ |
|---|---|---|---|
| Accuracy | 100% | 100% | |
| Consistency | 1.00 | 1.00 | |
| Avg tokens | 153,767 | 5,044 | 30× |
| Avg cost | $0.3665 | $0.3774 | 1.0× |
| Avg latency | 76.9s | 77.6s | 1.0× |

#### [ai_trends](examples/ai_trends/TASK.md) 

_Linear AI-trends summary (completion-graded) · repeats: 3_

| Metric | claude-code | claude-code + argus | Δ |
|---|---|---|---|
| Accuracy | 100% | 100% | |
| Consistency | 1.00 | 1.00 | |
| Avg tokens | 91,549 | 3,960 | 23× |
| Avg cost | $0.3458 | $0.2956 | 1.2× |
| Avg latency | 63.7s | 79.5s | 0.8× |

---

## License

Licensed under the Apache License, Version 2.0 — [LICENSE](LICENSE)

## Built by [BotCircuits](https://botcircuits.ai)
