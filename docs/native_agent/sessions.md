# Sessions (`agent/sessions.py`)

The in-memory conversation store.

- `Conversation` — one session: `session_id`, system prompt, message
  history, and an asyncio lock that serializes turns within the session.
- `ConversationStore` — dict keyed by `session_id`; `get_or_create`,
  `reset`, `list_sessions`. Subclass to add persistence.

Persistent memory (see [memory.md](memory.md)) is loaded once at session
creation and frozen into the system prompt — never re-read per turn — so the
prompt stays cache-friendly and mid-session memory edits apply from the next
session onward.
