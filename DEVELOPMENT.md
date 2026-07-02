# Developer Setup Guide

This guide covers how to set up the **BotCircuits Argus** project for local development, run tests, and work on the Python backend, CLI, and Next.js manager UI.

---

## Quick start

```bash
# 1. Clone the repo
git clone https://github.com/botcircuits-ai/botcircuits-argus.git
cd botcircuits-argus

# 2. Install Python dependencies (requires Python >=3.11)
uv sync

# 3. Configure environment
cp .env.example .env
# edit .env and add at least one LLM API key

# 4. Run unit tests
uv run pytest

# 5. Install the CLI into the project you're working on
uv run botcircuits init
```

---

## Prerequisites

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** — Python package/environment manager used by this project
- **Node.js 18+** and **npm** — only if you plan to run the Argus Web Manager frontend
- A host AI agent **(Claude Code or Hermes)** if you want to drive workflows through an agent conversation rather than the CLI directly
- API keys for the LLM providers you plan to use: **Anthropic**, **OpenAI**, and/or **Google/Gemini**

---

## Project structure

```
├── src/botcircuits/           Python source (src layout)
│   ├── agent/                 Core agent loop, tools, skills, MCP, persistence
│   ├── agent/workflow/        Workflow authoring, optimizer, condition processor,
│   │                          engine handlers, runner, executor, state
│   ├── cli/                   botcircuits console entry point and commands
│   ├── gateway/               FastAPI messaging gateway (Slack, WhatsApp, webhook, cron)
│   ├── manager/               FastAPI backend for the Web Manager
│   ├── providers/             LLM provider adapters (anthropic, openai, gemini)
│   ├── runtime/               Host-agent runtime abstraction
│   └── usage/                 Token/cost accounting
├── tests/                     pytest unit-test collection
├── evals/                     deepeval-based evaluation suite (optional extra)
├── manager_web/               Next.js frontend for the Web Manager
├── skills/                    Claude/Hermes skill definitions
├── examples/                  End-to-end use cases
├── .botcircuits/              Per-project runtime config and workflow storage
└── pyproject.toml             Project metadata, deps, pytest config
```

---

## Python environment

Install all project dependencies (including editable install of `botcircuits`):

```bash
uv sync
```

This creates a virtual environment managed by `uv`. Use `uv run` to execute commands inside it.

### Add eval dependencies

The evaluation harness in `evals/` requires the optional `evals` dependency group:

```bash
uv sync --extra evals
```

---

## Configuration

### Environment variables

Copy the example file and fill in the values you need:

```bash
cp .env.example .env
```

Required for any run that calls an LLM:

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | `anthropic`, `openai`, or `gemini` |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `GEMINI_API_KEY` | Google Gemini API key |

Optional overrides:

| Variable | Purpose |
|---|---|
| `ANTHROPIC_MODEL` / `OPENAI_MODEL` / `GEMINI_MODEL` | Model selection |
| `BOTCIRCUITS_ENV_FILE` | Custom `.env` file path |
| `BOTCIRCUITS_WORKFLOWS_DIR` | Override `.botcircuits/workflows/` location |

### BotCircuits settings

`botcircuits init` creates `.botcircuits/settings.json` from `settings.example.json`. This file controls the active runtime, model, tool behavior, and MCP servers. You can edit it directly:

```bash
uv run botcircuits init
```

Key fields:

| Field | Default | Purpose |
|---|---|---|
| `provider` | `anthropic` | LLM provider |
| `model` | `claude-opus-4-7` | Model name |
| `mode` | `native` | Execution mode |
| `stream` | `true` | Stream LLM responses |
| `max_tokens` | `4096` | Max tokens per LLM call |
| `max_steps` | `10` | Max agent steps |
| `tools.shell_exec` | object | Shell tool timeout/output/auto settings |

---

## Running tests

### Unit tests

```bash
uv run pytest
```

The default `pytest` collection is limited to the `tests/` directory by `pyproject.toml`:

```toml
[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

### Eval suite

The `evals/` directory uses `deepeval` and is **not** collected by default. Run it with:

```bash
uv run deepeval test run evals/test_task_completion.py
uv run deepeval test run evals/test_tool_correctness.py
```

You must install the `evals` extra (`uv sync --extra evals`) first.

---

## CLI development workflow

The `botcircuits` command is the primary interface. During development always invoke it through `uv run`:

```bash
uv run botcircuits --help
```

### Initialize a project

```bash
uv run botcircuits init
```

Options:

- `--dir <path>` — target another folder
- `--runtime claude-code|hermes` — seed `settings.runtime` and install that runtime's skills
- `--link` — symlink skills instead of copying, so source changes are picked up immediately

### Install/update skills

```bash
uv run botcircuits skills install
uv run botcircuits skills install --agent hermes --link
```

### Build and run workflows

Author workflows in `.botcircuits/workflows/<name>.json`, then build them:

```bash
uv run botcircuits workflow build --name <workflow_name>
```

Built artifacts land in `.botcircuits/workflows/.build/<name>.json` and are the only files the engine loads.

Run a built workflow:

```bash
uv run botcircuits workflow run --name <workflow_name> --initial-args '{"key": "value"}'
```

### MCP management

```bash
uv run botcircuits mcp list
uv run botcircuits mcp add <name> <transport> <uri_or_command>
```

---

## Argus Web Manager

The Manager has a **FastAPI backend** (`manager/`) and a **Next.js frontend** (`manager_web/`).

### Backend

From the repo root:

```bash
export BOTCIRCUITS_MANAGER_ADMIN_USERNAME=admin
export BOTCIRCUITS_MANAGER_ADMIN_PASSWORD=change-me
uv run botcircuits manager start --backend-only
```

Backend URL: `http://127.0.0.1:8700`

Other commands:

```bash
uv run botcircuits manager restart
uv run botcircuits manager stop
```

### Frontend

```bash
cd manager_web
cp .env.example .env.local
npm install
npm run dev
```

Frontend URL: `http://localhost:3700`

Sign in with the same `BOTCIRCUITS_MANAGER_ADMIN_*` credentials used for the backend.

### Frontend production build

```bash
cd manager_web
npm run build && npm start
```

---

## Gateway (optional)

The FastAPI gateway exposes workflow/agent access over messaging channels. To run it directly:

```bash
uv run python -m botcircuits.gateway.app
```

Configure Slack/WhatsApp/webhook credentials in `.env` (see `.env.example`) before enabling those channels.

---

## Common development commands

| Task | Command |
|---|---|
| Install deps | `uv sync` |
| Install deps + evals | `uv sync --extra evals` |
| Run unit tests | `uv run pytest` |
| Run one test file | `uv run pytest tests/test_workflow_engine_runner.py` |
| Run with verbose output | `uv run pytest -v` |
| Run evals | `uv run deepeval test run evals/test_task_completion.py` |
| Format/lint (if configured) | `uv run ruff check .` / `uv run ruff format .` |
| CLI help | `uv run botcircuits --help` |
| Initialize project | `uv run botcircuits init` |
| Build workflow | `uv run botcircuits workflow build --name <name>` |
| Run workflow | `uv run botcircuits workflow run --name <name>` |
| Start manager backend | `uv run botcircuits manager start --backend-only` |
| Start manager frontend | `cd manager_web && npm run dev` |

---

## Workflow file layout

When working with workflows locally, keep the generated artifacts in mind:

| Path | Purpose |
|---|---|
| `.botcircuits/settings.json` | Runtime config |
| `.botcircuits/workflows/*.json` | Hand-editable authored sources |
| `.botcircuits/workflows/.build/*.json` | Built, deterministic state machines (runtime input only) |
| `.botcircuits/workflows/.runs/` | Pause/resume cursors for in-progress runs |
| `.botcircuits/sessions/*-session.json` | Execution traces consumed by the Manager |

You can move the workflows directory with:

```bash
export BOTCIRCUITS_WORKFLOWS_DIR=/absolute/path/to/workflows
```

---

## GitNexus code intelligence

This project is indexed by GitNexus. When modifying code, follow the safety rules in `AGENTS.md`:

1. Run impact analysis before editing symbols.
2. Run `gitnexus_detect_changes()` before committing.
3. Do not rename symbols with find-and-replace; use `gitnexus_rename`.

If the index is stale, refresh it:

```bash
npx gitnexus analyze
```

---

## Tips

- Use `uv run` for **every** Python command so you run inside the project's environment.
- Use `--link` when installing skills during active skill development so you don't have to reinstall after every change.
- Keep authored workflow JSON in `.botcircuits/workflows/`; never edit `.botcircuits/workflows/.build/` directly — it is regenerated by `botcircuits workflow build`.
- Run the unit-test suite before opening a PR; evals are optional unless you changed workflow authoring or evaluation logic.

---

## License

Licensed under the Apache License, Version 2.0 — see [LICENSE](LICENSE).
