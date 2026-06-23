"""S4 — Tier-0 deterministic slot resolution (no LLM).

A branch variable that is a pure function of data the engine can already read is
computed by the engine in code, and the segment that would have asked the model
to report it is skipped entirely — no provider round-trip, zero tokens.

A `flow.variables` entry opts in by carrying a `resolver` spec describing the
computation. Supported kinds (deliberately small; extend as needed):

    {"kind": "enum_check", "source": {...}, "allowed": ["US","EU","APAC"],
     "true": "valid", "false": "invalid"}
        Read a value from `source`; emit `true` if it is in `allowed` (and, when
        `non_empty` is set, non-empty), else `false`. Also covers presence
        checks via `non_empty: true` with no `allowed`.

    {"kind": "file_membership", "file": "data/fraud_blocklist.txt",
     "value_source": {...}, "true": "blocked", "false": "clear",
     "ignore_comments": true}
        Emit `true` if the value from `value_source` appears as a line in
        `file` (comment/blank lines ignored when `ignore_comments`), else
        `false`.

    {"kind": "jsonpath", "file": "data/x.json", "path": "a.b"}
        Pull a typed value out of a JSON file by dotted path.

    {"kind": "range", "source": {...}, "min": 0, "max": 5000,
     "true": "in", "false": "out"}
        Numeric bound check.

`source` / `value_source` shapes:
    {"slot": "name"}                     — an already-filled slot
    {"file": "data/x.json", "path": "a"} — a dotted path into a JSON file
    {"literal": "..."}                   — a constant

Resolution is best-effort and pure: a missing file, unreadable value, or
unknown kind returns "unresolved" for that variable, and the engine falls back
to running the segment with the LLM (Tier 1). It never raises into the run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_UNRESOLVED = object()  # sentinel: this resolver couldn't produce a value


def _read_json(base: Path | None, rel: str) -> Any:
    p = (base / rel) if base else Path(rel)
    return json.loads(p.read_text())


def _dotted(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            return _UNRESOLVED
    return cur


def _read_source(src: dict, slots: dict, base: Path | None) -> Any:
    """Resolve a value from a slot / file path / literal source."""
    if not isinstance(src, dict):
        return _UNRESOLVED
    if "slot" in src:
        return slots.get(src["slot"], _UNRESOLVED)
    if "literal" in src:
        return src["literal"]
    if "file" in src:
        try:
            data = _read_json(base, src["file"])
        except Exception:
            return _UNRESOLVED
        path = src.get("path")
        if not path:
            return data
        try:
            return _dotted(data, path)
        except Exception:
            return _UNRESOLVED
    return _UNRESOLVED


def _resolve_one(spec: dict, slots: dict, base: Path | None) -> Any:
    kind = spec.get("kind")
    if kind == "enum_check":
        val = _read_source(spec.get("source", {}), slots, base)
        if val is _UNRESOLVED:
            return _UNRESOLVED
        ok = True
        if spec.get("non_empty") and (val is None or val == ""):
            ok = False
        allowed = spec.get("allowed")
        if ok and allowed is not None:
            ok = val in allowed
        return spec.get("true", True) if ok else spec.get("false", False)

    if kind == "file_membership":
        val = _read_source(spec.get("value_source", {}), slots, base)
        if val is _UNRESOLVED or val is None:
            return _UNRESOLVED
        try:
            p = (base / spec["file"]) if base else Path(spec["file"])
            lines = p.read_text().splitlines()
        except Exception:
            return _UNRESOLVED
        members = set()
        for ln in lines:
            s = ln.strip()
            if not s:
                continue
            if spec.get("ignore_comments") and s.startswith("#"):
                continue
            members.add(s)
        found = str(val).strip() in members
        return spec.get("true", True) if found else spec.get("false", False)

    if kind == "jsonpath":
        try:
            data = _read_json(base, spec["file"])
        except Exception:
            return _UNRESOLVED
        out = _dotted(data, spec.get("path", ""))
        return out if out is not _UNRESOLVED else _UNRESOLVED

    if kind == "range":
        val = _read_source(spec.get("source", {}), slots, base)
        try:
            n = float(val)
        except (TypeError, ValueError):
            return _UNRESOLVED
        lo, hi = spec.get("min"), spec.get("max")
        ok = (lo is None or n >= lo) and (hi is None or n <= hi)
        return spec.get("true", True) if ok else spec.get("false", False)

    return _UNRESOLVED


def resolve_tier0(
    variables: list[dict],
    slots: dict,
    *,
    base_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Resolve EVERY variable in `variables` deterministically, or return None.

    Returns a `{variableName: value}` dict only when *all* given variables carry
    a `resolver` and every one resolved — that is the precondition for the
    engine to skip the segment's LLM call. If any variable lacks a resolver or
    can't be resolved, returns None and the caller runs the segment normally.
    """
    if not variables:
        return None
    out: dict[str, Any] = {}
    for v in variables:
        name = v.get("variableName")
        spec = v.get("resolver")
        if not isinstance(name, str) or not isinstance(spec, dict):
            return None
        val = _resolve_one(spec, {**slots, **out}, base_dir)
        if val is _UNRESOLVED:
            return None
        out[name] = val
    return out
