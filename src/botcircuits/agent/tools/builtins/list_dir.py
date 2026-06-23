"""`list_dir` — list directory entries with type, size, and modified time.

Read-only, no gate. Skips dotfiles by default (override with
`show_hidden=true`). Output is capped at `max_entries` to keep results
bounded for very large directories.
"""

from __future__ import annotations

import os

from botcircuits.agent.tools.registry import LocalTool, ToolRegistry

DEFAULT_MAX_ENTRIES = 500


def list_dir_tool(*, max_entries: int = DEFAULT_MAX_ENTRIES) -> LocalTool:
    async def _handler(args: dict) -> dict:
        path = args.get("path", ".")
        show_hidden = bool(args.get("show_hidden", False))
        if not isinstance(path, str) or not path:
            return {"error": "`path` must be a non-empty string"}
        if not os.path.isdir(path):
            return {"error": f"Not a directory: {path}"}

        try:
            names = sorted(os.listdir(path))
        except OSError as e:
            return {"error": f"Failed to list {path}: {type(e).__name__}: {e}"}

        entries = []
        truncated = False
        for name in names:
            if not show_hidden and name.startswith("."):
                continue
            if len(entries) >= max_entries:
                truncated = True
                break
            full = os.path.join(path, name)
            try:
                st = os.stat(full, follow_symlinks=False)
            except OSError:
                entries.append({"name": name, "type": "unknown"})
                continue
            if os.path.isdir(full):
                kind = "dir"
            elif os.path.islink(full):
                kind = "symlink"
            elif os.path.isfile(full):
                kind = "file"
            else:
                kind = "other"
            entries.append({
                "name": name,
                "type": kind,
                "size": st.st_size if kind == "file" else None,
            })

        return {
            "path": path,
            "count": len(entries),
            "truncated": truncated,
            "entries": entries,
        }

    return LocalTool(
        name="list_dir",
        description=(
            "List entries in a directory. Each entry has name, type "
            "(file/dir/symlink/other), and size (for files). Hidden "
            "entries (starting with '.') are omitted unless show_hidden=true. "
            f"Capped at {max_entries} entries."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "Directory to list. Default: current working directory."},
                "show_hidden": {"type": "boolean", "default": False,
                                "description": "Include dotfiles."},
            },
        },
        handler=_handler,
    )


def register(reg: ToolRegistry, **config) -> None:
    allowed = {"max_entries"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(
            f"list_dir config has unknown keys: {sorted(unknown)}. "
            f"Allowed: {sorted(allowed)}"
        )
    reg.register(list_dir_tool(**config))
