"""Small helpers shared by the local STM handlers.

Ports of `runtime-handler` utilities, narrowed to what the action/choice
handlers actually use.
"""

from __future__ import annotations

import json
import re
from typing import Any


def get_next_step_for_prompt_action(journey_id: str, settings: dict) -> str:
    """Build the next-step id used in the agent-action response.

    Uses the qualified `<journeyId>:<nextStepId>` form so downstream
    consumers that need to disambiguate by journey have it.
    """
    return f"{journey_id}:{settings.get('nextStepId')}"


def fill_text_with_slots(display_text: str | None, session_context: dict) -> str:
    """Interpolate `{slot}` placeholders in `display_text` using slot values.

    Case-insensitive on the slot name. Dict slot values are JSON-encoded so
    they don't blow up `re.sub`'s backref handling.
    """
    if not display_text:
        return ""

    out = display_text
    for slot_key, slot_value in (session_context.get("slots") or {}).items():
        if slot_value is None or slot_value == "":
            continue
        if isinstance(slot_value, (dict, list)):
            slot_value = json.dumps(slot_value, default=str)
        # Escape any backslashes in the replacement so they don't get
        # interpreted as regex backrefs.
        replacement = str(slot_value).replace("\\", r"\\")
        out = re.compile(rf"\{{{slot_key}\}}", re.IGNORECASE).sub(replacement, out)
    return out.strip()


def coerce_for_compare(value: Any) -> Any:
    """Strip surrounding whitespace on strings so comparisons aren't tripped
    by stray padding (mirrors the runtime-handler behavior)."""
    if isinstance(value, str):
        return value.strip()
    return value


def to_number(value: Any) -> float | None:
    """Best-effort coerce a slot/check value to a float for ordered (`<`, `<=`,
    `>`, `>=`) comparisons. Returns ``None`` when the value can't be compared
    numerically — e.g. an unfilled slot (``None``), a non-numeric string, or a
    bool (which we deliberately exclude so ``True``/``1`` don't conflate).

    Ordered comparisons must coerce *both* operands through this so a slot that
    arrives as a string (`"640"`) or was never filled (`None`) routes to the
    fall-through branch instead of raising ``TypeError``."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None
