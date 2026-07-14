# Native Agent

The in-process BotCircuits agent, structured around the **Model + Harness + UI**
framing: the model is a thin, swappable seam; the harness — the loop, the
context, the tools, the memory, the workflow engine — is where the engineering
lives.

```
src/botcircuits/
  providers/      the MODEL seam (anthropic / openai / gemini / openrouter)
  agent/          the HARNESS (everything below)
  cli/            the UI (interactive REPL / TUI)
```

## How it connects

```
                                    user
                                     │ ▲
                             message │ │ reply / StreamEvents
                                     ▼ │
 ┌───────────────────────────────────┴─┬─────────────────────────────────┐
 │   UI — cli/ (REPL · TUI)         gateway/ (Slack · WhatsApp · cron)   │
 └─────────────────────────────────────┬─────────────────────────────────┘
                                       │ Agent.chat / chat_stream
 ════════════════════ HARNESS — agent/ ╪════════════════════════════════════
                                       ▼
  sessions.py ──(history + lock)──► ┌────────────────────────────┐
  memory.py ───(frozen into ──────► │       loop.py — Agent      │
                system prompt)      │                            │
  react.py ◄──(text-mode ─────────► │  1 append user message;    │
               parse/render)        │    auto-resume paused wf   │
  events.py ◄─(pause detection, ──► │  2 call the model ─────────┼──► providers/
               segment events       │  3 interpret the reply     │   (MODEL seam)
               → StreamEvents)      │  4 run tool calls          │
  context.py ─(bounded snapshot ──► │  5 feed results back, loop │
               for every tool call) └─────────────┬──────────────┘
                                                  │ step 4
                                                  ▼
  ┌───────────────────────────────────────────────┬───────────────────────┐
  │                        tools/ — ToolRegistry                          │
  │        permissions.py gate (deny → ask → allow) on every dispatch     │
  │                                                                       │
  │   builtins           mcp.py           skill/           workflow/      │
  │   shell · files ·    MCP servers      SKILL.md dirs    one tool per   │
  │   web · memory · …   as LocalTools    as LocalTools    workflow       │
  └──────────────────────────────────────────────┬────────────────────────┘
                                                 │ a workflow tool fires
                                                 ▼
  ┌──────────────────────────────────────────────┬────────────────────────┐
  │  workflow/engine — run_workflow_engine                                │
  │  the ENGINE owns the loop now: walks branch-delimited segments,       │
  │  evaluates branches deterministically, pauses on questions            │
  │                        │ one call per segment                         │
  │                        ▼                                              │
  │  segments.py — SegmentRunner._run_segment                             │
  │  cache-stable prompt + the agent's real tools + record_slots capture; │
  │  per-agent model override via providers.make_provider ────────────────┼──► providers/
  └───────────────────────────────────────────────────────────────────────┘
```

Two control modes, one seam:

- **Conversational** — the model drives: `loop.py` calls the provider, the
  model picks tools, results are fed back until it stops (or pauses on
  `human_feedback`).
- **Workflow** — the engine drives: once a workflow tool fires, the state
  machine advances itself and invokes the LLM only per segment via
  `segments.py`. Control returns to the loop on *done* or a *question*
  pause; the user's next message auto-resumes the paused workflow (step 1).

Both modes call the model only through the `providers/` seam, and every tool
call — builtin, MCP, skill, or workflow — goes through the same
permission-gated registry.

## Harness modules (`src/botcircuits/agent/`)

| Module | Responsibility | Doc |
|---|---|---|
| `loop.py` | The `Agent` drive loop: chat / chat_stream, native + ReAct modes | [loop.md](loop.md) |
| `context.py` | Bounded context snapshot handed to tools | [context.md](context.md) |
| `events.py` | Loop internals → UI `StreamEvent`s; pause detection | [events.md](events.md) |
| `segments.py` | Engine-driven workflow segment execution | [segments.md](segments.md) |
| `sessions.py` | Conversation store: durable JSON-L sessions + episodic search | [sessions.md](sessions.md) |
| `memory.py` | Persistent MEMORY.md / USER.md notes across sessions | [memory.md](memory.md) |
| `tools/` | `ToolRegistry`, `LocalTool`, the builtin tool set | [tools.md](tools.md) |
| `permissions.py` | allow / ask / deny rules gating tool calls | [permissions.md](permissions.md) |
| `skill/` | Hosted skills (`SkillSpec`) + filesystem skills (`SKILL.md`) | [skills.md](skills.md) |
| `mcp.py` | MCP servers, hosted or local, exposed as tools | [mcp.md](mcp.md) |
| `react.py` | ReAct text-mode fallback for providers without tool APIs | [react.md](react.md) |
| `workflow/` | The deterministic workflow engine + workflow tools | [workflow.md](workflow.md) |

## The model seam (`src/botcircuits/providers/`)

Every provider implements one `LLMProvider` base (`complete`, `stream`,
tool-spec translation, usage accounting). The loop never talks to a vendor
SDK directly, so swapping models never touches harness code. `make_provider`
builds a client from a short name (`anthropic` / `openai` / `gemini` /
`openrouter`) — also used per-segment when a workflow pins a step to a named
agent with its own model.

## Entry points

- `botcircuits` (CLI) — interactive agent, `botcircuits/cli`.
- `botcircuits.gateway` — HTTP/SSE + channels (Slack, WhatsApp, webhook, cron).
- `botcircuits.runtime` — the runtime seam for workflow runs hosted by an
  *external* CLI agent (claude-code, hermes, …). The native path doesn't use a
  runtime provider: the in-process `Agent` hands the engine its callbacks
  directly.
