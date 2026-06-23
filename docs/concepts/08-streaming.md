# 7. Streaming

[← Index](00-index.md)

---

Streaming lets a UI watch a turn happen **live** instead of waiting for the final
answer.

## What you can watch

As the agent works, it emits events:

- **text deltas** — the reply being written, word by word.
- **tool calls** — the moment the agent decides to use a tool.
- **tool results** — what each tool returned.
- **turn markers** — start, end, done.

```
ask the model
   │  text delta… text delta… text delta…
   │  ▸ tool call
   │  ◂ tool result
   │  text delta… text delta…
   ▼  done
```

## Why it matters

- A chat UI shows the answer as it forms, not after a long pause.
- During a workflow, the steps stay visible on screen as the engine runs them.
- Long tool calls don't look like a frozen app.

## How to use it

Call the streaming version of a turn (`chat_stream()`) and consume the events.
The non-streaming version (`chat()`) runs the exact same logic and just returns
the final answer — streaming is a presentation choice, not a different engine.

Next: [Configuration & CLI](09-configuration-and-cli.md).
