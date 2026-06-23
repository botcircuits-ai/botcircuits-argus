"""In-process registry of background shell processes.

Lives next to the shell tools because it's intrinsically tied to their
lifetime in this process. Module-global on purpose — there's exactly
one registry per Python process, and `shell_exec`, `shell_status`, and
`shell_stop` all touch it. Same pattern as `todo_write._STORE`.

For each background command we keep:
  - the asyncio.subprocess.Process handle
  - two bounded `deque`s tailing stdout / stderr (each line)
  - argv, started_at, bg_id

Two background tasks per process pump output from the pipes into the
deques. Lines are kept; bytes-per-line are truncated at LINE_BYTES so a
runaway producer can't OOM the agent.

`terminate_all()` is wired into `Agent.aclose()` and an `atexit` hook
so background procs don't survive the agent process.
"""

from __future__ import annotations

import asyncio
import atexit
import os
import signal
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

# Per-stream ring-buffer caps. The model gets at most TAIL_DEFAULT lines
# back from shell_status; the buffer keeps a bit more so a poll right
# after a burst still sees recent context.
MAX_LINES_PER_STREAM = 500
LINE_BYTES = 4_000
TAIL_DEFAULT = 100


@dataclass
class BackgroundProc:
    bg_id: str
    argv: list[str]
    proc: asyncio.subprocess.Process
    started_at: float
    stdout: "deque[str]" = field(default_factory=lambda: deque(maxlen=MAX_LINES_PER_STREAM))
    stderr: "deque[str]" = field(default_factory=lambda: deque(maxlen=MAX_LINES_PER_STREAM))
    stdout_task: Optional[asyncio.Task] = None
    stderr_task: Optional[asyncio.Task] = None


_REGISTRY: dict[str, BackgroundProc] = {}
_ATEXIT_REGISTERED = False


def _ensure_atexit() -> None:
    """Register a cleanup hook the first time we spawn a bg process.
    We don't register at import time so test runs that never touch the
    bg path don't pay for an extra hook."""
    global _ATEXIT_REGISTERED
    if _ATEXIT_REGISTERED:
        return
    atexit.register(_terminate_all_sync)
    _ATEXIT_REGISTERED = True


async def register(argv: list[str], proc: asyncio.subprocess.Process) -> BackgroundProc:
    """Wrap a started subprocess as a BackgroundProc, hook up tail
    readers, and store it. Returns the registered entry."""
    _ensure_atexit()
    bg_id = uuid.uuid4().hex[:12]
    entry = BackgroundProc(
        bg_id=bg_id,
        argv=list(argv),
        proc=proc,
        started_at=time.time(),
    )
    if proc.stdout is not None:
        entry.stdout_task = asyncio.create_task(_pump(proc.stdout, entry.stdout))
    if proc.stderr is not None:
        entry.stderr_task = asyncio.create_task(_pump(proc.stderr, entry.stderr))
    _REGISTRY[bg_id] = entry
    return entry


def get(bg_id: str) -> Optional[BackgroundProc]:
    return _REGISTRY.get(bg_id)


def all_ids() -> list[str]:
    return list(_REGISTRY.keys())


def tail(buf: "deque[str]", n: int) -> list[str]:
    """Last `n` lines from a ring buffer, oldest first."""
    if n <= 0:
        return []
    if n >= len(buf):
        return list(buf)
    return list(buf)[-n:]


async def _pump(stream: asyncio.StreamReader, buf: "deque[str]") -> None:
    """Read lines from a subprocess pipe into a bounded deque. Each
    line is truncated at LINE_BYTES so one producer can't blow memory
    by writing forever without newlines.

    Exits cleanly when the stream closes (EOF). We swallow CancelledError
    so callers can cancel us without noise — the process is going away."""
    try:
        while True:
            chunk = await stream.readline()
            if not chunk:
                return  # EOF
            if len(chunk) > LINE_BYTES:
                chunk = chunk[:LINE_BYTES] + b"\n[line truncated]\n"
            try:
                buf.append(chunk.decode("utf-8", errors="replace").rstrip("\n"))
            except Exception:
                buf.append("[undecodable line]")
    except asyncio.CancelledError:
        return
    except Exception:
        return


async def stop(entry: BackgroundProc, grace_seconds: float = 3.0) -> dict:
    """Terminate one background process. SIGTERM, wait `grace_seconds`,
    then SIGKILL if still alive. Cancels the tail readers. Removes the
    entry from the registry.

    Returns a status dict the tool layer can return to the model.
    Idempotent — calling stop() on an already-dead process is fine.
    """
    proc = entry.proc
    runtime_s = round(time.time() - entry.started_at, 2)

    already_exited = proc.returncode is not None
    if not already_exited:
        try:
            proc.terminate()
        except ProcessLookupError:
            already_exited = True

    if not already_exited:
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    for task in (entry.stdout_task, entry.stderr_task):
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    _REGISTRY.pop(entry.bg_id, None)
    # Use `terminated_exit_code` rather than `exit_code` so the
    # registry's is_error heuristic doesn't flag a successful SIGTERM
    # (returncode = -15) as a tool error. A successful stop is not a
    # failure observation.
    return {
        "stopped": True,
        "bg_id": entry.bg_id,
        "terminated_exit_code": proc.returncode,
        "runtime_s": runtime_s,
    }


async def terminate_all(grace_seconds: float = 2.0) -> None:
    """Async cleanup hook. Called from `Agent.aclose()` so the loop is
    still running and we can await each stop cleanly."""
    entries = list(_REGISTRY.values())
    if not entries:
        return
    await asyncio.gather(
        *(stop(e, grace_seconds=grace_seconds) for e in entries),
        return_exceptions=True,
    )


def _terminate_all_sync() -> None:
    """atexit hook. The loop is gone by now in most cases, so we fall
    back to direct signals — best-effort, not async."""
    for entry in list(_REGISTRY.values()):
        proc = entry.proc
        if proc.returncode is not None:
            _REGISTRY.pop(entry.bg_id, None)
            continue
        pid = proc.pid
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        # Brief poll for graceful exit, then SIGKILL.
        deadline = time.time() + 1.0
        while time.time() < deadline:
            try:
                os.kill(pid, 0)  # probe
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        _REGISTRY.pop(entry.bg_id, None)
