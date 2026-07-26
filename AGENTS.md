# Argus (botcircuits-agent)

Workflow-native AI agent framework: instead of letting an LLM reason through
every step of a repetitive process, Argus compiles a natural-language process
description into a deterministic state machine. A lightweight engine drives
navigation between steps; the LLM is invoked only to execute the current
step's action, receiving just the variables that step needs. This cuts token
usage (~80% per README benchmarks) while keeping execution traceable and
repeatable.

## Workflow lifecycle

1. **Author** — natural language → `.botcircuits/workflows/<name>.json` (source of truth, hand-editable).
2. **Build** — compiles `condition` strings into deterministic `choices[]` and aggregates `flow.variables` → `.botcircuits/workflows/.build/<name>/<name>.json`. **Only `.build/` is loaded at runtime.** Also auto-generates a **verification gate** — deterministic script checks + LLM-judge checks derived from the workflow's declared result/variables — under `.botcircuits/workflows/.build/<name>/verifications/` (`agent/workflow/verification/generator.py`).
3. **Run** — the deterministic engine walks the built state machine, evaluates compiled choices, and dispatches one `agentAction`/`question` step at a time to the host agent. Pause/resume cursors live in `.botcircuits/workflows/.runs/`. If a gate exists, `workflow run` checks the finished run against it and **self-repairs** by retrying the whole workflow (capped, with the failure fed back as context) before reporting a genuine failure (`agent/workflow/verification/executor.py::run_gate`, wired into `runtime/run_workflow.py::_run`).

Two Claude/Hermes skills drive this from a host agent: `skills/botcircuits-workflow-authoring` (author + build) and `skills/botcircuits-workflow-running` (run/resume via the `botcircuits` CLI).

## Source layout (`src/botcircuits/`)

- `agent/` — the core agent loop (`core.py` `Agent`, ReAct parsing in `react.py`), tool registry (`agent/tools/builtins/*`), skill discovery (`agent/skill/`), MCP client (`agent/mcp.py`), and conversation persistence (`agent/store.py`).
- `agent/workflow/` — workflow authoring (`generator.py`, `graph_optimizer.py`, `condition_processor.py`), path resolution (`paths.py` — the single source of truth for `.build/<name>/...` paths), the runnable `engine/` (step `handlers/` for action/choice/question, `executor.py`, `runner.py`, `segment_exec.py`, `state.py`), and `verification/` (the verification-gate framework: `executor.py::run_gate`, `script_check.py`, `judge_check.py`, `generator.py::generate_gate`). `evaluation/` holds the deepeval-based workflow eval harness.
- `providers/` — LLM provider adapters (`anthropic.py`, `openai.py`, `gemini.py`) behind `providers/base.py:LLMProvider`.
- `runtime/` — host-agent runtime abstraction (`runtime/providers/claude_code.py`, `hermes.py`, `inline.py`, `native.py`) used to dispatch a step's prompt to whichever CLI agent is configured as `settings.runtime`.
- `cli/` — the `botcircuits` console-script entry point (`cli/app.py`, `cli/commands*.py`) — init, workflow build/run, mcp management, manager start/stop, skills install.
- `gateway/` — FastAPI app (`gateway/app.py`) exposing workflow/agent access over `channels/`: Slack, WhatsApp, generic webhook, and cron-triggered runs.
- `manager/` — FastAPI backend (`manager/app.py`, `workflows.py`, `authoring.py`, `supervisor.py`) for the Argus Web Manager; paired with the Next.js frontend in `manager_web/` (React Flow-based visual editor + execution trace viewer, `npm run dev` on port 3700).
- `usage/` — token/cost accounting across providers and runtimes.

## Key project state

- `.botcircuits/settings.json` — project runtime config (`runtime: claude-code|hermes`, MCP servers); `settings.example.json` is the template. `BOTCIRCUITS_WORKFLOWS_DIR` overrides the workflows directory.
- `examples/*/TASK.md` — end-to-end use cases (research assistant, shipment tracking, CI/PR gates, incident triage, etc.) used for the README benchmark table.
- `evals/` — `deepeval`-based eval suite (optional dependency group `evals`), separate from the standard `pytest` `tests/` collection (`testpaths = ["tests"]` in `pyproject.toml`).

## Dev commands

```bash
uv sync                                  # install deps (Python >=3.11)
uv run pytest                            # unit tests (tests/)
uv run botcircuits <subcommand>          # CLI: init, workflow build/run, mcp, manager, skills install
cd manager_web && npm install && npm run dev   # Manager web UI (port 3700)
```

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **botcircuits-argus** (5413 symbols, 9814 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/botcircuits-argus/context` | Codebase overview, check index freshness |
| `gitnexus://repo/botcircuits-argus/clusters` | All functional areas |
| `gitnexus://repo/botcircuits-argus/processes` | All execution flows |
| `gitnexus://repo/botcircuits-argus/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->