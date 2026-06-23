# 2. The Agent Loop

[← Index](00-index.md)

---

> **Note.** This page describes the **native** agent loop — now one of several
> [runtime providers](11-runtime-providers.md), not the only way to run a
> workflow. When the host is an existing agent (claude-code, …), that agent
> supplies the intelligence and the native loop is bypassed. The native loop
> remains the default and the CI/offline fallback.

The agent loop is the heart of the native runtime: one turn of conversation,
driven to completion.

## How a turn runs

```
user message
     │
     ▼
  ask the model  ──►  model replies with text and/or tool calls
     │                          │
     │              any tool calls?  ── no ──►  return the reply  ✓
     │                          │ yes
     │                          ▼
     └──────────  run the tools, feed results back
```

The loop repeats — model, tools, model, tools — until the model answers with no
more tool calls. A safety cap (`max_steps`) prevents runaway loops.

## Key ideas

- **Messages are typed blocks.** A turn can hold text *and* tool calls *and* tool
  results together, so one message shape carries every kind of turn.
- **Tools run together.** When the model asks for several tools at once, they run
  concurrently; results come back in the original order.
- **Sessions are isolated.** Each conversation has its own history and runs
  independently; two requests on the same session are serialized so order stays
  correct.

## Two flavors

- **`chat()`** — runs the whole turn and returns the final answer.
- **`chat_stream()`** — same logic, but emits events (text, tool calls, results)
  as they happen, so a UI can show progress live (see [Streaming](08-streaming.md)).

## When a workflow is involved

If the request matches a workflow, control hands off to the **workflow engine**,
which drives the steps deterministically and hands back a result. The
conversational loop resumes afterward. See [Workflows](05-workflows.md).

Next: [Providers](03-providers.md).
