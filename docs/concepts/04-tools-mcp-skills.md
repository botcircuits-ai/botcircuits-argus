# 4. Tools, MCP & Skills

[← Index](00-index.md)

---

These are the ways the agent actually *does* things beyond talking. The model
asks to use one; the agent runs it and feeds the result back.

## Tools

A **tool** is a named capability with typed inputs — read a file, run a shell
command, call an API, ask the user a question. Built-in tools cover common needs;
you add your own in code (their settings live in config, their implementation
lives in code).

## MCP (Model Context Protocol)

**MCP** is a standard way to plug in *external* tools. The agent supports two
modes:

| Mode | Where the tool runs |
|---|---|
| **Hosted MCP** | On the provider's side (when the provider supports it) |
| **Local MCP** | In-process — the agent connects to an MCP server itself |

Either way, MCP tools appear to the model just like built-in ones.

## Skills

A **Skill** is a reusable, self-contained capability — instructions plus
optional helper files — that the agent can load when relevant. Two kinds:

- **Filesystem skills** — a folder with a `SKILL.md` (Claude-Code style),
  auto-discovered and exposed as a tool.
- **Hosted skills** — mapped to a provider's code-execution surface.

## How they relate

```
            the model asks to use a capability
                          │
        ┌─────────────┬───┴────┬──────────────┐
        ▼             ▼        ▼              ▼
   built-in       MCP tools  Skills      Workflow tools
   tools        (local/hosted)         (run a workflow — §5)
```

All of them are just "tools" to the model — a uniform surface. Workflows are a
special tool that hands control to the deterministic engine.

Next: [Workflows](05-workflows.md).
