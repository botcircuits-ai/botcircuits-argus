"""Built-in local tools.

Each module exposes:
  - one or more `<name>_tool()` factory functions returning a `LocalTool`
  - a `register(reg, **config)` helper that registers the tool on a registry

`default_registry()` wires every module listed in
`agent/tools/__init__._BUILTINS` and threads per-tool JSON config through
each module's `register()`. Tools that mutate state (`shell_exec`,
`write_file`, `edit_file`, `plan_and_confirm`) ship behind a per-call
y/N confirmation gate that is bypassed with `--auto`.
"""

from __future__ import annotations

from botcircuits.agent.tools.builtins import (
    arithmetic,
    build_workflow,
    edit_file,
    glob_search,
    grep_search,
    human_feedback,
    list_dir,
    memory,
    plan_and_confirm,
    read_file,
    search_memory,
    shell,
    shell_status,
    shell_stop,
    time,
    todo_write,
    web_extract,
    web_search,
    write_file,
)
from botcircuits.agent.tools.builtins.shell import shell_exec_tool

__all__ = [
    "arithmetic",
    "build_workflow",
    "edit_file",
    "glob_search",
    "grep_search",
    "human_feedback",
    "list_dir",
    "memory",
    "plan_and_confirm",
    "read_file",
    "search_memory",
    "shell",
    "shell_exec_tool",
    "shell_status",
    "shell_stop",
    "time",
    "todo_write",
    "web_extract",
    "web_search",
    "write_file",
]
