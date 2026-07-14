# Memory (`agent/memory.py`)

Persistent, bounded, curated notes that survive across sessions (modeled on
Hermes Agent's memory feature).

```
 ~/.botcircuits/memories/
   MEMORY.md ──┐                       session N            session N+1
   USER.md  ──┤  read ONCE at         ┌──────────┐         ┌──────────┐
              └─ session creation ──► │ frozen in │         │ fresh    │
                                      │ system    │         │ snapshot │
                                      │ prompt    │         └────▲─────┘
                                      └────┬──────┘              │
                                           │  memory tool        │
                                           │  add / replace /    │
                                           └─ remove ──► files ──┘
                                              (edits land NEXT session —
                                               the live prompt stays
                                               cache-stable)
```

Two flat files under `~/.botcircuits/memories/`:

| File | Content | Cap |
|---|---|---|
| `MEMORY.md` | agent notes: environment, conventions, lessons learned | 2200 chars (~800 tokens) |
| `USER.md` | user profile: preferences, style, expectations | 1375 chars (~500 tokens) |

Entries are `§`-delimited and may span lines. Both files are read once at
session start and injected into the system prompt under `<agent_memory>` /
`<user_profile>` tags — there is no `read` action because the content is
already in context.

The `memory` builtin tool exposes three actions against targets `memory`
(→ MEMORY.md) and `user` (→ USER.md):

- `add` — append an entry
- `replace` — substring-match an entry, swap in new text
- `remove` — substring-match an entry, drop it

The caps are enforced on write; the model is expected to curate (replace /
remove) rather than accumulate.

This is the *curated* memory. Raw conversation history is a separate system:
durable JSON-L sessions plus the `search_memory` recall tool — see
[sessions.md](sessions.md).
