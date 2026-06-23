"""`memory` — bounded, curated persistent memory across sessions.

Exposes one tool, ``memory``, with three actions: ``add``, ``replace``,
``remove``. The two backing files (MEMORY.md / USER.md) are read once per
session and injected into the system prompt; this tool is how the agent
writes to them. There is intentionally no ``read`` action — the content
is already in the system prompt.

See ``botcircuits.agent.memory`` for the storage model and limits.
"""

from __future__ import annotations

from botcircuits.agent.memory import (
    MemoryError,
    add_entry,
    remove_entry,
    replace_entry,
)
from botcircuits.agent.tools.registry import LocalTool, ToolRegistry

_ACTIONS = ("add", "replace", "remove")
_TARGETS = ("memory", "user")


def memory_tool() -> LocalTool:
    async def _handler(args: dict) -> dict:
        action = args.get("action")
        target = args.get("target")
        if action not in _ACTIONS:
            return {"error": f"`action` must be one of {list(_ACTIONS)}; got {action!r}"}
        if target not in _TARGETS:
            return {"error": f"`target` must be one of {list(_TARGETS)}; got {target!r}"}

        try:
            if action == "add":
                text = args.get("text")
                if not isinstance(text, str):
                    return {"error": "`text` (string) is required for action=add"}
                return add_entry(target, text)

            if action == "replace":
                old_text = args.get("old_text")
                new_text = args.get("new_text")
                if not isinstance(old_text, str):
                    return {"error": "`old_text` (string) is required for action=replace"}
                if not isinstance(new_text, str):
                    return {"error": "`new_text` (string) is required for action=replace"}
                return replace_entry(target, old_text, new_text)

            # remove
            old_text = args.get("old_text")
            if not isinstance(old_text, str):
                return {"error": "`old_text` (string) is required for action=remove"}
            return remove_entry(target, old_text)

        except MemoryError as e:
            return {"error": str(e)}

    return LocalTool(
        name="memory",
        description=(
            "Persistent memory shared across sessions. Two targets: "
            "'memory' (agent's notes: project conventions, tool quirks, "
            "lessons learned) and 'user' (user profile: preferences, "
            "communication style, role). Content is auto-loaded into the "
            "system prompt at session start — do NOT call this just to "
            "read; only to mutate.\n\n"
            "Actions:\n"
            "  add      — append a new entry. Args: target, text.\n"
            "  replace  — substring-match an existing entry and swap text. "
            "Args: target, old_text, new_text. Pick a unique substring.\n"
            "  remove   — substring-match and drop an entry. Args: target, "
            "old_text.\n\n"
            "When to use: save durable facts the user would want recalled "
            "next session — names, preferences, environment details, "
            "project conventions discovered during this session. Skip "
            "ephemeral details (current task progress, transient errors).\n"
            "Caps: memory=2200 chars, user=1375 chars. If you hit the cap, "
            "consolidate or remove obsolete entries first."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_ACTIONS),
                    "description": "Which mutation to perform.",
                },
                "target": {
                    "type": "string",
                    "enum": list(_TARGETS),
                    "description": (
                        "'memory' for agent-side notes (environment, "
                        "conventions, lessons). 'user' for user profile "
                        "(preferences, role, communication style)."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "New entry text. Required for action=add.",
                },
                "old_text": {
                    "type": "string",
                    "description": (
                        "Unique substring identifying the entry to replace or "
                        "remove. Required for action=replace and action=remove."
                    ),
                },
                "new_text": {
                    "type": "string",
                    "description": (
                        "Replacement text. Required for action=replace."
                    ),
                },
            },
            "required": ["action", "target"],
        },
        handler=_handler,
    )


def register(reg: ToolRegistry, **config) -> None:
    if config:
        raise ValueError(f"`memory` takes no config; got: {sorted(config)}")
    reg.register(memory_tool())
