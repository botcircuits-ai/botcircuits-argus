"""`todo_write` — model-maintained live TODO list.

The model passes the full updated list each call (replace semantics, not
append). Each item has `content` and `status` (`pending` | `in_progress`
| `completed`). The tool renders the list to stderr so the user sees
progress in real time, and returns the stored list to the model so it
can confirm its own state.

Storage is per-process and in-memory — sessions can re-read it via the
return value but it isn't persisted across runs.
"""

from __future__ import annotations

import os
import sys

from botcircuits.agent.tools.registry import LocalTool, ToolRegistry

STATUSES = {"pending", "in_progress", "completed"}
_STORE: list[dict] = []  # module-global so all callers see the same list


def todo_write_tool() -> LocalTool:
    async def _handler(args: dict) -> dict:
        items = args.get("todos")
        if not isinstance(items, list):
            return {"error": "`todos` must be a list"}
        cleaned: list[dict] = []
        for i, raw in enumerate(items):
            if not isinstance(raw, dict):
                return {"error": f"todos[{i}] must be an object"}
            content = raw.get("content")
            status = raw.get("status", "pending")
            if not isinstance(content, str) or not content.strip():
                return {"error": f"todos[{i}].content must be a non-empty string"}
            if status not in STATUSES:
                return {"error": (
                    f"todos[{i}].status must be one of "
                    f"{sorted(STATUSES)}; got {status!r}"
                )}
            cleaned.append({"content": content.strip(), "status": status})

        _STORE.clear()
        _STORE.extend(cleaned)
        _render(cleaned)
        return {"count": len(cleaned), "todos": list(cleaned)}

    return LocalTool(
        name="todo_write",
        description=(
            "Replace the live TODO list with a new set of items. Each item "
            "is {content, status} where status is 'pending', 'in_progress', "
            "or 'completed'. Call this whenever the plan changes or a "
            "step finishes — keep exactly one item 'in_progress' at a "
            "time. The list is rendered to the user so they see progress."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": sorted(STATUSES),
                            },
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["todos"],
        },
        handler=_handler,
    )


def register(reg: ToolRegistry, **config) -> None:
    if config:
        raise ValueError(f"`todo_write` takes no config; got: {sorted(config)}")
    reg.register(todo_write_tool())


def _color(code: str, s: str) -> str:
    if not (hasattr(sys.stderr, "isatty") and sys.stderr.isatty()):
        return s
    if os.getenv("NO_COLOR"):
        return s
    return f"\033[{code}m{s}\033[0m"


_GLYPHS = {
    "completed":  ("✓", "32"),   # green check
    "in_progress": ("●", "33"),  # yellow dot
    "pending":    ("○", "37"),   # dim circle
}


def _render(items: list[dict]) -> None:
    sys.stderr.write(_color("36", "  ▸ todo_write updated:") + "\n")
    if not items:
        sys.stderr.write(_color("37", "      (empty)") + "\n")
        sys.stderr.flush()
        return
    for it in items:
        glyph, code = _GLYPHS.get(it["status"], ("?", "37"))
        line = f"      {_color(code, glyph)} {it['content']}"
        sys.stderr.write(line + "\n")
    sys.stderr.flush()
