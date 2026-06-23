# Native Agent Provider

[← README](../README.md)

---

BotCircuits ships its **own** agent loop — an LLM-driven, multi-round tool-use
agent with providers for Anthropic / OpenAI / Gemini, MCP, persistent memory,
streaming, and a FastAPI gateway. This is the **native runtime provider**: one
of several ways to run a workflow (see [Runtime Providers](concepts/11-runtime-providers.md)),
and the default fallback when no external host agent is detected.

> **You usually don't need this.** If you're running workflows inside an
> existing agent (claude-code, codex, …), use the **botcircuits-workflow-running** skill —
> see the [README](../README.md). This document covers the *self-contained*
> BotCircuits agent: its CLI, configuration, tools, MCP, and gateway.

---

## Setup

### Clone and install

```bash
# 1. Install uv (skip if you already have it)
curl -LsSf https://astral.sh/uv/install.sh | sh
# or: brew install uv

# 2. Clone the repo
git clone https://github.com/botcircuits-ai/botcircuits-agent
cd botcircuits-agent

# 3. Pick a Python (3.11+) and create the project venv
uv python install 3.11
uv venv --python 3.11        # creates ./.venv

# 4. Activate the venv
source .venv/bin/activate    # bash / zsh

# 5. Install dependencies into the venv
uv sync
```

Configure your LLM provider, model, and API key:

```bash
botcircuits setup
```

The wizard walks you through provider (`anthropic` / `openai` / `gemini`), model, and API key with arrow-key navigation (↑/↓ to move, Enter to select, Esc to keep the current value). Each pick is saved as you go:

- `provider` and `model` → `~/.botcircuits/settings.json`
- API key → `~/.botcircuits/.env` (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY`, file mode `0600`)

Re-running `botcircuits setup` shows your existing values as defaults, and an existing API key gives you a **Keep / Replace / Clear** choice instead of re-prompting for the secret.

| Form | What it does |
|---|---|
| `botcircuits setup` | Full wizard (currently the LLM section) |
| `botcircuits setup llm` | Just the LLM provider/model/API-key section |
| `botcircuits setup --user` | Write to `~/.botcircuits/` (default) |
| `botcircuits setup --project` | Write to `./.botcircuits/settings.json` (shared via VCS) |
| `botcircuits setup --local` | Write to `./.botcircuits/settings.local.json` (gitignored personal override) |

Prefer to configure by hand? Copy the env template instead:

```bash
cp .env.example .env
```

```bash
# .env — API key for the provider you want to use (required)
ANTHROPIC_API_KEY=...
OPENAI_API_KEY=...
GEMINI_API_KEY=...

# Optional — only used as a fallback when settings.json / CLI flags don't set them
LLM_PROVIDER=anthropic        # anthropic | openai | gemini
ANTHROPIC_MODEL=claude-opus-4-7
OPENAI_MODEL=gpt-4.1
GEMINI_MODEL=gemini-2.5-flash
```

Effective precedence (highest wins): **CLI flag → `settings.json` (layered) → env var → built-in default**.

---

## Run

Interactive CLI:

```bash
botcircuits
botcircuits --provider openai
```

Pipe a single message (non-interactive):

```bash
echo "what is 2+2?" | botcircuits --no-stream
```

FastAPI gateway (for HTTP + messaging channels):

```bash
uv run uvicorn botcircuits.gateway:app --reload --port 8000
# or
botcircuits-gateway
```

### Useful CLI flags

| Flag | Description |
|---|---|
| `--provider` | `anthropic` (default) \| `openai` \| `gemini` |
| `--model` | Override the provider's default model |
| `--stream` / `--no-stream` | Force streaming on/off |
| `--auto` | Skip y/N confirmation on every gated tool. A warning still prints before each action. |
| `--config <path>` | Load a specific `settings.json` (in addition to the auto-discovered files) |

### Settings files (optional)

The CLI auto-loads these in order (later layers win):

| Layer | Path |
|---|---|
| User | `~/.botcircuits/settings.json` |
| Project (shared) | `.botcircuits/settings.json` |
| Project (local) | `.botcircuits/settings.local.json` (gitignored) |

CLI flags always win over JSON. A starter file is at [.botcircuits/settings.example.json](../.botcircuits/settings.example.json).

### Tool-use mode

`"mode"` selects how the agent invokes tools:

| Mode | Behaviour |
|---|---|
| `"native"` (default) | Tools are handed to the provider's structured function-calling API; tool calls are read back as typed objects. Most robust. |
| `"react"` | Tools are described in the system prompt and the model emits a `Thought / Action / Action Input` text block, parsed by the agent and fed back as an `Observation:`. Works on any provider regardless of native tool-use quality, and exposes a visible reasoning trace — at the cost of parse brittleness. Classic ReAct (one action per turn). |

Both modes run the same loop and expose the same tools; only the call/parse mechanism differs. Set it in `settings.json` (`"mode": "react"`).

### Slash commands inside the CLI

| Command | Action |
|---|---|
| `/help` | Show all commands |
| `/reset` | Clear the current session and start fresh |
| `/session [id]` | Show or switch session id |
| `/stream on\|off` | Toggle streaming |
| `/tools` | List the tools the model can call |
| `/skills` | List filesystem skills |
| `/memory` | Show persistent memory |
| `/workflow add "<prompt>"` | Author a new workflow from natural language |
| `/workflow add --file <path.md>` | Author a new workflow from a prompt in a Markdown file |
| `/workflow edit "<prompt>" --name <wf>` | Edit an existing workflow |
| `/workflow run --name <wf> [--initial-args '{"k":"v"}']` | Force-start a workflow tool, bypassing the model's tool choice |
| `/quit` | Exit |

Type `"""` on its own line to start (and again to end) a multi-line message.

---

## Authoring & running workflows from the CLI

The native agent can author workflows for you through the `/workflow` slash command (no hand-editing JSON):

```
botcircuits
```
```
/workflow add --name workflow_demo "Create a workflow with 11 steps total (step_1 through step_10 plus an end step) ..."
```

The agent drafts the workflow, shows you a preview, asks `y/N`, writes the file, and registers it as a tool. The new workflow becomes callable on the very next message — no restart.

Pass `--name <wf>` to set the slug yourself (it becomes both the filename `.botcircuits/workflows/<wf>.json` and the registered tool name; must match `^[a-zA-Z0-9_-]+$`). For a long or reusable prompt, keep it in a Markdown file and point `--file` at it (mutually exclusive with an inline `"<prompt>"`):

```
/workflow add --file ./prompts/check_order_status.md --name check_order_status
/workflow edit "also handle refunds" --name check_order_status
```

**Force-run** a workflow directly instead of leaving it to the model:

```
/workflow run --name workflow_demo
/workflow run --name workflow_demo --initial-args '{"end_id":"step_3"}'
```

This calls the workflow tool right away with the args you supplied, seeds the conversation with the resulting first step, and hands control back to the model. `--initial-args` must be a JSON object; omit it to start with `{}`. The target must already be registered — workflow tools are auto-discovered from `.botcircuits/workflows/.build/`, and the command refreshes that registry first, so a freshly authored workflow works without a restart.

> The **workflow shape**, the **build step**, and **where workflows live** are
> the same regardless of runtime — see the [README](../README.md#workflows).

---

## MCP

[MCP](https://modelcontextprotocol.io) servers expose external tools (filesystem, GitHub, databases, …) to the native agent. Configure them once and they're available across every CLI session and gateway request.

### Where MCP servers live

MCP server configs live in `mcp.json` files, layered the same way as `settings.json`:

| Layer | Path |
|---|---|
| User | `~/.botcircuits/mcp.json` |
| Project (shared) | `.botcircuits/mcp.json` |
| Project (local) | `.botcircuits/mcp.local.json` (gitignored) |

Two modes:

- **`local`** — the agent runs the MCP server in-process. Works with every provider, including Gemini.
- **`hosted`** — the provider executes the MCP server itself (Anthropic and OpenAI only). Gemini auto-promotes hosted entries to local.

### Managing from the CLI

```bash
# List configured servers
botcircuits mcp list

# Add a local stdio server (writes to .botcircuits/mcp.json)
botcircuits mcp add fs \
    --mode local --transport stdio --command npx \
    --args -y @modelcontextprotocol/server-filesystem /tmp

# Add a hosted server to your user-wide mcp.json
botcircuits mcp add github --user \
    --mode hosted --url https://api.githubcopilot.com/mcp/ \
    --authorization-token "$GITHUB_PAT"

# Personal override that won't be committed (auto-added to .gitignore)
botcircuits mcp add fs-debug --local \
    --mode local --transport stdio --command npx --args -y debug-fs /tmp

# Connect, list tools, disconnect — verifies a local server works
botcircuits mcp test fs

# Remove a server
botcircuits mcp remove github
```

If a flag value starts with `-`, use the `--flag=value` form (e.g. `--args=-y,pkg,/tmp`) so argparse doesn't read it as a flag.

---

## Tools

The native agent ships with built-in tools so the model can read files, run commands, search code, and keep a TODO list. Run `/tools` inside the CLI to see what's currently loaded.

| Tool | What it does | Gate |
|---|---|---|
| `now` | Current UTC time (ISO 8601) | — |
| `read_file` | Read a UTF-8 text file | — |
| `write_file` | Create or overwrite a file | y/N + preview |
| `edit_file` | Exact string-replace in a file | y/N + unified diff |
| `list_dir` | List a directory | — |
| `glob_search` | Find files by glob (`**/*.py`) | — |
| `grep_search` | Regex search across files | — |
| `todo_write` | Maintain a live TODO list | — |
| `plan_and_confirm` | Present a plan and gate execution | y/N + plan preview |
| `shell_exec` | Run a system command (background mode supported) | y/N + argv preview |
| `shell_status` | Poll a background process | — |
| `shell_stop` | Terminate a background process | y/N |
| `memory` | Read/write the persistent agent memory | — |
| `build_workflow` | Author a workflow JSON. **Loaded on demand** via `/workflow add\|edit`. | y/N + workflow preview |

There is no shell expansion — `shell_exec` takes an `argv` list, so pipes/redirects/globs don't work; the model has to break the command apart itself.

### Approving gated actions

Before any gated tool runs, the agent pauses and shows you what it's about to do:

```
  ▸ shell_exec proposes:
      cmd:  git status
      run? [y/N]:
```

Press `y` (or `yes`) to allow, Enter (or anything else) to deny. A denied tool returns `{"denied": true, ...}` to the model along with a hint not to retry.

### Auto mode

`--auto` (or `tools.<name>.auto: true` in `settings.json`) skips the y/N prompt. A warning banner still prints so you can see what ran:

```
  ⚠ shell_exec running (auto mode):
      cmd:  git status
```

Non-interactive contexts (the gateway, piped stdin) engage auto mode automatically — otherwise every gated tool would deadlock waiting for input that never arrives.

### Tuning or disabling tools

Per-tool config lives under a `tools` block in any `settings.json`:

```json
{
  "tools": {
    "shell_exec": { "timeout_seconds": 60, "max_output_bytes": 20000 },
    "write_file": { "auto": false, "max_bytes": 2000000 },
    "now": null
  }
}
```

- A dict → override the tool's defaults.
- `null` or `false` → disable the tool entirely.
- Omitted → keep the built-in defaults.

Unknown tool names or unknown keys are rejected at startup with a clear error.

---

## Message Gateway

The same FastAPI process can connect the native agent to messaging platforms. One process can serve WhatsApp, Slack, generic webhooks, and a built-in cron scheduler — all routed through the same agent and conversation store.

```bash
uv run uvicorn botcircuits.gateway:app --reload --port 8000
```

### Supported channels

| Channel | Inbound | Outbound |
|---|---|---|
| WhatsApp | Meta Cloud API webhook (`POST /messaging/whatsapp`) | Graph API |
| Slack | **Socket Mode** (outbound WebSocket — no public URL required) | `chat.postMessage` |
| Webhook | `POST /messaging/webhook` (Bearer auth) | POST to a configured `outbound_url` (optional) |
| Cron | Synthesized every 60 seconds | Logged, or forwarded to another channel |

Each user gets an independent conversation history per channel — sessions are keyed by `{channel}:{external_chat_id}`.

### Enabling a channel

A channel registers automatically when its credentials are present. Anything left blank is skipped silently.

```bash
# WhatsApp (all three required to enable)
WHATSAPP_PHONE_NUMBER_ID=123456789
WHATSAPP_ACCESS_TOKEN=EAA…
WHATSAPP_VERIFY_TOKEN=any-shared-secret      # echoed back during Meta's GET verify

# Slack — Socket Mode (no public URL required)
SLACK_BOT_TOKEN=xoxb-…                       # bot token, for chat.postMessage
SLACK_APP_TOKEN=xapp-…                       # app-level token, scope: connections:write

# Generic webhook
WEBHOOK_OUTBOUND_URL=https://your-app.example.com/incoming   # optional
WEBHOOK_TOKEN=shared-bearer-token                            # optional (recommended)
```

Check which channels are live:

```bash
curl http://localhost:8000/messaging/status
```

### Platform setup notes

**WhatsApp.** In the Meta WhatsApp Business app, set the webhook URL to `https://<your-host>/messaging/whatsapp` and the verify token to whatever you put in `WHATSAPP_VERIFY_TOKEN`. Subscribe to the `messages` field on the WhatsApp Business Account.

**Slack (Socket Mode).** Create a Slack app, enable Socket Mode, and generate an app-level token with `connections:write` (→ `SLACK_APP_TOKEN`). Add bot scopes `chat:write`, `channels:history`, `groups:history`, `app_mentions:read`, `im:history`, `im:write`, `users:read` and install the workspace (→ `SLACK_BOT_TOKEN`). Subscribe to bot events `message.im`, `message.channels`, `message.groups`, `app_mention`. Missing `message.channels` / `message.groups` is the most common setup mistake — the bot will reply in DMs but appear dead in channels.

**Generic webhook.**

```bash
curl -X POST http://localhost:8000/messaging/webhook \
  -H "authorization: Bearer $WEBHOOK_TOKEN" \
  -H "content-type: application/json" \
  -d '{"chat_id": "user-42", "text": "What is the weather like?"}'
```

If `WEBHOOK_OUTBOUND_URL` is set, the gateway posts the agent's reply back to that URL with the same bearer token.

### Cron scheduler

The cron channel ticks every 60 seconds. Each due job synthesizes an inbound message — the agent treats it like a user request and the reply is either logged or forwarded to another channel. Jobs live in `.botcircuits/messaging.json`:

```json
{
  "cron": {
    "enabled": true,
    "jobs": [
      {
        "name": "daily-standup",
        "prompt": "Summarize yesterday's merged PRs and post a standup.",
        "schedule": "0 9 * * 1-5",
        "deliver_to_channel": "slack",
        "deliver_to_chat_id": "C0123456789"
      },
      {
        "name": "hourly-health",
        "prompt": "Check the production health endpoint and report anomalies.",
        "schedule": "0 * * * *"
      }
    ]
  }
}
```

Schedules use standard 5-field cron expressions evaluated in **UTC** (`*`, literals, `A-B` ranges, `*/S` steps, comma lists). Day-of-week accepts `0` or `7` for Sunday.

### Terminal
##### SSH ( TODO )
##### Docker ( TODO )

### Deployment
##### Docker-Compose ( TODO )
##### Kubernetes ( TODO )
