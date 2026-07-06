"""`edit_file` — exact string replacement inside an existing file.

Same gating as `write_file`: y/N per call (skipped with `auto=True`,
forced on non-tty). The prompt shows a unified diff so the user can see
exactly what will change.

The contract mirrors Claude Code's Edit tool:
  - `old_string` must appear in the file (and, unless `replace_all`,
    must be unique). The tool errors otherwise rather than guessing.
  - `new_string` may be empty (delete) or a different multi-line block.
"""

from __future__ import annotations

import difflib
import os

from botcircuits.agent.tools.registry import LocalTool, ToolRegistry
from botcircuits.agent.tools.builtins import _confirm

DIFF_PREVIEW_LINES = 60


def edit_file_tool(*, auto: bool = False) -> LocalTool:
    effective_auto = _confirm.effective_auto(auto)

    async def _handler(args: dict, context: dict | None = None) -> dict:
        path = args.get("path")
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        replace_all = bool(args.get("replace_all", False))
        preapproved = bool((context or {}).get("permission_preapproved"))

        if not isinstance(path, str) or not path:
            return {"error": "`path` must be a non-empty string"}
        if not isinstance(old_string, str):
            return {"error": "`old_string` must be a string"}
        if not isinstance(new_string, str):
            return {"error": "`new_string` must be a string"}
        if old_string == new_string:
            return {"error": "`old_string` and `new_string` must differ"}
        if not os.path.isfile(path):
            return {"error": f"File not found: {path}"}

        try:
            with open(path, "r", encoding="utf-8") as f:
                original = f.read()
        except OSError as e:
            return {"error": f"Failed to read {path}: {type(e).__name__}: {e}"}

        count = original.count(old_string)
        if count == 0:
            return {
                "error": (
                    f"`old_string` not found in {path}. Read the file first "
                    "and copy the exact text (including whitespace) you want "
                    "to replace."
                ),
            }
        if count > 1 and not replace_all:
            return {
                "error": (
                    f"`old_string` matches {count} times in {path}. Either "
                    "include more surrounding context to make it unique, "
                    "or pass replace_all=true."
                ),
            }

        updated = (original.replace(old_string, new_string) if replace_all
                   else original.replace(old_string, new_string, 1))

        diff = _make_diff(path, original, updated)
        lines = [
            f"path:    {path}",
            f"matches: {count}{' (replace_all)' if replace_all else ''}",
            "diff:",
            *[f"  {ln}" for ln in diff.splitlines()],
        ]
        if preapproved:
            pass
        elif effective_auto:
            _confirm.warn("edit_file editing:", lines)
        else:
            allowed = await _confirm.confirm("edit_file proposes:", lines,
                                             prompt="apply? [y/N]: ")
            if not allowed:
                return {
                    "denied": True,
                    "path": path,
                    "message": (
                        "User denied the edit. Do not retry the same "
                        "replacement; ask the user what to change."
                    ),
                }

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(updated)
        except OSError as e:
            return {"error": f"Failed to write {path}: {type(e).__name__}: {e}"}

        return {
            "path": path,
            "replacements": count if replace_all else 1,
        }

    gate = (
        "Auto mode: edits go through without prompting; a warning shows the "
        "diff. "
        if effective_auto else
        "Each edit requires human y/N confirmation; the prompt shows a "
        "unified diff. The user may deny — if they do, denied=true is "
        "returned. "
    )

    return LocalTool(
        name="edit_file",
        description=(
            "Replace an exact string inside an existing UTF-8 text file. "
            "old_string must appear in the file; unless replace_all is "
            "true it must appear exactly once. " + gate +
            "Use read_file first to copy the exact text you intend to "
            "replace, including whitespace."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file to edit."},
                "old_string": {"type": "string",
                               "description": "Exact text to replace."},
                "new_string": {"type": "string",
                               "description": "Replacement text (may be empty to delete)."},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence instead of requiring a unique match.",
                    "default": False,
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
        handler=_handler,
    )


def register(reg: ToolRegistry, **config) -> None:
    allowed = {"auto"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(
            f"edit_file config has unknown keys: {sorted(unknown)}. "
            f"Allowed: {sorted(allowed)}"
        )
    reg.register(edit_file_tool(**config))


def _make_diff(path: str, before: str, after: str) -> str:
    diff_iter = difflib.unified_diff(
        before.splitlines(keepends=False),
        after.splitlines(keepends=False),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        lineterm="",
        n=2,
    )
    lines = list(diff_iter)
    if len(lines) > DIFF_PREVIEW_LINES:
        head = "\n".join(lines[:DIFF_PREVIEW_LINES])
        return f"{head}\n…[{len(lines) - DIFF_PREVIEW_LINES} more diff lines]"
    return "\n".join(lines)
