"""`shell_stop` — terminate a background shell process.

Gated by per-call y/N confirmation by default (bypassed with
`auto=True` or `--auto`). Stopping a running process is a real
side-effecting action — same gate semantics as `shell_exec` itself.

SIGTERM, wait `grace_seconds` for graceful exit, then SIGKILL if the
process is still alive. Cleans up the tail readers and removes the
entry from the registry.

Idempotent: stopping an already-dead process returns success with the
already-known exit code.
"""

from __future__ import annotations

from botcircuits.agent.tools.registry import LocalTool, ToolRegistry
from botcircuits.agent.tools.builtins import _bg, _confirm

DEFAULT_GRACE_SECONDS = 3.0


def shell_stop_tool(*, auto: bool = False) -> LocalTool:
    effective_auto = _confirm.effective_auto(auto)

    async def _handler(args: dict) -> dict:
        bg_id = args.get("bg_id")
        grace_seconds = args.get("grace_seconds", DEFAULT_GRACE_SECONDS)
        if not isinstance(bg_id, str) or not bg_id:
            return {"error": "`bg_id` must be a non-empty string"}
        if not isinstance(grace_seconds, (int, float)) or grace_seconds < 0:
            return {"error": "`grace_seconds` must be a non-negative number"}

        entry = _bg.get(bg_id)
        if entry is None:
            return {
                "error": (
                    f"No background process with bg_id={bg_id!r}. It may "
                    "have already been stopped or never existed."
                ),
            }

        pretty = " ".join(repr(a) if " " in a else a for a in entry.argv)
        lines = [
            f"bg_id: {bg_id}",
            f"pid:   {entry.proc.pid}",
            f"cmd:   {pretty}",
            f"grace: {grace_seconds}s (SIGTERM, then SIGKILL)",
        ]
        if effective_auto:
            _confirm.warn("shell_stop terminating:", lines)
        else:
            allowed = await _confirm.confirm("shell_stop proposes:", lines,
                                             prompt="stop? [y/N]: ")
            if not allowed:
                return {
                    "denied": True,
                    "bg_id": bg_id,
                    "message": (
                        "User denied stopping the process. It is still "
                        "running. Do not retry immediately; ask the user "
                        "what to do or leave it alone."
                    ),
                }

        return await _bg.stop(entry, grace_seconds=grace_seconds)

    gate = (
        "Auto mode: termination proceeds without prompting; a warning "
        "shows the bg_id and argv. "
        if effective_auto else
        "Each call requires human y/N confirmation. On denial, the "
        "process keeps running. "
    )
    return LocalTool(
        name="shell_stop",
        description=(
            "Terminate a background shell process by bg_id. SIGTERM "
            "first, wait grace_seconds, then SIGKILL. " + gate +
            "Returns the exit code and runtime. Idempotent for "
            "already-exited processes."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "bg_id": {
                    "type": "string",
                    "description": "Returned by shell_exec(background=true).",
                },
                "grace_seconds": {
                    "type": "number",
                    "minimum": 0,
                    "default": DEFAULT_GRACE_SECONDS,
                    "description": "Seconds to wait between SIGTERM and SIGKILL.",
                },
            },
            "required": ["bg_id"],
        },
        handler=_handler,
    )


def register(reg: ToolRegistry, **config) -> None:
    allowed = {"auto"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(
            f"shell_stop config has unknown keys: {sorted(unknown)}. "
            f"Allowed: {sorted(allowed)}"
        )
    reg.register(shell_stop_tool(**config))
