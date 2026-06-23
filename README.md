# botcircuits-agent

**Workflow-native automation, delivered as skills for the agent you already
use.** Install the BotCircuits skills into your agent (claude-code, hermes, …)
and then just talk to it:

```
claude > "create an order fulfillment workflow with stock check, ship, and backorder branches"
claude > "run order fulfillment"
```

Under the hood a BotCircuits workflow is a **deterministic state machine**: the
engine owns the control flow (branching, ordering, slot evaluation) and is
run-to-run predictable, while your agent supplies the LLM reasoning for each
step. The result is predictable, token-efficient multi-step automation 

![botcircuits-agent-solution](docs/solution.png)

---

## How it works

BotCircuits ships **two skills** your agent loads:

| Skill | The user says… | The agent does… |
|---|---|---|
| **botcircuits-workflow-authoring** | _"create an order fulfillment workflow with …"_ | Writes the workflow JSON and builds it. |
| **botcircuits-workflow-running** | _"run order fulfillment"_ | Kicks off the run and relays results — it does **not** perform the steps itself. |

When a workflow runs, your agent starts it with a single call and then steps
back. The deterministic engine walks the state machine in a background process
and dispatches each action step to **its own separate agent process** (the
`claude-code` runtime spawns one headless `claude` per step). The engine decides
every branch; the per-step process does the work. Control only returns to your
session when a step needs **human feedback** — the run pauses with a question,
you relay it to the user, and resume with their reply. On completion you relay
the summary.

This **external-host** model keeps your interactive session free of the
step-by-step grind: it only starts the run, answers human-feedback pauses, and
reports the result. (You can also run the same workflow in-session with the
inline _self_ runtime, or use the self-contained
[native agent](docs/native-agent.md); see [Runtime Providers](docs/concepts/11-runtime-providers.md).)

### The two paths

The diagram uses one example — an **order fulfillment** workflow: _check stock →
if all in stock, ship; otherwise back-order_.

```
            ┌──────────────────────────────────────────────────────────────┐
            │  AGENT  (Claude / Hermes / …)  — has the intelligence + tools │
            └──────────────────────────────────────────────────────────────┘
                 │                                          │
   "create an order…"                              "run order_fulfillment"
   (authoring path)                                (running path)
                 │                                          │
                 ▼                                          ▼
   ┌───────────────────────────┐          ┌───────────────────────────────────┐
   │ skill: workflow-authoring │          │ skill: workflow-running           │
   │  • LLM writes the JSON    │          │  • find the workflow name         │
   │  • `workflow build`       │          │  • `workflow run --name …`        │
   └───────────────────────────┘          │  • relay {success|failure|pause}  │
                 │                         └───────────────────────────────────┘
                 ▼                                          │ starts
   ┌───────────────────────────┐                           ▼
   │ .botcircuits/workflows/   │          ┌───────────────────────────────────────────┐
   │   order_fulfillment.json  │ ───────► │   WORKFLOW ENGINE  (Python, deterministic)│
   │   .build/…  (runnable)    │  loads   │   owns ALL flow navigation + branching    │
   └───────────────────────────┘          └───────────────────────────────────────────┘
                                                           │
                                            ┌──────────────┴───────────────┐
                                            ▼                              │
                            ┌───────────────────────────────┐             │
                  ┌────────►│ step: check_stock             │             │
                  │         │  action text → run on AGENT ──┼──► action runs on a separate
                  │         └───────────────────────────────┘    AGENT session (it has the
                  │                         │ returns slots       tools); result flows back
                  │   ┌─────────────────────┴───────────────┐
                  │   │ SLOT RESOLVE:                        │
                  │   │  • Tier-0 deterministic (in engine)  │
                  │   │  • else ask AGENT to extract (Tier-2)│
                  │   └─────────────────────┬────────────────┘
                  │                         ▼
                  │         ┌───────────────────────────────┐
                  │         │ BRANCH (pure Python, no LLM):  │
                  │         │  all_items_in_stock == true ?  │
                  │         └───────────────────────────────┘
                  │            yes │                 │ no
                  │                ▼                 ▼
                  │      ┌──────────────┐   ┌──────────────────┐
                  └──────┤ step: ship   │   │ step: backorder  │   (next step's action
                  next   │  (action on  │   │  (action on      │    again runs on the
                         │   AGENT)     │   │   AGENT)         │    AGENT — loop repeats)
                         └──────┬───────┘   └────────┬─────────┘
                                └─────────┬──────────┘
                                          ▼
                            ┌───────────────────────────────────┐
                            │ engine ends → {status, message}   │
                            │ back to the calling AGENT session │
                            └───────────────────────────────────┘
```

Key invariant: the **engine never decides anything with an LLM**. It walks the
compiled state machine, evaluates branches in pure Python, and resolves
deterministic slots in-process. The agent is called for exactly two things —
**performing a step's action** (it has the tools and reasoning the workflow does
not) and **Tier-2 slot extraction** — and the run only pauses back to your
session when a step needs **human feedback**.

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

The skills shell out to the `botcircuits` CLI (e.g. `botcircuits workflow run`),
so the `botcircuits` package must be installed and on PATH in your agent's
environment — the step above provides it. No LLM API key is needed: your host
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
into the agent's skills directory (the `botcircuits-` prefix keeps them clearly
separated from any other skills there). Your agent now picks them up by
description.

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
`flow.variables` list, so the engine picks branches without re-calling the LLM:

```bash
botcircuits workflow build --name order_fulfillment
```

The runtime only loads from `.botcircuits/workflows/.build/`. The authoring
skill builds for you automatically.

### Where things live

- `.botcircuits/workflows/*.json` — your authored sources (override the dir with
  `BOTCIRCUITS_WORKFLOWS_DIR`).
- `.../.build/` — built, runnable copies.
- `.../.runs/` — transient pause/resume cursors (gitignored).

---

## Skills

A **skill** is a folder with a `SKILL.md` an agent reads from disk. BotCircuits
ships its functionality *as* skills:

```
skills/
├── botcircuits-workflow-authoring/SKILL.md
├── botcircuits-workflow-running/SKILL.md
└── botcircuits-faq/SKILL.md
```

`SKILL.md` frontmatter declares a `name` and a `description` (which the agent
uses to decide when to invoke it); `allowed-tools` (optional) restricts which
tools the skill may call. The same folders work in any agent that supports
Claude-Code-style filesystem skills.

---

## Native agent (self-contained, optional)

BotCircuits also ships a **complete standalone agent** — an LLM-driven CLI with
its own provider adapters (Anthropic / OpenAI / Gemini), MCP, persistent memory,
streaming, and a FastAPI gateway for WhatsApp / Slack / webhooks / cron. It's the
`native` runtime provider and the default fallback when no host agent is
detected.

Setup, CLI, tool-use modes, slash commands, MCP, built-in tools, and the message
gateway all live in **[docs/native-agent.md](docs/native-agent.md)**.

---

## Manager & execution tracing

Every workflow run is **traced** to its own session file under
`.botcircuits/sessions/<session_id>-session.json` — the engine records when it
received the request, each step entered, before/after each action (with the
sub-agent's input, output, and duration), slot resolutions, branch decisions,
and the slot snapshot at each point. A paused → resumed run keeps the same
`session_id`, so its trace is one continuous timeline.

The **Manager** surfaces these traces:

- **Backend** (`src/botcircuits/manager`, FastAPI) — username/password auth from
  `BOTCIRCUITS_MANAGER_ADMIN_USERNAME` / `_ADMIN_PASSWORD`, with read-only
  session APIs. Run it with `botcircuits-manager` (default port 8700).
- **Web** (`manager_web`, Next.js + Tailwind + ReactFlow) — lists sessions and
  renders each as a **trace graph + memory flow** alongside an event timeline.
  Light/dark themes. See [manager_web/README.md](manager_web/README.md).

```bash
export BOTCIRCUITS_MANAGER_ADMIN_USERNAME=admin BOTCIRCUITS_MANAGER_ADMIN_PASSWORD=change-me
botcircuits-manager                          # backend  :8700
cd manager_web && npm install && npm run dev # web       :3700
```

---

## Documentation

- [Concepts](docs/concepts/00-index.md) — a concept-level tour (incl. [Runtime Providers](docs/concepts/11-runtime-providers.md)).
- [Implementation Guide](IMPLEMENTATION.md) — architecture & internals (incl. [Runtime Providers](docs/developer-guide/14-runtime-providers.md)).
- [Native Agent](docs/native-agent.md) — the self-contained BotCircuits agent.

---

## License

Licensed under the Apache License, Version 2.0 — [LICENSE](LICENSE)

## Built by [BotCircuits](https://botcircuits.ai)
