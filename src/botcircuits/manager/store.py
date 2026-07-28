"""Session trace store — reads the files the tracing layer writes.

The manager is read-only over ``.botcircuits/sessions/*-session.json``. This
module is the single place that knows the on-disk layout, so the API layer
deals in plain dicts. No database: the session files ARE the store.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

#: Env override for where session files live (else the workflow tracing default).
SESSIONS_DIR_ENV = "BOTCIRCUITS_SESSIONS_DIR"
_SESSION_SUFFIX = "-session.json"


def sessions_dir() -> Path:
    """The directory holding ``<session_id>-session.json`` files."""
    raw = os.getenv(SESSIONS_DIR_ENV)
    if raw:
        return Path(raw).expanduser().resolve()
    # Reuse the tracer's resolution so writer and reader never diverge.
    from botcircuits.agent.workflow.tracing import SessionTrace

    return SessionTrace.sessions_dir().resolve()


def _read(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _gate_summary(trace: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Fold a session's `verification`/`retry` events into a compact verdict:
    the last gate evaluation's pass/fail, how many evaluations ran, and how
    many repair retries were triggered. `None` when the workflow has no gate
    (no `verification` events at all) — distinct from a gate that ran and
    passed, so the UI can tell "no gate configured" from "gate passed"."""
    verifications = [e for e in trace if e.get("type") == "verification"]
    if not verifications:
        return None
    retries = [e for e in trace if e.get("type") == "retry"]
    last = verifications[-1].get("data") or {}
    return {
        "passed": bool(last.get("passed")),
        "attempts": len(verifications),
        "retries": len(retries),
        "checks": last.get("checks") or [],
    }


def _summary(doc: dict[str, Any], *, mtime: float) -> dict[str, Any]:
    """The compact record the list endpoint returns (no full trace)."""
    wf = doc.get("workflow") or {}
    trace = doc.get("trace") or []
    end = wf.get("end")
    last = trace[-1] if trace else {}
    status = "running"
    if end:
        # The session_end event carries the terminal status.
        for ev in reversed(trace):
            if ev.get("type") == "session_end":
                status = (ev.get("data") or {}).get("status") or "done"
                break
        else:
            status = "done"
    elif last.get("type") == "paused":
        status = "paused"
    return {
        "session_id": doc.get("session_id"),
        "workflow": wf.get("name"),
        "runtime": (doc.get("agent") or {}).get("runtime"),
        "start": wf.get("start"),
        "end": end,
        "status": status,
        "event_count": len(trace),
        "updated_at": mtime,
        "gate": _gate_summary(trace),
    }


def list_sessions() -> list[dict[str, Any]]:
    """All sessions, newest first, as compact summaries."""
    d = sessions_dir()
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in d.glob(f"*{_SESSION_SUFFIX}"):
        doc = _read(path)
        if doc is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        out.append(_summary(doc, mtime=mtime))
    out.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
    return out


def get_session(session_id: str) -> dict[str, Any] | None:
    """The full session document (trace + memory graph), or ``None``.

    ``session_id`` is matched against the filename; we guard against path
    traversal by rejecting anything that isn't a bare id token.
    """
    if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
        return None
    path = sessions_dir() / f"{session_id}{_SESSION_SUFFIX}"
    return _read(path)


def latest_gate_by_workflow() -> dict[str, dict[str, Any]]:
    """The gate verdict from each workflow's MOST RECENT session, keyed by
    workflow name. Used by the workflow list endpoint so it can show "did the
    last run pass verification" without the caller reading session files
    itself. Sessions with no gate (no `verification` events) are skipped —
    they leave the workflow absent from the map rather than reporting a
    false verdict."""
    out: dict[str, dict[str, Any]] = {}
    latest_mtime: dict[str, float] = {}
    d = sessions_dir()
    if not d.is_dir():
        return out
    for path in d.glob(f"*{_SESSION_SUFFIX}"):
        doc = _read(path)
        if doc is None:
            continue
        name = (doc.get("workflow") or {}).get("name")
        if not name:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if name in latest_mtime and mtime <= latest_mtime[name]:
            continue
        gate = _gate_summary(doc.get("trace") or [])
        if gate is None:
            continue
        latest_mtime[name] = mtime
        out[name] = gate
    return out


__all__ = [
    "sessions_dir", "list_sessions", "get_session", "latest_gate_by_workflow",
    "SESSIONS_DIR_ENV",
]
