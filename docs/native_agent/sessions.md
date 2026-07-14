# Sessions (`agent/sessions.py`)

The conversation store, plus durable state and episodic recall.

- `Conversation` — one session: `session_id`, system prompt, message
  history, and an asyncio lock that serializes turns within the session.
- `ConversationStore` — in-memory, dict keyed by `session_id`;
  `get_or_create`, `reset`, `list_sessions`. Sessions die with the process.
- `DurableConversationStore` — the same store, persisted.

## Durable state

Conversation is not state: nothing survives a kill unless it's written.
The durable store saves each session as JSON-L (one message per line, plus a
meta header carrying the frozen system prompt) under `.botcircuits/sessions/`
(override with `$BOTCIRCUITS_SESSIONS_DIR`).

- The agent loop calls `store.persist(session_id)` after **every turn** —
  terminal reply, `human_feedback` pause, step cap, or exception alike
  (a no-op on the in-memory store).
- Writes are atomic (temp file + rename): a kill mid-write never shreds the
  previous good file. Reads skip unparseable lines: a half-written final
  line must not make the session unrecoverable.
- `get_or_create` resumes from disk when the id isn't live in memory,
  restoring the system prompt frozen at creation. `reset` also deletes the
  file. Session ids are sanitized to their final path component so
  `../evil` can't write or unlink outside the sessions dir.

The CLI uses the durable store, so `--session <id>` / `/session <id>`
resume across runs; `/session` with no argument lists saved sessions.

## Episodic recall

A log isn't memory until you can recover the right slice.
`search_sessions(query, exclude=...)` does keyword text search across all
stored sessions (no embeddings), ranked by how many query terms a message
contains; `exclude` drops the current session so recall surfaces facts that
aren't already in context. The `search_memory` builtin tool exposes this to
the model (see [tools.md](tools.md)).

Persistent *curated* memory (MEMORY.md / USER.md, see [memory.md](memory.md))
is a different concept: it's loaded once at session creation and frozen into
`system` — never re-read per turn — so the prompt stays cache-friendly, and
mid-session memory edits apply from the next session onward.
