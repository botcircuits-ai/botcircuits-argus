"""Shell command execution tool.

Runs a system command via `asyncio.create_subprocess_exec` (no shell, no
metacharacter expansion). The model passes a list of argv tokens.

This tool IS in `default_registry()`. Every command requires human
confirmation by default — when the model wants to run something, the
agent loop pauses and prints the proposed argv, then reads y/N from
stdin. Press Enter or `n` to deny; type `y` (or `yes`) to allow.

Set `auto=true` (CLI: `--auto`, JSON: `tools.shell_exec.auto`) to skip
the prompt. A warning is still printed before each command so the user
sees what ran. Auto mode is mandatory for non-interactive contexts (the
FastAPI gateway, piped stdin) where there's no human to answer.

Foreground vs background:
  - Default is foreground. The handler waits for the process and
    returns {argv, exit_code, stdout, stderr}.
  - Pass `background: true` for non-terminating commands (servers,
    watchers, `tail -f`, dev builds). The handler returns immediately
    with {bg_id, pid, argv, started_at}. The model then polls with
    `shell_status` and stops it with `shell_stop` when done. The
    process registry (agent/tools/builtins/_bg.py) cleans up survivors
    on Agent.aclose() and at process exit.

Guardrails (all overridable):
  - timeout_seconds:  kill after N seconds (foreground only, default 30)
  - max_output_bytes: truncate stdout/stderr (default 10 KB)
  - auto:             skip confirmation, just warn. Default False.
                      Forced True when stdin isn't a TTY.

There is intentionally NO command allow-list and NO cwd pinning — every
shell command is permitted and runs in the process's current working
directory. The confirmation prompt is the only gate; the user is
responsible for deciding what is safe to run and where.
"""

from __future__ import annotations

import asyncio

from botcircuits.agent.tools.registry import LocalTool, ToolRegistry
from botcircuits.agent.tools.builtins import _bg, _confirm


DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_OUTPUT_BYTES = 10_000


def shell_exec_tool(
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    auto: bool = False,
) -> LocalTool:
    """Build a `shell_exec` tool. The closure captures the policy so the
    model can't override it through tool arguments.

    `auto=True` skips the y/N confirmation prompt (still warns). When
    stdin isn't a TTY, `auto` is treated as True regardless of the
    constructor argument — there's no human to ask.
    """
    effective_auto = _confirm.effective_auto(auto)

    async def _handler(args: dict) -> dict:
        argv = args.get("argv")
        background = bool(args.get("background", False))
        if not isinstance(argv, list) or not argv:
            return {"error": "argv must be a non-empty list of strings"}
        if not all(isinstance(a, str) for a in argv):
            return {"error": "argv must contain only strings"}

        pretty = " ".join(repr(a) if " " in a else a for a in argv)
        lines = [f"cmd:  {pretty}"]
        if background:
            lines.append("mode: background (use shell_status / shell_stop)")
        if effective_auto:
            _confirm.warn("shell_exec running:", lines)
        else:
            allowed = await _confirm.confirm("shell_exec proposes:", lines)
            if not allowed:
                return {
                    "denied": True,
                    "argv": argv,
                    "message": "User denied the command. Do not retry the "
                               "same argv; explain why or pick a different "
                               "approach.",
                }

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return {"error": f"Executable not found: {argv[0]!r}"}
        except OSError as e:
            return {"error": f"Failed to start process: {type(e).__name__}: {e}"}

        if background:
            entry = await _bg.register(argv, proc)
            return {
                "bg_id": entry.bg_id,
                "pid": proc.pid,
                "argv": argv,
                "started_at": entry.started_at,
                "message": (
                    "Process started in background. Poll output with "
                    f"shell_status(bg_id='{entry.bg_id}') and terminate "
                    f"with shell_stop(bg_id='{entry.bg_id}') when done."
                ),
            }

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "error": (
                    f"Command timed out after {timeout_seconds}s. If this "
                    "is a long-running command (server, watcher, etc.), "
                    "retry with background=true."
                ),
                "argv": argv,
            }

        return {
            "argv": argv,
            "exit_code": proc.returncode,
            "stdout": _truncate(stdout, max_output_bytes),
            "stderr": _truncate(stderr, max_output_bytes),
        }

    if effective_auto:
        gate_clause = (
            "Auto mode: commands run without prompting; the user sees a "
            "warning before each one. "
        )
    else:
        gate_clause = (
            "Each command requires human y/N confirmation before it runs. "
            "The user may deny — if they do, the result will have "
            "denied=true and you should not retry the same argv. "
        )
    description = (
        "Run a system command. Commands run in whatever working directory "
        "the agent process was started in; if you need a specific location, "
        "pass it through the command itself (e.g. ['ls', '/some/path']). "
        + gate_clause +
        "No shell expansion: pipes, redirects, globs, and metacharacters "
        "do NOT work — pass argv as a list (e.g. ['ls', '-la', 'subdir']). "
        f"Foreground timeout: {timeout_seconds}s, output truncated at "
        f"{max_output_bytes} bytes. Set background=true for non-terminating "
        "commands (dev servers, watchers, tail -f); the call returns "
        "immediately with a bg_id, and you poll with shell_status / stop "
        "with shell_stop."
    )

    return LocalTool(
        name="shell_exec",
        description=description,
        input_schema={
            "type": "object",
            "properties": {
                "argv": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "Command and arguments as a list of strings.",
                },
                "background": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Start the process in the background and return a "
                        "bg_id immediately. Use this for non-terminating "
                        "commands like servers, watchers, or `tail -f`. "
                        "Poll output with `shell_status`, terminate with "
                        "`shell_stop`. Foreground timeout does NOT apply."
                    ),
                },
            },
            "required": ["argv"],
        },
        handler=_handler,
    )


def register(reg: ToolRegistry, **config) -> None:
    """Register `shell_exec` on `reg`. Any keyword args override the
    factory defaults — `register(reg, auto=True)`. Used by
    `default_registry()` to thread per-tool config from the JSON file.

    Recognized keys: `timeout_seconds`, `max_output_bytes`, `auto`.
    Unknown keys are rejected so typos in the JSON surface immediately.
    """
    allowed_keys = {"timeout_seconds", "max_output_bytes", "auto"}
    unknown = set(config) - allowed_keys
    if unknown:
        raise ValueError(
            f"shell_exec config has unknown keys: {sorted(unknown)}. "
            f"Allowed: {sorted(allowed_keys)}"
        )
    reg.register(shell_exec_tool(**config))


def _truncate(b: bytes, limit: int) -> str:
    """Decode subprocess output to a string, truncating with a marker if
    it exceeds `limit` bytes. Replaces undecodable bytes rather than
    raising — terminal output isn't always valid UTF-8."""
    if len(b) <= limit:
        return b.decode("utf-8", errors="replace")
    head = b[:limit].decode("utf-8", errors="replace")
    return f"{head}\n…[truncated {len(b) - limit} bytes]"
