"""`glob_search` — find files by glob pattern (e.g. `**/*.py`).

Read-only, no gate. Uses stdlib `glob` with `recursive=True` so `**`
matches across directories. Results are sorted by modification time
(newest first) and capped at `max_results`.

Common ignore directories (.git, node_modules, __pycache__, .venv,
dist, build) are skipped by default; pass `respect_ignores=false` to
include them.
"""

from __future__ import annotations

import glob as _glob
import os

from botcircuits.agent.tools.registry import LocalTool, ToolRegistry

DEFAULT_MAX_RESULTS = 200
DEFAULT_IGNORES = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".cache", ".mypy_cache", ".pytest_cache",
}


def glob_search_tool(*, max_results: int = DEFAULT_MAX_RESULTS) -> LocalTool:
    async def _handler(args: dict) -> dict:
        pattern = args.get("pattern")
        path = args.get("path", ".")
        respect_ignores = args.get("respect_ignores", True)
        if not isinstance(pattern, str) or not pattern:
            return {"error": "`pattern` must be a non-empty string"}
        if not isinstance(path, str) or not path:
            return {"error": "`path` must be a non-empty string"}
        if not os.path.isdir(path):
            return {"error": f"Not a directory: {path}"}

        search_pattern = os.path.join(path, pattern)
        try:
            matches = _glob.glob(search_pattern, recursive=True)
        except OSError as e:
            return {"error": f"Glob failed: {type(e).__name__}: {e}"}

        if respect_ignores:
            matches = [m for m in matches if not _is_ignored(m, path)]

        matches = [m for m in matches if os.path.isfile(m)]

        def _mtime(p: str) -> float:
            try:
                return os.stat(p).st_mtime
            except OSError:
                return 0.0

        matches.sort(key=_mtime, reverse=True)
        truncated = len(matches) > max_results
        matches = matches[:max_results]

        return {
            "pattern": pattern,
            "path": path,
            "count": len(matches),
            "truncated": truncated,
            "files": matches,
        }

    return LocalTool(
        name="glob_search",
        description=(
            "Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). "
            "`**` recurses into subdirectories. Results are sorted by mtime "
            f"(newest first) and capped at {max_results}. Common ignore "
            "directories like .git, node_modules, __pycache__, .venv are "
            "skipped unless respect_ignores=false."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern, e.g. '**/*.py' or 'tests/test_*.py'.",
                },
                "path": {"type": "string", "default": ".",
                         "description": "Root directory to search. Default: cwd."},
                "respect_ignores": {
                    "type": "boolean", "default": True,
                    "description": "Skip common build/cache dirs (.git, node_modules, etc).",
                },
            },
            "required": ["pattern"],
        },
        handler=_handler,
    )


def register(reg: ToolRegistry, **config) -> None:
    allowed = {"max_results"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(
            f"glob_search config has unknown keys: {sorted(unknown)}. "
            f"Allowed: {sorted(allowed)}"
        )
    reg.register(glob_search_tool(**config))


def _is_ignored(filepath: str, root: str) -> bool:
    """True if any path segment between `root` and `filepath` is in the
    ignore set."""
    try:
        rel = os.path.relpath(filepath, root)
    except ValueError:
        return False
    parts = rel.split(os.sep)
    return any(part in DEFAULT_IGNORES for part in parts)
