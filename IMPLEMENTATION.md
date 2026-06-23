# Implementation Guide

This document explains the architecture, data flow, and *why* behind the design choices in `botcircuits-agent`. Read [README.md](README.md) first for usage; this guide is for engineers extending or maintaining the system.

It was split out of a single large file into topic-scoped pages under [docs/implementations/](docs/implementations/). Read them in order for a full pass, or jump to the one you need. The original section numbers (`§1`–`§18`) are preserved inside each page, so cross-references like "see §8.6" still resolve to the headings shown below.

---

## Contents

| Page | Sections | What's inside |
|---|---|---|
| [1. Overview, Package Layout & Architecture](docs/implementations/01-overview.md) | §1–§3 | Goals & non-goals, the `src/botcircuits/` tree, and the high-level layer diagram (Agent → Provider → MCP/Skills/Workflows/Memory) including the workflow **guardrail** box. |
| [2. Data Model & The Agent Loop](docs/implementations/02-data-model-and-agent-loop.md) | §4–§5 | The normalized `Message`/`ToolCall`/`LLMResponse` block model, and the multi-round loop: concurrent tools, `max_steps`, per-conversation lock, engine-driven workflow handoff (`run_segment`) + human-feedback pause + reminders. |
| [3. Provider Abstraction](docs/implementations/03-providers.md) | §6 | The `LLMProvider` ABC and the Anthropic / OpenAI / Gemini adapters. |
| [4. MCP: Hosted vs Local](docs/implementations/04-mcp.md) | §7 | `MCPServer` config, the `LocalMCPManager`, auto-promotion, and `mcp.json` wiring. |
| [5. Local Tools & Workflows](docs/implementations/05-local-tools-and-workflows.md) | §8 | The `builtins/` package + per-tool config, the code-gen tool surface, background shells, and the full BotCircuits **workflow** subsystem (engine, normalization, `build_workflow`, `human_feedback`). |
| [6. Persistent Memory](docs/implementations/06-persistent-memory.md) | §8a | MEMORY.md / USER.md storage, the `memory` tool, capacity + threat scrub, the frozen-snapshot read path. |
| [7. Filesystem & Hosted Skills](docs/implementations/07-skills.md) | §8b–§9 | Claude-Code-style `SKILL.md` discovery + dynamic substitutions, and hosted `SkillSpec`. |
| [8. Streaming Pipeline](docs/implementations/08-streaming.md) | §10 | Provider-level and agent-level streaming events. |
| [9. Configuration](docs/implementations/09-configuration.md) | §11 | Layered settings, sentinel-None CLI flags, `CLIConfig`, `.env`, why JSON. |
| [10. CLI](docs/implementations/10-cli.md) | §12 | The chat REPL and the `mcp` / `workflow` subcommands. |
| [11. FastAPI Gateway & Message Gateway](docs/implementations/11-gateway.md) | §13–§13a | JSON + SSE HTTP surface and the multi-channel inbound→agent→outbound message gateway. |
| [12. Conversation Store](docs/implementations/12-conversation-store.md) | §14 | In-memory session store + per-session locks. |
| [13. Capability Matrix, Extension Points & Trade-offs](docs/implementations/13-reference.md) | §15–§18 | Per-provider capability table, how-to recipes for extending the system, named design trade-offs, and suggested next improvements. |
| [14. Runtime Providers](docs/developer-guide/14-runtime-providers.md) | — | Using an **existing host agent** (claude-code, …) to run workflows: the `AgentRuntimeProvider` seam, the native (behavior-preserving) and CLI providers, runtime detection, and the workflow-authoring / workflow-running skills. |

---

## Conventions

- **Source links** in the topic pages are written relative to that page (`../../src/botcircuits/...`), so they click through correctly from `docs/implementations/`.
- **`§N` references** inside the pages point to the heading that still carries that number on the page listed above.
- Each page links back here via the header at its top.
