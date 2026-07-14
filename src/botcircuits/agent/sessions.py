"""Sessions — the conversation store, plus durable state and episodic recall.

Each `Conversation` carries the message history, an optional system
prompt, and an asyncio lock that serializes turns within one session.
The store is keyed by `session_id`.

Two store flavors:

- `ConversationStore` — in-memory only; sessions die with the process.
- `DurableConversationStore` — additionally persists each session as
  JSON-L (one message per line) under `.botcircuits/sessions/`, so a
  killed agent can resume by reloading it. Conversation is not state:
  the session boundary is the kill point — nothing survives unless it's
  written. Writes are atomic (temp file + rename) so a kill mid-write
  never shreds the previous good file.

A log isn't *memory* until you can recover the right slice:
`search_sessions` does keyword text search across all stored sessions
(no embeddings); the `search_memory` builtin tool lets the model pull
matching chunks from sessions that aren't in the current context.

Persistent curated memory (MEMORY.md / USER.md — see `agent/memory.py`)
is a different concept: it is loaded once when a session is created and
frozen into `system` — that way the prompt cache hit rate stays high and
mid-session edits via the `memory` tool don't leak back into the active
conversation. Users get the new memory snapshot on the next session.
"""

from __future__ import annotations

import asyncio
import json
import os
import string
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from botcircuits.types import Message
from botcircuits.agent.memory import load_snapshot, render_for_system_prompt

#: Where session JSON-L files live, unless overridden.
SESSIONS_DIR_ENV = "BOTCIRCUITS_SESSIONS_DIR"
DEFAULT_SESSIONS_DIR = ".botcircuits/sessions"

#: Max hits `search_sessions` returns.
SEARCH_LIMIT = 8

#: Per-hit content cap in search results — recall should surface the fact,
#: not replay a transcript into the context window.
_SEARCH_SNIPPET_CHARS = 400


def resolve_sessions_dir(base: str | Path | None = None) -> Path:
    """The sessions directory: explicit `base` > $BOTCIRCUITS_SESSIONS_DIR >
    `.botcircuits/sessions` under the current working directory."""
    if base is not None:
        return Path(base)
    env = os.getenv(SESSIONS_DIR_ENV)
    return Path(env) if env else Path(DEFAULT_SESSIONS_DIR)


# ---------------------------------------------------------------------------
# JSON-L persistence
# ---------------------------------------------------------------------------


def _session_path(session_id: str, base: str | Path | None = None) -> Path:
    # Session ids come from user input (--session / /session) — take only the
    # final path component so a name like "../secret" can't write (or, via
    # /reset, unlink) files outside the sessions dir.
    safe = Path(session_id).name or "session"
    return resolve_sessions_dir(base) / f"{safe}.jsonl"


def _write_jsonl_atomic(path: Path, rows: list[dict]) -> None:
    """Write rows to `path` atomically (temp file + rename in the same dir).

    A kill mid-write must not shred the previous good file — durable state
    is the whole point, and saves run after every turn."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            for r in rows:
                f.write(json.dumps(r, default=str) + "\n")
        os.replace(tmp, path)  # atomic on POSIX; a reader never sees a partial file
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_jsonl(path: Path) -> list[dict]:
    """Parse a JSON-L file, skipping any unparseable line.

    A killed agent can leave a half-written final line; one bad line must
    not make the whole session unrecoverable."""
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _serialize_block(block: dict) -> dict:
    """One message block as a JSON-safe dict.

    `thought_signature` (Gemini) is opaque provider bytes — it can't be
    faithfully round-tripped through JSON text, so it is dropped on save.
    A resumed session replays without it; providers treat None as absent.
    """
    out = dict(block)
    out.pop("thought_signature", None)
    return out


def save_session(
    session_id: str,
    system: str,
    messages: list[Message],
    base: str | Path | None = None,
) -> None:
    """Persist one session: a meta header row (the frozen system prompt),
    then one row per message."""
    rows: list[dict] = [{"type": "meta", "system": system}]
    for m in messages:
        rows.append({"role": m.role, "blocks": [_serialize_block(b) for b in m.blocks]})
    _write_jsonl_atomic(_session_path(session_id, base), rows)


def load_session(
    session_id: str,
    base: str | Path | None = None,
) -> tuple[str | None, list[Message]] | None:
    """Load a persisted session. Returns (system, messages), or None when
    nothing was saved under this id. `system` is None for legacy files
    without a meta row."""
    path = _session_path(session_id, base)
    if not path.is_file():
        return None
    system: str | None = None
    messages: list[Message] = []
    for row in _read_jsonl(path):
        if row.get("type") == "meta":
            if isinstance(row.get("system"), str):
                system = row["system"]
            continue
        role = row.get("role")
        blocks = row.get("blocks")
        if role in ("user", "assistant", "system") and isinstance(blocks, list):
            messages.append(Message(role=role, blocks=blocks))
    return system, messages


def delete_session(session_id: str, base: str | Path | None = None) -> None:
    """Wipe a session's persisted messages (used by /reset). Idempotent —
    a missing file is fine, since reset should work whether or not
    anything was saved."""
    _session_path(session_id, base).unlink(missing_ok=True)


def list_saved_sessions(base: str | Path | None = None) -> list[dict]:
    """Persisted sessions as {name, messages, mtime}, most recent first."""
    base_dir = resolve_sessions_dir(base)
    if not base_dir.is_dir():
        return []
    out: list[dict] = []
    for path in base_dir.glob("*.jsonl"):
        try:
            rows = sum(1 for line in path.read_text(encoding="utf-8").splitlines()
                       if line.strip())
        except OSError:
            continue
        # Don't count the meta header as a message.
        out.append({"name": path.stem, "messages": max(rows - 1, 0),
                    "mtime": path.stat().st_mtime})
    out.sort(key=lambda s: s["mtime"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# Episodic recall — keyword search across stored sessions
# ---------------------------------------------------------------------------


def _message_text(row: dict) -> str:
    """The searchable text of one persisted message row: its text blocks
    plus tool-result content (facts often land in tool output)."""
    parts: list[str] = []
    for b in row.get("blocks") or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and b.get("text"):
            parts.append(str(b["text"]))
        elif b.get("type") == "tool_result" and b.get("content"):
            parts.append(str(b["content"]))
    return "\n".join(parts)


def search_sessions(
    query: str,
    base: str | Path | None = None,
    limit: int = SEARCH_LIMIT,
    *,
    exclude: str | None = None,
) -> list[dict]:
    """Keyword text search across stored sessions. Returns the best-matching
    messages as {session, role, content}, ranked by how many query terms
    appear.

    `exclude` drops one session (the current one) so recall surfaces facts
    that *aren't* already in the live context. Query terms are stripped of
    surrounding punctuation so a natural phrasing like `order id?` still
    matches `id`."""
    terms = [t.strip(string.punctuation) for t in query.lower().split()]
    terms = [t for t in terms if t]
    if not terms:
        return []
    base_dir = resolve_sessions_dir(base)
    if not base_dir.is_dir():
        return []
    scored: list[tuple[int, dict]] = []
    for path in sorted(base_dir.glob("*.jsonl")):
        if exclude is not None and path.stem == exclude:
            continue
        for row in _read_jsonl(path):
            if row.get("type") == "meta":
                continue
            text = _message_text(row)
            lowered = text.lower()
            score = sum(term in lowered for term in terms)
            if score:
                snippet = (text[:_SEARCH_SNIPPET_CHARS] + "…"
                           if len(text) > _SEARCH_SNIPPET_CHARS else text)
                scored.append((score, {
                    "session": path.stem,
                    "role": row.get("role"),
                    "content": snippet,
                }))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [m for _, m in scored[:limit]]


# ---------------------------------------------------------------------------
# Stores
# ---------------------------------------------------------------------------


@dataclass
class Conversation:
    session_id: str
    system: str = "You are a helpful assistant."
    messages: list[Message] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ConversationStore:
    """Simple dict-backed store. Sessions die with the process; see
    `DurableConversationStore` for on-disk persistence."""

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

    def persist(self, session_id: str) -> None:
        """Write-through hook the agent loop calls after every turn.
        No-op here; `DurableConversationStore` overrides it."""

    def reset(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def list_sessions(self) -> list[str]:
        return list(self._sessions.keys())


class DurableConversationStore(ConversationStore):
    """A `ConversationStore` that survives the process.

    - `get_or_create` resumes a session from disk when it isn't live in
      memory (restoring the system prompt frozen at its creation, so the
      resumed history stays consistent with what the model already saw).
    - `persist` (called by the agent loop after every turn) rewrites the
      session file atomically.
    - `reset` also deletes the file — reset means gone, not "back after
      restart".
    """

    def __init__(self, base_dir: str | Path | None = None) -> None:
        super().__init__()
        self.base_dir = resolve_sessions_dir(base_dir)

    def get_or_create(self, session_id: str | None,
                      system: str | None = None) -> Conversation:
        if session_id and session_id not in self._sessions:
            saved = load_session(session_id, self.base_dir)
            if saved is not None:
                saved_system, messages = saved
                c = Conversation(
                    session_id=session_id,
                    system=saved_system if saved_system is not None
                    else (system or "You are a helpful assistant."),
                    messages=messages,
                )
                self._sessions[session_id] = c
                return c
        return super().get_or_create(session_id, system=system)

    def persist(self, session_id: str) -> None:
        convo = self._sessions.get(session_id)
        if convo is None:
            return
        save_session(session_id, convo.system, convo.messages, self.base_dir)

    def reset(self, session_id: str) -> None:
        super().reset(session_id)
        delete_session(session_id, self.base_dir)
