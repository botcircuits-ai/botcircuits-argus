# Context (`agent/context.py`)

What surrounding conversation state a tool call gets to see.

```
 convo.messages (full history — can be huge)
      │
      │  extract + truncate (≤ 2000 chars each)
      ▼
 ┌───────────────────────────────┐
 │ tool_context                  │
 │   last_user_message      ─────┼── the human's utterance (tool
 │   last_assistant_message ─────┼── results skipped)   most recent
 │   session_id                  │   assistant prose
 │   run_segment (engine seam)   │
 └───────────────┬───────────────┘
                 ▼
        handler(args, context)   ── only if the handler's signature
                                    accepts a second argument
```

The loop hands every tool call a small, bounded snapshot instead of the whole
transcript:

```python
tool_context = {
    "last_assistant_message": last_assistant_text(convo.messages),  # ≤ 2000 chars
    "last_user_message":      last_user_text(convo.messages),       # ≤ 2000 chars
    "session_id":             convo.session_id,
    "run_segment":            <engine callback, see segments.md>,
}
```

- `last_user_text` skips tool-result blocks (which also live on user-role
  messages) — it returns the human's actual utterance.
- `last_assistant_text` returns the most recent assistant prose.
- Both truncate at `LAST_MESSAGE_CHARS` (2000).

Keeping the snapshot small and stable is deliberate: the main consumer is the
workflow tool's variable normalization, which only needs the freshest prose,
and a bounded snapshot keeps tool payloads cheap and cache-friendly.

Handlers opt in by accepting a second `context` argument — the registry
inspects each handler's signature and only passes context to handlers that
take it (see [tools.md](tools.md)).
