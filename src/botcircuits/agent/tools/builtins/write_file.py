"""`write_file` — create or overwrite a UTF-8 text file on disk.

Gated by per-call y/N confirmation by default (skipped with `auto=True`,
forced on non-tty). The prompt shows the path, byte size, and a content
preview so the user can decide before committing.

Parent directories are created automatically.
"""

from __future__ import annotations

import os

from botcircuits.agent.tools.registry import LocalTool, ToolRegistry
from botcircuits.agent.tools.builtins import _confirm

DEFAULT_MAX_BYTES = 5_000_000
PREVIEW_LINES = 20


def write_file_tool(
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    auto: bool = False,
) -> LocalTool:
    effective_auto = _confirm.effective_auto(auto)

    async def _handler(args: dict, context: dict | None = None) -> dict:
        path = args.get("path")
        content = args.get("content")
        if not isinstance(path, str) or not path:
            return {"error": "`path` must be a non-empty string"}
        if not isinstance(content, str):
            return {"error": "`content` must be a string"}

        size = len(content.encode("utf-8"))
        if size > max_bytes:
            return {
                "error": (
                    f"Content is {size} bytes, exceeds max_bytes={max_bytes}. "
                    "Write the file in smaller chunks or raise max_bytes."
                ),
            }

        preapproved = bool((context or {}).get("permission_preapproved"))
        existed = os.path.exists(path)
        preview = _make_preview(content)
        lines = [
            f"path:    {path}",
            f"size:    {size} bytes",
            f"action:  {'overwrite' if existed else 'create'}",
            "preview:",
            *[f"  {ln}" for ln in preview.splitlines()],
        ]
        if preapproved:
            pass
        elif effective_auto:
            _confirm.warn("write_file writing:", lines)
        else:
            allowed = await _confirm.confirm("write_file proposes:", lines,
                                             prompt="write? [y/N]: ")
            if not allowed:
                return {
                    "denied": True,
                    "path": path,
                    "message": (
                        "User denied the write. Do not retry the same path "
                        "with the same content; ask the user what to change."
                    ),
                }

        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError as e:
                return {"error": f"Failed to create parent dir: {type(e).__name__}: {e}"}

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            return {"error": f"Failed to write {path}: {type(e).__name__}: {e}"}

        return {
            "path": path,
            "bytes_written": size,
            "created": not existed,
        }

    gate = (
        "Auto mode: writes go through without prompting; the user sees a "
        "warning before each one. "
        if effective_auto else
        "Each write requires human y/N confirmation. The user may deny — "
        "if they do, denied=true is returned and you should not retry the "
        "same path with the same content. "
    )

    return LocalTool(
        name="write_file",
        description=(
            "Create or overwrite a UTF-8 text file. Parent directories are "
            "created automatically. " + gate +
            f"Max {max_bytes} bytes per call."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Target file path."},
                "content": {"type": "string",
                            "description": "Full file contents to write."},
            },
            "required": ["path", "content"],
        },
        handler=_handler,
    )


def register(reg: ToolRegistry, **config) -> None:
    allowed = {"max_bytes", "auto"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(
            f"write_file config has unknown keys: {sorted(unknown)}. "
            f"Allowed: {sorted(allowed)}"
        )
    reg.register(write_file_tool(**config))


def _make_preview(content: str) -> str:
    lines = content.splitlines()
    if len(lines) <= PREVIEW_LINES:
        return content
    head = "\n".join(lines[:PREVIEW_LINES])
    return f"{head}\n…[{len(lines) - PREVIEW_LINES} more lines]"
