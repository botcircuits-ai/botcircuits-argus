"""`shell_status` — poll a background shell process.

Returns the most recent stdout/stderr lines, whether the process is
still alive, its exit code if it isn't, and how long it's been running.

Read-only, no gate. Pure observation tool — the model uses it to
decide whether the server it started has finished booting, whether the
test runner is still going, etc.

If no `bg_id` is passed the tool lists every registered background
process — useful when the model has lost track.
"""

from __future__ import annotations

import time

from botcircuits.agent.tools.registry import LocalTool, ToolRegistry
from botcircuits.agent.tools.builtins import _bg


def shell_status_tool() -> LocalTool:
    async def _handler(args: dict) -> dict:
        bg_id = args.get("bg_id")
        lines = args.get("lines", _bg.TAIL_DEFAULT)
        if not isinstance(lines, int) or lines < 0:
            return {"error": "`lines` must be a non-negative integer"}
        if lines > _bg.MAX_LINES_PER_STREAM:
            lines = _bg.MAX_LINES_PER_STREAM

        if not bg_id:
            return {
                "bg_processes": [
                    _summarize(_bg.get(i), lines=0)
                    for i in _bg.all_ids()
                ],
            }

        if not isinstance(bg_id, str):
            return {"error": "`bg_id` must be a string"}
        entry = _bg.get(bg_id)
        if entry is None:
            return {
                "error": (
                    f"No background process with bg_id={bg_id!r}. It may "
                    "have already been stopped. Call shell_status with no "
                    "bg_id to list active processes."
                ),
            }
        return _summarize(entry, lines=lines)

    return LocalTool(
        name="shell_status",
        description=(
            "Poll a background shell process started with "
            "shell_exec(background=true). Returns whether it's still "
            "alive, exit_code if it exited, uptime in seconds, and the "
            "last `lines` lines from stdout and stderr (default 100). "
            "Pass no bg_id to list every active background process."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "bg_id": {
                    "type": "string",
                    "description": "Returned by shell_exec(background=true).",
                },
                "lines": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        f"Lines of recent output to include from each of "
                        f"stdout/stderr. Default {_bg.TAIL_DEFAULT}, max "
                        f"{_bg.MAX_LINES_PER_STREAM}."
                    ),
                },
            },
        },
        handler=_handler,
    )


def register(reg: ToolRegistry, **config) -> None:
    if config:
        raise ValueError(f"`shell_status` takes no config; got: {sorted(config)}")
    reg.register(shell_status_tool())


def _summarize(entry, *, lines: int) -> dict:
    if entry is None:
        return {"error": "process not found"}
    proc = entry.proc
    exit_code = proc.returncode
    summary = {
        "bg_id": entry.bg_id,
        "pid": proc.pid,
        "argv": entry.argv,
        "alive": exit_code is None,
        "exit_code": exit_code,
        "uptime_s": round(time.time() - entry.started_at, 2),
    }
    if lines > 0:
        summary["stdout_tail"] = _bg.tail(entry.stdout, lines)
        summary["stderr_tail"] = _bg.tail(entry.stderr, lines)
    return summary
