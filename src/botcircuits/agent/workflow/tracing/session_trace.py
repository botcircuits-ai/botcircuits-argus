"""Per-session workflow execution tracing.

A *session* is one workflow run, identified by an auto-generated `session_id`,
persisted to ``.botcircuits/sessions/<session_id>-session.json``. The file is
the single source of truth for the manager web's trace + memory-graph view.

The session file survives the pause/resume boundary: a workflow that pauses for
human feedback and is resumed in a later process keeps the SAME `session_id`
(threaded through the run-state file by the runner), so its trace events append
into one continuous timeline rather than splitting across files.

Schema (``<session_id>-session.json``)::

    {
      "session_id": "<uuid>",
      "agent":    {"runtime": "claude-code" | "self" | ...},
      "workflow": {"name": "...", "start": "<iso8601>", "end": "<iso8601>|null",
                   "initial_slots": {...}},
      "trace":  [ <event>, ... ],
      "memory": { "nodes": [...], "edges": [...] }
    }

Each ``trace`` event is::

    {"seq": 0, "ts": "<iso8601>", "type": "<event-type>", "step": "<id>|null",
     "duration_ms": <float>|null, "slots": {<snapshot at this moment>},
     "data": { ... type-specific ... }}

Event types (see EventType): ``session_start``, ``step_enter``,
``action_before``, ``action_after``, ``slot_resolve``, ``branch``,
``session_end``.

Writing is best-effort and never raises into the run — tracing must not be able
to break a workflow. Each append rewrites the file atomically (temp + rename),
which is fine at the per-step cadence of a workflow run.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_session_id() -> str:
    """A fresh, unique session id."""
    return uuid.uuid4().hex


class EventType:
    SESSION_START = "session_start"
    STEP_ENTER = "step_enter"
    ACTION_BEFORE = "action_before"
    ACTION_AFTER = "action_after"
    SLOT_RESOLVE = "slot_resolve"
    BRANCH = "branch"
    USAGE = "usage"
    SESSION_END = "session_end"


def _public_slots(slots: dict[str, Any] | None) -> dict[str, Any]:
    """A serializable snapshot of slots with engine-internal (``__*``) keys
    dropped and non-JSON values stringified."""
    out: dict[str, Any] = {}
    for k, v in (slots or {}).items():
        if isinstance(k, str) and k.startswith("__"):
            continue
        try:
            json.dumps(v)
            out[k] = v
        except (TypeError, ValueError):
            out[k] = repr(v)
    return out


def _flow_graph(flow: dict[str, Any] | None) -> dict[str, Any]:
    """A compact branch topology for the trace view.

    Shape::

        {"start": "<step id>",
         "steps": {
            "<id>": {
              "type": "agentAction|question|start|parallel|...",
              "action": "<slot-free action text>",
              "next": "<default/otherwise next id or null>",
              "choices": [{"condition": "<NL test>", "next": "<id>"}, ...],
              # `type: "parallel"` only:
              "branches": {"<name>": ["<step id>", ...], ...},
              "onError": "<step id or null>"
            }, ...
         }}

    Reads the human-readable `conditions` (the authored NL test) when present,
    falling back to the compiled `choices[].next` so we always know the targets
    even on a workflow built without conditions echoed back. A `parallel`
    step's `branches`/`onError` are passed through as-is (already step-id
    references, nothing to compile) so the trace view can draw the fan-out/
    join edges the same way the authoring canvas does.
    """
    if not isinstance(flow, dict):
        return {}
    steps_in = flow.get("steps") or {}
    if not isinstance(steps_in, dict):
        return {}

    steps_out: dict[str, Any] = {}
    for sid, step in steps_in.items():
        if not isinstance(step, dict):
            continue
        action = ""
        settings = step.get("settings")
        if isinstance(settings, dict):
            action = str(settings.get("action") or "")

        # Prefer authored `conditions` (carry the NL test); else derive labels
        # from compiled `choices` so the edges still have targets.
        choices_out: list[dict[str, Any]] = []
        conditions = step.get("conditions")
        if isinstance(conditions, list) and conditions:
            for c in conditions:
                if isinstance(c, dict) and c.get("next"):
                    choices_out.append({
                        "condition": str(c.get("condition") or ""),
                        "next": c.get("next"),
                    })
        else:
            for c in step.get("choices") or []:
                if isinstance(c, dict) and c.get("next"):
                    choices_out.append({
                        "condition": str(c.get("expCondition") or ""),
                        "next": c.get("next"),
                    })

        steps_out[sid] = {
            "type": step.get("type"),
            "action": action,
            "next": step.get("next"),
            "choices": choices_out,
        }
        if step.get("type") == "parallel":
            branches = step.get("branches")
            if isinstance(branches, dict):
                steps_out[sid]["branches"] = {
                    str(name): [s for s in chain if isinstance(s, str)]
                    for name, chain in branches.items()
                    if isinstance(chain, list)
                }
            on_error = step.get("onError")
            if isinstance(on_error, str) and on_error:
                steps_out[sid]["onError"] = on_error

    return {"start": flow.get("start"), "steps": steps_out}


class SessionTrace:
    """Writer for one session's trace file.

    Create with :meth:`start` (fresh run) or :meth:`load` (resume). All
    ``record_*`` / ``event`` calls append to ``trace`` and flush to disk. The
    writer is intentionally synchronous and self-contained: the engine calls it
    from its async loop, but the work is a small file write.
    """

    def __init__(self, path: Path, doc: dict[str, Any]):
        self._path = path
        self._doc = doc

    # -- lifecycle ----------------------------------------------------------

    @staticmethod
    def sessions_dir() -> Path:
        """``.botcircuits/sessions`` under the workflows root, honoring the
        same dir override the rest of the local workflow code uses."""
        from botcircuits.agent.workflow.local import _resolve_workflows_dir

        return _resolve_workflows_dir() / ".." / "sessions"

    @classmethod
    def path_for(cls, session_id: str) -> Path:
        return (cls.sessions_dir() / f"{session_id}-session.json").resolve()

    @classmethod
    def start(
        cls,
        *,
        workflow_name: str,
        runtime: str,
        initial_slots: dict[str, Any] | None,
        session_id: str | None = None,
        flow: dict[str, Any] | None = None,
    ) -> "SessionTrace":
        """Begin a new session and emit ``session_start``.

        `flow` is the built workflow's flow dict; we snapshot its branch
        topology (steps, their conditions → next, default next) into
        ``workflow.graph`` so the trace view can draw the FULL workflow —
        including conditional paths that this run did not take — and overlay
        the path the trace actually walked.
        """
        sid = session_id or new_session_id()
        doc = {
            "session_id": sid,
            "agent": {"runtime": runtime},
            "workflow": {
                "name": workflow_name,
                "start": _now_iso(),
                "end": None,
                "initial_slots": _public_slots(initial_slots),
                "graph": _flow_graph(flow),
            },
            "trace": [],
            "memory": {"nodes": [], "edges": []},
        }
        trace = cls(cls.path_for(sid), doc)
        trace.event(
            EventType.SESSION_START,
            slots=initial_slots,
            data={"runtime": runtime, "workflow": workflow_name},
        )
        return trace

    @classmethod
    def load(cls, session_id: str) -> "SessionTrace | None":
        """Reopen an existing session file for appending (resume). Returns
        ``None`` if it can't be read."""
        path = cls.path_for(session_id)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(doc, dict):
            return None
        return cls(path, doc)

    @property
    def session_id(self) -> str:
        return self._doc.get("session_id", "")

    # -- events -------------------------------------------------------------

    def event(
        self,
        event_type: str,
        *,
        step: str | None = None,
        slots: dict[str, Any] | None = None,
        duration_ms: float | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Append one trace event and flush. Never raises."""
        try:
            trace = self._doc.setdefault("trace", [])
            trace.append({
                "seq": len(trace),
                "ts": _now_iso(),
                "type": event_type,
                "step": step,
                "duration_ms": duration_ms,
                "slots": _public_slots(slots),
                "data": data or {},
            })
            self._flush()
        except Exception:  # pragma: no cover - tracing must never break a run
            pass

    def end(self, *, status: str, summary: str = "", slots: dict[str, Any] | None = None) -> None:
        """Mark the session finished: stamp ``workflow.end`` and emit
        ``session_end``."""
        try:
            self._doc.setdefault("workflow", {})["end"] = _now_iso()
        except Exception:  # pragma: no cover
            pass
        self.event(
            EventType.SESSION_END,
            slots=slots,
            data={"status": status, "summary": summary},
        )

    # -- memory graph -------------------------------------------------------

    def add_memory_node(self, node_id: str, kind: str, **attrs: Any) -> None:
        """Add (or update) a node in the session's memory graph. Used to map
        slots/steps into the graph the manager web renders."""
        try:
            mem = self._doc.setdefault("memory", {"nodes": [], "edges": []})
            nodes = mem.setdefault("nodes", [])
            for n in nodes:
                if n.get("id") == node_id:
                    n.update({"kind": kind, **attrs})
                    break
            else:
                nodes.append({"id": node_id, "kind": kind, **attrs})
            self._flush()
        except Exception:  # pragma: no cover
            pass

    def add_memory_edge(self, src: str, dst: str, **attrs: Any) -> None:
        try:
            mem = self._doc.setdefault("memory", {"nodes": [], "edges": []})
            edges = mem.setdefault("edges", [])
            edges.append({"from": src, "to": dst, **attrs})
            self._flush()
        except Exception:  # pragma: no cover
            pass

    # -- persistence --------------------------------------------------------

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".session-", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._doc, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)
        except Exception:  # pragma: no cover
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


class _Timer:
    """Small monotonic stopwatch returning elapsed milliseconds."""

    def __init__(self) -> None:
        self._t0 = time.perf_counter()

    def ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000.0, 3)


def timer() -> _Timer:
    return _Timer()


__all__ = ["SessionTrace", "EventType", "new_session_id", "timer"]
