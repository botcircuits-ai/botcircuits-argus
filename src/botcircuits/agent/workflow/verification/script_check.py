"""Deterministic `type: "script"` check execution.

A script check is a checked-in Python file (generated at build time,
see `generator.py`) exposing no particular API — it's run as a plain
subprocess, not imported. This mirrors `agent/tools/builtins/shell.py`'s
`asyncio.create_subprocess_exec` pattern (no shell, no metacharacter
expansion) rather than `importlib`-loading and calling the script
in-process: a buggy or adversarial generated script must not be able to
corrupt the caller's process, hang its event loop, or poison
`sys.modules` for the next check in the same gate run.

Contract: the script is invoked as `<python> <script_path>` with the
run record JSON-encoded on stdin, and must print exactly one line of
`{"passed": bool, "detail": str}` to stdout. A non-zero exit code or
unparsable stdout is an execution ERROR, not a normal fail — the
executor (`executor.py`) always treats an error as blocking regardless
of the check's declared severity.
"""

from __future__ import annotations

import asyncio
import json
import sys

from .types import CheckResult

DEFAULT_TIMEOUT_SECONDS = 10
MAX_TIMEOUT_SECONDS = 60


async def run_one(check: dict, run: dict, *, base_dir) -> CheckResult:
    """Execute one `type: "script"` check against `run` and return its
    `CheckResult`. Never raises — subprocess/parse failures become an
    `error`-carrying result instead."""
    check_id = check.get("id") or "<unnamed>"
    severity = check.get("severity") or "blocking"
    script_rel = check.get("script")
    if not isinstance(script_rel, str) or not script_rel:
        return CheckResult(
            check_id=check_id, passed=False, severity=severity,
            error="check has no `script` path",
        )
    script_path = (base_dir / script_rel).resolve()
    if not script_path.is_file():
        return CheckResult(
            check_id=check_id, passed=False, severity=severity,
            error=f"script not found: {script_path}",
        )

    timeout = check.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = min(float(timeout), MAX_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        return CheckResult(
            check_id=check_id, passed=False, severity=severity,
            error=f"failed to start check script: {type(e).__name__}: {e}",
        )

    stdin_bytes = json.dumps(run, ensure_ascii=False).encode("utf-8")
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(stdin_bytes), timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return CheckResult(
            check_id=check_id, passed=False, severity=severity,
            error=f"check script timed out after {timeout}s",
        )

    if proc.returncode != 0:
        return CheckResult(
            check_id=check_id, passed=False, severity=severity,
            error=(
                f"check script exited {proc.returncode}: "
                f"{stderr.decode('utf-8', errors='replace').strip()[:2000]}"
            ),
        )

    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace").strip())
    except (ValueError, TypeError):
        return CheckResult(
            check_id=check_id, passed=False, severity=severity,
            error=f"check script produced non-JSON stdout: {stdout[:2000]!r}",
        )
    if not isinstance(payload, dict) or "passed" not in payload:
        return CheckResult(
            check_id=check_id, passed=False, severity=severity,
            error=f"check script stdout missing `passed`: {payload!r}",
        )
    return CheckResult(
        check_id=check_id,
        passed=bool(payload.get("passed")),
        severity=severity,
        detail=str(payload.get("detail") or ""),
    )
