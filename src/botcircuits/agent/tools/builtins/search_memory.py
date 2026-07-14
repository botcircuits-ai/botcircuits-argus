"""`search_memory` — episodic recall from past sessions.

A session log isn't memory until the right slice can be recovered. This
tool runs a keyword text search across the JSON-L sessions persisted by
`DurableConversationStore` (see ``botcircuits.agent.sessions``) and
returns the best-matching messages. The current session is excluded —
its lines are already in context; recall is for the *other* sessions.

Distinct from the ``memory`` tool: that one mutates the small curated
MEMORY.md / USER.md notes injected into every system prompt; this one
searches raw conversation history on demand.
"""

from __future__ import annotations

from botcircuits.agent.sessions import search_sessions
from botcircuits.agent.tools.registry import LocalTool, ToolRegistry


def search_memory_tool() -> LocalTool:
    def _handler(args: dict, context: dict | None = None) -> str:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return "`query` (non-empty string) is required"
        exclude = (context or {}).get("session_id")
        hits = search_sessions(query, exclude=exclude)
        if not hits:
            return "no matching memory found"
        return "\n".join(
            f"[{h['session']}] {h['role']}: {h['content']}" for h in hits
        )

    return LocalTool(
        name="search_memory",
        description=(
            "Search past conversation sessions for relevant facts by "
            "keyword. Use when the user refers to something from an "
            "earlier conversation that isn't in the current context "
            "(a name, a decision, an id, an earlier result). Returns "
            "the best-matching messages from other sessions."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Keywords to look for (a few specific terms beat a full sentence).",
                },
            },
            "required": ["query"],
        },
        handler=_handler,
    )


def register(reg: ToolRegistry, **_config) -> None:
    reg.register(search_memory_tool())
