"""Parse a CLI agent's stdout into the engine's `SegmentResult` / slot dict.

CLI providers can't call our synthetic `record_slots` tool, so the contract
is JSON-on-stdout instead: the host agent is instructed to print a single
strict-JSON object as its final output. This module turns that text into the
same typed shapes the native path produces, tolerantly (a host CLI may wrap
the JSON in a result envelope, fence it in ```json, or emit prose around it).

Two layers of tolerance, both mirroring patterns already in the codebase
(`variable_normalizer._extract_json`, `segment_exec._loads_lenient`):

  1. Unwrap a known CLI envelope (claude-code's `--output-format json` puts
     the assistant text under `result`/`text`); then
  2. extract the inner JSON object from that text (fence-strip, then a
     last-resort `{...}` span match).
"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

from botcircuits.agent.workflow.engine.runner import SegmentResult


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

#: Envelope keys CLI agents commonly nest their assistant text under. We peel
#: these to reach the model's actual final message before looking for our
#: JSON contract inside it. `result` is claude-code's `--output-format json`.
_ENVELOPE_TEXT_KEYS = ("result", "text", "content", "response", "output")


def _loads_lenient(text: str) -> Any:
    """Parse text as JSON, then as a Python literal (single-quoted dicts are a
    common provider quirk). Returns the parsed object or ``None``."""
    for parse in (json.loads, ast.literal_eval):
        try:
            return parse(text)
        except (ValueError, SyntaxError, TypeError):
            continue
    return None


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```\s*$", "", t)
    return t.strip()


def extract_json_object(raw: str) -> dict | None:
    """Best-effort: pull a single JSON object out of arbitrary CLI stdout.

    Tries, in order: the whole string; a CLI envelope's text field; a
    fence-stripped body; a last-resort ``{...}`` span. Returns the object or
    ``None`` when nothing parses (caller treats that as an empty capture).
    """
    raw = raw or ""

    # 1. The whole thing parses to an object — either our contract directly,
    #    or a CLI envelope we then unwrap.
    direct = _loads_lenient(raw.strip())
    if isinstance(direct, dict):
        if any(k in direct for k in
               ("slots", "items", "paused", "needs_tool", "normalized")):
            return direct
        for key in _ENVELOPE_TEXT_KEYS:
            inner = direct.get(key)
            if isinstance(inner, str) and inner.strip():
                got = extract_json_object(inner)
                if got is not None:
                    return got
        # An envelope whose text field already held a parsed object.
        for key in _ENVELOPE_TEXT_KEYS:
            inner = direct.get(key)
            if isinstance(inner, dict):
                return inner
        return direct

    # 2. Fence-stripped body.
    body = _strip_fence(raw)
    parsed = _loads_lenient(body)
    if isinstance(parsed, dict):
        return parsed

    # 3. Last resort: the first {...} span anywhere in the output.
    m = _JSON_OBJECT_RE.search(body)
    if m:
        parsed = _loads_lenient(m.group(0))
        if isinstance(parsed, dict):
            return parsed
    return None


def assistant_text_from_stdout(raw: str) -> str:
    """Return the host CLI's final assistant TEXT from its stdout.

    Unlike `segment_result_from_stdout` / `normalized_slots_from_stdout` (which
    expect our JSON contract), this is for callers that want the raw model
    reply as a string — the build-time LLM helpers do their own JSON
    extraction on it. We only peel a known CLI envelope (claude-code's
    `--output-format json` nests the reply under `result`/`text`); anything
    else is passed through verbatim so the caller's own parser sees exactly
    what the model emitted.
    """
    raw = raw or ""
    direct = _loads_lenient(raw.strip())
    if isinstance(direct, dict):
        for key in _ENVELOPE_TEXT_KEYS:
            inner = direct.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner
    return raw


def segment_result_from_stdout(raw: str) -> SegmentResult:
    """Turn a CLI `run_segment` invocation's stdout into a `SegmentResult`.

    Recognized contract fields (all optional):
      - ``slots``    : ``{var: value}`` branch slots the agent observed.
      - ``items``    : list of per-item fact objects (listDecision segments).
      - ``paused``   : true when the agent needs the user (with ``question``).
      - ``question`` : the question to surface on pause.
      - ``text``     : the agent's final assistant text.

    A non-parsing / empty stdout yields an empty result — the engine then
    routes through its own clarification / default-branch logic rather than
    crashing, matching the native path's "never raise to the engine" stance.
    """
    obj = extract_json_object(raw)
    if not isinstance(obj, dict):
        return SegmentResult(text=(raw or "").strip())

    slots = obj.get("slots")
    captured_slots = {
        k: v for k, v in slots.items()
        if v is not None and v != ""
    } if isinstance(slots, dict) else {}

    items = obj.get("items")
    if isinstance(items, str):
        items = _loads_lenient(items)
    captured_items = [it for it in items if isinstance(it, dict)] \
        if isinstance(items, list) else []

    paused = bool(obj.get("paused"))
    question = obj.get("question") if isinstance(obj.get("question"), str) else ""
    text = obj.get("text") if isinstance(obj.get("text"), str) else ""

    # A permission-style pause names the tool(s) it was blocked on. Accept a
    # list of strings or a single string; ignore anything else.
    raw_needs = obj.get("needs_tool")
    if isinstance(raw_needs, str):
        needs_tool = [raw_needs] if raw_needs.strip() else []
    elif isinstance(raw_needs, list):
        needs_tool = [t for t in raw_needs if isinstance(t, str) and t.strip()]
    else:
        needs_tool = []

    return SegmentResult(
        text=text,
        captured_slots=captured_slots,
        captured_items=captured_items,
        paused=paused,
        question=question or "",
        needs_tool=needs_tool,
    )


def normalized_slots_from_stdout(raw: str) -> dict[str, Any]:
    """Turn a CLI `resolve_slots` (Tier-2) invocation's stdout into a
    ``{variableName: value}`` dict.

    Accepts either the bare object or ``{"normalized": {...}}`` (the shape
    `variable_normalizer` already uses), so the existing prompt body and
    hallucination guard carry over unchanged — only the transport differs.
    """
    obj = extract_json_object(raw)
    if not isinstance(obj, dict):
        return {}
    inner = obj.get("normalized")
    if isinstance(inner, dict):
        return inner
    # Bare object: treat top-level keys as the normalized values, but drop
    # any contract/envelope keys that aren't slot values.
    return {
        k: v for k, v in obj.items()
        if k not in ("paused", "question", "text", "items", "needs_tool")
    }


__all__ = [
    "extract_json_object",
    "assistant_text_from_stdout",
    "segment_result_from_stdout",
    "normalized_slots_from_stdout",
]
