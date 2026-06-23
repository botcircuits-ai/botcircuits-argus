# 1. What It Is

[← Index](00-index.md)

---

BotCircuits is an **AI agent** — a program that takes a request in plain
language, decides what to do, uses tools to do it, and replies. It works the
same way regardless of which LLM provider sits underneath.

## What it does

- **Runs an agent loop** — calls the model, lets it use tools, feeds the results
  back, and repeats until the task is done.
- **Works with any major LLM** — Anthropic, OpenAI, and Gemini behind one
  interface. Swap the provider without changing your code.
- **Gets things done through tools** — built-in tools, external tools via MCP,
  and Skills (reusable capabilities).
- **Runs workflows** — for repeatable multi-step processes, a deterministic
  engine drives the steps so the outcome is predictable, not left to chance.
- **Remembers** — persistent memory carries facts across sessions.
- **Streams** — a UI can watch text, tool calls, and results as they happen.
- **Runs anywhere** — as a CLI, an HTTP service, or connected to chat channels.

## What it is not

- It does not lock you into one LLM vendor.
- It does not load tool *code* from configuration — config holds settings, code
  lives in code.
- The core stays small; extras (HTTP, persistence) are optional add-ons.

## The two ways the agent acts

| Mode | When | Who's in control |
|---|---|---|
| **Conversational** | Open-ended requests | The model decides each step |
| **Workflow** | Repeatable, structured processes | The engine decides each step (deterministic) |

The rest of this guide walks through each piece. Next: [the agent loop](02-agent-loop.md).
