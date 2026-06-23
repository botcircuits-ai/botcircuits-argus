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


__all__ = ["sessions_dir", "list_sessions", "get_session", "SESSIONS_DIR_ENV"]
