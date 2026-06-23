# BotCircuits Agent — High-Level Guide

A concept-level tour of the BotCircuits agent: what each part is and how the
pieces fit together. Each page is a short overview — for full detail see the
[Implementation Guide](../implementations/01-overview.md).

---

## Index

1. [What It Is](01-what-it-is.md) — the agent in one page: goals, the big picture.
2. [The Agent Loop](02-agent-loop.md) — how a turn runs: model → tools → repeat.
3. [Providers](03-providers.md) — talking to any LLM (Anthropic, OpenAI, Gemini).
4. [Tools, MCP & Skills](04-tools-mcp-skills.md) — how the agent gets things done.
5. [Workflows](05-workflows.md) — deterministic, engine-driven multi-step automation.
6. [Authoring a Workflow](06-workflow-authoring-guide.md) — how to write one (end-user guide).
7. [Memory](07-memory.md) — what the agent remembers across sessions.
8. [Streaming](08-streaming.md) — watching a turn happen live.
9. [Configuration & CLI](09-configuration-and-cli.md) — how it's set up and run.
10. [Gateway](10-gateway.md) — exposing the agent over HTTP and chat channels.
11. [Runtime Providers](11-runtime-providers.md) — using an existing host agent (claude-code, …) to run workflows.

---

## The shape of it

```
        you / a channel (CLI, HTTP, WhatsApp, Slack)
                          │
                          ▼
                   ┌─────────────┐
                   │  Agent Loop │   calls the model, runs tools, repeats
                   └─────────────┘
                    │     │     │
        ┌───────────┘     │     └───────────┐
        ▼                 ▼                 ▼
   Provider           Tools / MCP        Workflows
 (any LLM)          / Skills          (deterministic engine)
        │
        └─ Memory, Streaming, Config wrap around all of it
```

Start with [What It Is](01-what-it-is.md).
