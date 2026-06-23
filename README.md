# Botcircuits Argus

**Argus** helps your AI agent (Claude, Codex, OpenClaw, Hermes, etc.) run tasks predictably, traceably, and cost-efficiently. It achieves this through a combination of structured flow control and stateful memory context.

```
claude > "create an order fulfillment workflow with stock check, ship, and backorder branches"
claude > "run order fulfillment"
```

## A better model for execution
Traditionally, AI agents are left to guess their next move, leading to unpredictable reasoning loops. Argus SKILL introduces a much more effective model: structured flow control.

## Stateful memory context
Complex tasks require perfect continuity. This SKILL enables your agent to handle multi-step workflows without ever losing track.


![botcircuits-agent-solution](docs/solution.png)

---

## How it works

Argus ships **two skills** your agent loads:

| Skill | The user says… | The agent does… |
|---|---|---|
| **botcircuits-workflow-authoring** | _"create an order fulfillment workflow with …"_ | Writes the workflow JSON and builds it. |
| **botcircuits-workflow-running** | _"run order fulfillment"_ | Kicks off the run and relays results — it does **not** perform the steps itself. |

```
When a workflow runs, the deterministic engine walks the state machine in a background process and dispatches each action step to its own separate agent process.
```
---

## Quick Start

### 1. Install the package

```bash
# Install uv if you don't have it: https://docs.astral.sh/uv/
git clone https://github.com/botcircuits-ai/botcircuits-agent
cd botcircuits-agent
uv venv --python 3.11 && source .venv/bin/activate
uv sync
```

The skills shell out to the `botcircuits` cli. no LLM API key is needed: your host
agent brings its own model.

### 2. Install the skills into your agent

```bash
# Claude Code (personal scope, ~/.claude/skills) — the default
scripts/install-skills.sh

# Project scope, or another agent (e.g. Hermes):
scripts/install-skills.sh --target .claude/skills
scripts/install-skills.sh --target ~/.hermes/skills

# Develop against the repo (symlink instead of copy):
scripts/install-skills.sh --link
```

This copies `botcircuits-workflow-authoring` and `botcircuits-workflow-running`
into the agent's skills directory.

### 3. Use them in natural language

```
# Author
claude > "create an order fulfillment workflow: check stock; if all items are
          in stock, ship; otherwise create a backorder and notify the customer"

# Run
claude > "run order fulfillment for order #1024"
```

The authoring skill writes `.botcircuits/workflows/order_fulfillment.json` and
builds it; the running skill kicks off the engine, which runs each step in its
own agent process, pausing to ask you for input only when a step needs human
feedback, and reporting the result at the end.

---

## Workflows

### Shape

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

`name` is the identifier; it must match `^[a-zA-Z0-9_-]+$`. Step types are
`start`, `agentAction`, `question`, and `systemAction`. To branch, attach a
`conditions` list at the **step root** (a sibling of `type`/`next`, not nested
in `settings`); the step's own `next` is the default ("otherwise") branch.

### Build

The raw file is *not* what runs. **Building** compiles each natural-language
`condition` into a deterministic `choices[]` entry and emits an aggregated
`flow.variables` list.

The runtime only loads from `.botcircuits/workflows/.build/`. The authoring
skill builds for you automatically.

### Where things live

- `.botcircuits/workflows/*.json` — your authored sources (override the dir with
  `BOTCIRCUITS_WORKFLOWS_DIR`).
- `.botcircuits/workflows/.build/` — built, runnable copies.
- `.botcircuits/workflows/.runs/` — transient pause/resume cursors.

---

## Skills

A **skill** is a folder with a `SKILL.md` an agent reads from disk. BotCircuits
ships its functionality *as* skills:

```
skills/
├── botcircuits-workflow-authoring/SKILL.md
├── botcircuits-workflow-running/SKILL.md 
```

`SKILL.md` frontmatter declares a `name` and a `description` (which the agent
uses to decide when to invoke it); `allowed-tools` (optional) restricts which
tools the skill may call. The same folders work in any agent that supports

---

## Argus Web Manager

Use the BotCircuits Manager to author workflows, edit them via the visual Flow UI, and monitor execution traces.

- username/password override with `BOTCIRCUITS_MANAGER_ADMIN_USERNAME` / `_ADMIN_PASSWORD`, 
- **Manager Web** — See [manager_web/README.md](manager_web/README.md).

```bash
# Start the manager
botcircuits manager start 

# Restart the manager
botcircuits manager restart 

# Stop the manager: 
botcircuits manager stop
```

---

## License

Licensed under the Apache License, Version 2.0 — [LICENSE](LICENSE)

## Built by [BotCircuits](https://botcircuits.ai)
