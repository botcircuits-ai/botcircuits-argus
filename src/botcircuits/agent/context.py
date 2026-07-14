"""Context extraction — what surrounding conversation state tools get to see.

The agent loop hands every tool call a small, bounded context snapshot
(the most recent user utterance and assistant reply) instead of the whole
transcript. Keeping the snapshot small and stable is deliberate: tools
that need conversational context (e.g. the workflow tool's variable
normalization) only need the freshest prose, and a bounded snapshot keeps
tool-call payloads cheap and cache-friendly.
"""

from __future__ import annotations

from botcircuits.types import Message

#: Truncation cap on the last-message text handed to tools via context.
#: Variable normalization (the workflow tool's main consumer) only needs
#: the most recent prose-y reply, not the model's entire monologue.
LAST_MESSAGE_CHARS = 2000


def last_assistant_text(messages: list[Message]) -> str:
    """Pull the most recent assistant `text` block out of `messages` and
    truncate it. Returns "" when no assistant text exists yet (e.g., the
    workflow tool is called on the very first turn before the model has
    said anything beyond a tool call).
    """
    for m in reversed(messages):
        if m.role != "assistant":
            continue
        for b in m.blocks:
            if b.get("type") == "text" and b.get("text"):
                return _clip(b["text"])
    return ""


def last_user_text(messages: list[Message]) -> str:
    """Pull the most recent user `text` block out of `messages` and
    truncate it. Tool-result blocks (which also live on user-role messages)
    are skipped — we want the human's actual utterance, not tool output.
    Returns "" when no user text exists yet.
    """
    for m in reversed(messages):
        if m.role != "user":
            continue
        for b in m.blocks:
            if b.get("type") == "text" and b.get("text"):
                return _clip(b["text"])
    return ""


def _clip(text: str) -> str:
    if len(text) > LAST_MESSAGE_CHARS:
        return text[:LAST_MESSAGE_CHARS] + "…"
    return text
