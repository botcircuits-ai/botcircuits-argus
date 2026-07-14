"""Sessions — the in-memory conversation store.

Each `Conversation` carries the message history, an optional system
prompt, and an asyncio lock that serializes turns within one session.
The store is keyed by `session_id`. Subclass `ConversationStore` to add
persistence.

Persistent memory (MEMORY.md / USER.md under ~/.botcircuits/memories/)
is loaded once when a session is created and frozen into `system` — that
way the prompt cache hit rate stays high and mid-session edits via the
`memory` tool don't leak back into the active conversation. Users get
the new memory snapshot on the next session.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field

from botcircuits.types import Message
from botcircuits.agent.memory import load_snapshot, render_for_system_prompt


@dataclass
class Conversation:
    session_id: str
    system: str = "You are a helpful assistant."
    messages: list[Message] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ConversationStore:
    """Simple dict-backed store. Subclass to add persistence."""

    def __init__(self) -> None:
        self._sessions: dict[str, Conversation] = {}

    def get_or_create(self, session_id: str | None,
                      system: str | None = None) -> Conversation:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        sid = session_id or str(uuid.uuid4())
        base_system = system or "You are a helpful assistant."
        # Inject persistent memory exactly once, at session creation —
        # never per-turn — so the prompt stays cache-friendly.
        memory_block = render_for_system_prompt(load_snapshot())
        c = Conversation(session_id=sid, system=base_system + memory_block)
        self._sessions[sid] = c
        return c

    def reset(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def list_sessions(self) -> list[str]:
        return list(self._sessions.keys())
