"""`grep_search` — regex search across files (ripgrep-style, pure-Python).

Read-only. Walks the tree under `path`, opens text files, applies the
regex per line, and returns `{file, line_number, line}` matches.
Binary-looking files (those that don't decode as UTF-8) are skipped.

Defaults skip common build/cache directories. Output is capped to keep
the result bounded for very large codebases.
"""

from __future__ import annotations

import os
import re

from botcircuits.agent.tools.registry import LocalTool, ToolRegistry
from botcircuits.agent.tools.builtins.glob_search import DEFAULT_IGNORES

DEFAULT_MAX_RESULTS = 200
DEFAULT_MAX_FILE_BYTES = 1_000_000


def grep_search_tool(
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> LocalTool:
    async def _handler(args: dict) -> dict:
        pattern = args.get("pattern")
        path = args.get("path", ".")
        case_insensitive = bool(args.get("case_insensitive", False))
        include = args.get("include")  # optional filename glob like "*.py"
        respect_ignores = args.get("respect_ignores", True)

        if not isinstance(pattern, str) or not pattern:
            return {"error": "`pattern` must be a non-empty string"}
        if not isinstance(path, str) or not path:
            return {"error": "`path` must be a non-empty string"}
        if not os.path.isdir(path):
            return {"error": f"Not a directory: {path}"}

        flags = re.IGNORECASE if case_insensitive else 0
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return {"error": f"Invalid regex: {e}"}

        include_re = None
        if include is not None:
            if not isinstance(include, str) or not include:
                return {"error": "`include` must be a non-empty string if provided"}
            include_re = re.compile(_glob_to_regex(include))

        matches: list[dict] = []
        files_searched = 0
        truncated = False

        for dirpath, dirnames, filenames in os.walk(path):
            if respect_ignores:
                dirnames[:] = [d for d in dirnames if d not in DEFAULT_IGNORES]
            for fn in filenames:
                if include_re is not None and not include_re.match(fn):
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(full) > max_file_bytes:
                        continue
                except OSError:
                    continue
                files_searched += 1
                try:
                    with open(full, "r", encoding="utf-8") as f:
                        for i, line in enumerate(f, start=1):
                            if regex.search(line):
                                matches.append({
                                    "file": full,
                                    "line_number": i,
                                    "line": line.rstrip("\n"),
                                })
                                if len(matches) >= max_results:
                                    truncated = True
                                    break
                except (OSError, UnicodeDecodeError):
                    continue
                if truncated:
                    break
            if truncated:
                break

        return {
            "pattern": pattern,
            "path": path,
            "files_searched": files_searched,
            "match_count": len(matches),
            "truncated": truncated,
            "matches": matches,
        }

    return LocalTool(
        name="grep_search",
        description=(
            "Search file contents by Python regex. Walks `path` recursively, "
            "skipping binary/non-UTF-8 files and common build/cache dirs "
            "(.git, node_modules, __pycache__, .venv, …). Filter by "
            "filename with `include` (glob, e.g. '*.py'). Case-sensitive "
            f"by default. Capped at {max_results} matches and "
            f"{max_file_bytes} bytes per file."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string",
                            "description": "Python regex to search for."},
                "path": {"type": "string", "default": ".",
                         "description": "Root directory. Default: cwd."},
                "include": {"type": "string",
                            "description": "Filename glob, e.g. '*.py'."},
                "case_insensitive": {"type": "boolean", "default": False},
                "respect_ignores": {"type": "boolean", "default": True},
            },
            "required": ["pattern"],
        },
        handler=_handler,
    )


def register(reg: ToolRegistry, **config) -> None:
    allowed = {"max_results", "max_file_bytes"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(
            f"grep_search config has unknown keys: {sorted(unknown)}. "
            f"Allowed: {sorted(allowed)}"
        )
    reg.register(grep_search_tool(**config))


def _glob_to_regex(pat: str) -> str:
    """Translate a simple filename glob (*, ?, [..]) to a fullmatch regex.
    Anchored so the pattern matches the entire filename."""
    import fnmatch
    return fnmatch.translate(pat)
