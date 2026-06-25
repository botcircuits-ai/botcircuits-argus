# BotCircuits Argus

**Argus** helps AI agents (Claude, Codex, OpenClaw, Hermes, etc.) run your workflows **predictably**, **traceably**, and **cost-efficiently**. It achieves this through a combination of structured flow control and stateful memory context.

```
claude > "create an order fulfillment workflow with stock check, ship, and backorder branches"
claude > "run order fulfillment"
```

## Structured flow control 
Traditionally, AI agents are left to guess their next move, leading to unpredictable reasoning loops. By defining your processes as declarative workflows, the engine acts as the primary navigator. It handles all routing, ensuring the AI only executes actions when explicitly directed. This guarantees that every task follows a secure, approved, and repeatable path.

## Stateful memory context
Complex tasks require perfect continuity. Argus automatically capture changes in memory states. Instead of forcing the AI to rely on a long, fragile context window, Argus provides the agent with exactly what is needed for the next immediate step. This keeps the agent entirely focused, boosting overall reliability while significantly lowering token costs.


![botcircuits-agent-solution](docs/solution.png)

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

### 1. Install a host agent (if you don't have one)

Argus is driven by a host AI coding agent. Install whichever you prefer, for example:
- **Claude Code**
- **Hermes**

### 2. Install BotCircuits Argus

```bash
curl -fsSL https://raw.githubusercontent.com/botcircuits-ai/botcircuits-argus/main/scripts/install.sh | bash
```

### 3. Install the skills

Install the workflow skills into your host agent (defaults to `~/.claude/skills`):

```bash
botcircuits skills install [--agent claude|hermes] [--link]
```

- `--agent` — target host agent (default: `claude`).
- `--link` — symlink the skills instead of copying, so updates to Argus are picked up automatically.

### 4. Select the runtime (`settings.runtime`)

Argus dispatches work to an **agent runtime** — the host that actually carries out the work, both when **authoring/building** a workflow and when **running** one. It resolves which runtime to use in this order (first hit wins). If no runtime set, default use **claude**:

1. The `BOTCIRCUITS_RUNTIME` environment variable.
2. The `runtime` key in `.botcircuits/settings.json`.
Supported values: `claude-code`, `codex`, `openclaw`, `hermes`.

```json
{
  "runtime": "hermes"
}
```

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

The authoring skill builds for you automatically. You can also build (or rebuild after a manual edit) explicitly:

```bash
botcircuits workflow build --name order_fulfillment
```

> **Always rebuild after editing the raw source.** The engine never reads your `*.json` source directly — only the `.build/` copy. Until you rebuild, your changes won't take effect at run time.

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
---

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

## License

Licensed under the Apache License, Version 2.0 — [LICENSE](LICENSE)

## Built by [BotCircuits](https://botcircuits.ai)
