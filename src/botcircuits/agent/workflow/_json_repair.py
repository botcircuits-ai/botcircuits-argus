"""Tolerant JSON extraction from LLM output.

Models asked for strict JSON still sometimes wrap it in code fences, add
leading prose, or make small syntax slips (a missing comma between array
members, a trailing comma before a closer). This is the one place that
repair logic lives — `condition_processor.py` and the verification
gate's LLM-judge executor both import it rather than keeping their own
copies.
"""

from __future__ import annotations

import json
import re


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def repair_json(text: str) -> str:
    """Light, safe repairs for the JSON slips models make most often: a
    missing comma between adjacent array/object members, and a trailing
    comma before a closer. Only edits whitespace between two structural
    tokens, so it can't corrupt string contents."""
    # comma between a value-closer and the next value-opener across a newline
    text = re.sub(r'([}\]"0-9eltn])\s*\n(\s*)([{\[\"])', r"\1,\n\2\3", text)
    # drop trailing commas before a closer
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text


def extract_json(raw: str) -> dict:
    """Strip code fences / leading prose and parse the first JSON object.
    Falls back to a light comma-repair for the common model JSON slips
    before giving up."""
    text = raw.strip()
    if text.startswith("```"):
        # Drop the opening ``` (and optional language tag) plus the closing ```.
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try the largest {...} span, then the same span repaired.
    candidates = []
    m = _JSON_OBJECT_RE.search(text)
    if m:
        candidates.append(m.group(0))
    candidates.append(text)
    for cand in candidates:
        for variant in (cand, repair_json(cand)):
            try:
                return json.loads(variant)
            except json.JSONDecodeError:
                continue
    # Re-raise the original error on the primary text for a clear message.
    return json.loads(text)
