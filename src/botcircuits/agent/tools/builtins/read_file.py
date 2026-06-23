"""`read_file` — read a UTF-8 text file from disk.

Read-only, no confirmation gate. Supports optional `offset`/`limit` for
large files. Truncates at `max_bytes` to keep the result bounded.
"""

from __future__ import annotations

import os

from botcircuits.agent.tools.registry import LocalTool, ToolRegistry

DEFAULT_MAX_BYTES = 200_000


def read_file_tool(*, max_bytes: int = DEFAULT_MAX_BYTES) -> LocalTool:
    async def _handler(args: dict) -> dict:
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return {"error": "`path` must be a non-empty string"}
        offset = args.get("offset", 0)
        limit = args.get("limit")
        if not isinstance(offset, int) or offset < 0:
            return {"error": "`offset` must be a non-negative integer"}
        if limit is not None and (not isinstance(limit, int) or limit <= 0):
            return {"error": "`limit` must be a positive integer or omitted"}

        if not os.path.exists(path):
            return {"error": f"File not found: {path}"}
        if not os.path.isfile(path):
            return {"error": f"Not a regular file: {path}"}

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            return {"error": f"Failed to read {path}: {type(e).__name__}: {e}"}

        total = len(lines)
        end = total if limit is None else min(total, offset + limit)
        selected = lines[offset:end]
        text = "".join(selected)
        truncated = False
        if len(text.encode("utf-8")) > max_bytes:
            truncated = True
            text = text.encode("utf-8")[:max_bytes].decode("utf-8", errors="replace")
            text += f"\n…[truncated at {max_bytes} bytes]"

        return {
            "path": path,
            "start_line": offset + 1,
            "end_line": end,
            "total_lines": total,
            "truncated": truncated,
            "content": text,
        }

    return LocalTool(
        name="read_file",
        description=(
            "Read a UTF-8 text file from disk and return its contents. "
            "Lines are 0-indexed via `offset`; pass `limit` to bound the "
            f"number of lines returned. Output is truncated at {max_bytes} "
            "bytes. Prefer this over `shell_exec cat` — it's faster and "
            "returns structured metadata."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to the file."},
                "offset": {
                    "type": "integer", "minimum": 0,
                    "description": "First line to include (0-indexed). Default 0.",
                },
                "limit": {
                    "type": "integer", "minimum": 1,
                    "description": "Max number of lines to return. Omit to read to EOF.",
                },
            },
            "required": ["path"],
        },
        handler=_handler,
    )


def register(reg: ToolRegistry, **config) -> None:
    allowed = {"max_bytes"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(
            f"read_file config has unknown keys: {sorted(unknown)}. "
            f"Allowed: {sorted(allowed)}"
        )
    reg.register(read_file_tool(**config))
