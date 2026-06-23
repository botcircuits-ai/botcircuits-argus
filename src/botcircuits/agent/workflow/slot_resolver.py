"""Deterministic slot resolution for workflow re-entry.

Sits between the raw tool args and Layer B (LLM extraction,
`variable_normalizer.py`): given the variables a pending branch
references, resolve as many as possible WITHOUT an LLM call. Only the
variables this module cannot satisfy are handed to Layer B — when it
resolves everything, the LLM round-trip is skipped entirely. This is
both the token saver and the determinism fix: a value resolved here is
the same value on every run.

Resolution sources, per variable, first hit wins:

  1. Raw args — the model passed the variable explicitly and the value
     coerces to the declared dataType.
  2. Choice-value match — the pending step's `choices[].expressionList`
     carries the literal values the branch compares against (`is`,
     `contains`, ...). If exactly one of them appears in the fresh
     context (last user message + raw args), that authored value IS the
     answer — assigned with its authored casing so `is` matches.
  3. Typed extraction from the last user message — a number-typed
     variable resolves when the message contains exactly one number; a
     boolean-typed one when the message (or its first word) is an
     unambiguous yes/no token.
  4. Question verbatim reply — when the pending step is a `question`
     referencing a single string variable with no authored choice
     values (branch only checks emptiness or containment), the user's
     reply is the slot value, verbatim.
  5. Saved slot — the variable already holds a coercible value from an
     earlier turn. Lowest priority: sources 1-4 read the FRESH turn, so
     a new answer always beats a stale one (matters when a loop
     re-visits the same branching step).

Anything still unresolved is returned as a spec list for Layer B. The
resolver never guesses: ambiguity (two choice values matched, two
numbers in the message) means "unresolved", not "pick one".

This module also owns the scalar Layer-A coercers (`coerce_number`,
`coerce_boolean`, `coerce_string`); `local.py` imports them for its
`_coerce_variables`, keeping one source of truth for type coercion.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any


# Strings that coerce to False for boolean-typed variables. Anything truthy
# outside this set is treated as `True`; anything ambiguous (e.g. "maybe")
# is dropped rather than guessed.
FALSY_STRINGS = {"false", "no", "0", "off", "", "null", "none"}
TRUTHY_STRINGS = {"true", "yes", "1", "on"}


class Missing:
    """Sentinel marker for 'coercion failed — drop this slot'."""
    __slots__ = ()


MISSING = Missing()


def coerce_number(value: Any) -> Any:
    """Best-effort number coercion. Returns the `MISSING` sentinel when
    the value can't be turned into a number — caller treats that as
    'drop this slot'."""
    if isinstance(value, bool):
        # bool is a subclass of int in Python — treat it as a coercion
        # failure for number-typed variables so we don't accept `True` as 1.
        return MISSING
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return MISSING
        try:
            if "." in s or "e" in s.lower():
                return float(s)
            return int(s)
        except ValueError:
            return MISSING
    return MISSING


def coerce_boolean(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in TRUTHY_STRINGS:
            return True
        if s in FALSY_STRINGS:
            return False
        return MISSING
    return MISSING


def coerce_string(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _coerce(value: Any, dtype: str) -> Any:
    dtype = (dtype or "string").lower()
    if dtype == "number":
        return coerce_number(value)
    if dtype == "boolean":
        return coerce_boolean(value)
    return coerce_string(value)


# Number tokens: standalone integers/decimals, not digits embedded in
# identifiers ("sys_10001") or dotted versions ("v1.2.3").
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_.])[-+]?\d+(?:\.\d+)?(?![A-Za-z0-9_])"
)

# Operators whose authored `value` is a literal the slot is compared
# against — usable as an answer candidate. Ordered/numeric operators and
# emptiness checks carry no usable literal.
_CANDIDATE_OPERATORS = {"is", "contains", "starts with", "ends with"}


def _candidate_values(flow: dict, step_id: str, variable_name: str) -> list[str]:
    """Collect the literal string values the pending step's choices
    compare `variable_name` against. These are the only values that can
    route the branch, so finding one of them in the fresh context is a
    deterministic answer. Values carrying `{slot}` placeholders are
    skipped — they aren't literals until interpolation.
    """
    step = (flow.get("steps") or {}).get(step_id) or {}
    seen: set[str] = set()
    out: list[str] = []
    for choice in step.get("choices") or []:
        for expr in choice.get("expressionList") or []:
            if expr.get("variable") != variable_name:
                continue
            if expr.get("operator") not in _CANDIDATE_OPERATORS:
                continue
            value = expr.get("value")
            if not isinstance(value, str) or not value.strip() or "{" in value:
                continue
            key = value.strip().lower()
            if key not in seen:
                seen.add(key)
                out.append(value.strip())
    return out


def _match_candidate(candidates: list[str], haystack: str) -> str | None:
    """Return the single candidate present in `haystack` (case-insensitive,
    token-boundary match), or None when zero or several match — several
    means the context is ambiguous and an LLM should arbitrate.
    """
    lowered = haystack.lower()
    matched: list[str] = []
    for cand in candidates:
        pattern = (
            r"(?<![A-Za-z0-9])" + re.escape(cand.lower()) + r"(?![A-Za-z0-9])"
        )
        if re.search(pattern, lowered):
            matched.append(cand)
    return matched[0] if len(matched) == 1 else None


def _extract_number(text: str) -> Any:
    """The single number in `text`, or MISSING when there are zero or
    several distinct ones."""
    tokens = {m.group(0) for m in _NUMBER_RE.finditer(text)}
    if len(tokens) != 1:
        return MISSING
    return coerce_number(next(iter(tokens)))


def _extract_boolean(text: str) -> Any:
    """Boolean from an unambiguous yes/no reply. Tries the whole message
    first ("yes", "true"), then its first word ("No, keep it") — both
    stripped of punctuation. Anything else is MISSING."""
    s = text.strip().lower().strip(".,!?")
    if not s:
        return MISSING
    if s in TRUTHY_STRINGS:
        return True
    if s in FALSY_STRINGS:
        return False
    first = s.split()[0].strip(".,!?")
    if first in TRUTHY_STRINGS:
        return True
    if first in FALSY_STRINGS:
        return False
    return MISSING


def _is_question_step(flow: dict, step_id: str) -> bool:
    step = (flow.get("steps") or {}).get(step_id) or {}
    return step.get("type") == "question"


def resolve_slots(
    *,
    flow: dict,
    step_id: str,
    variables: list[dict],
    raw_args: dict,
    saved_slots: dict,
    last_user_message: str,
) -> tuple[dict[str, Any], list[dict]]:
    """Resolve branch variables deterministically.

    Returns `(resolved, unresolved)`: `resolved` maps variable names to
    already-coerced values; `unresolved` is the subset of `variables`
    (spec dicts, same shape as the input) that still needs Layer B.
    """
    raw_args = raw_args if isinstance(raw_args, dict) else {}
    saved_slots = saved_slots if isinstance(saved_slots, dict) else {}
    user_text = last_user_message or ""
    # Choice-value matching also scans raw args so a value the model
    # parked under the wrong key (e.g. {"status": "delivered"} for
    # `order_status`) still resolves.
    fresh_context = "\n".join(
        [user_text, json.dumps(raw_args, default=str) if raw_args else ""]
    )

    resolved: dict[str, Any] = {}
    unresolved: list[dict] = []

    for spec in variables:
        if not isinstance(spec, dict):
            continue
        name = spec.get("variableName")
        if not isinstance(name, str) or not name:
            continue
        dtype = (spec.get("dataType") or "string").lower()
        candidates = _candidate_values(flow, step_id, name)

        # 1. Explicit raw arg, if it coerces.
        if name in raw_args:
            coerced = _coerce(raw_args[name], dtype)
            if not isinstance(coerced, Missing):
                resolved[name] = coerced
                _log(name, coerced, "raw args")
                continue

        # 2. Exactly one of the branch's authored literals appears in
        #    the fresh context.
        if dtype == "string" and candidates:
            hit = _match_candidate(candidates, fresh_context)
            if hit is not None:
                resolved[name] = hit
                _log(name, hit, "choice-value match")
                continue

        # 3. Typed extraction from the user's reply.
        if user_text:
            if dtype == "number":
                value = _extract_number(user_text)
                if not isinstance(value, Missing):
                    resolved[name] = value
                    _log(name, value, "number in user reply")
                    continue
            elif dtype == "boolean":
                value = _extract_boolean(user_text)
                if not isinstance(value, Missing):
                    resolved[name] = value
                    _log(name, value, "yes/no user reply")
                    continue

        # 4. Question step + single string variable + no authored
        #    literals: the reply IS the value.
        if (
            dtype == "string"
            and not candidates
            and user_text.strip()
            and len(variables) == 1
            and _is_question_step(flow, step_id)
        ):
            value = user_text.strip()
            resolved[name] = value
            _log(name, value, "question verbatim reply")
            continue

        # 5. A coercible value already saved on the session.
        if name in saved_slots:
            coerced = _coerce(saved_slots[name], dtype)
            if not isinstance(coerced, Missing):
                resolved[name] = coerced
                _log(name, coerced, "saved slot")
                continue

        unresolved.append(spec)

    return resolved, unresolved


def _log(name: str, value: Any, source: str) -> None:
    print(
        f"[workflow] slot resolver: {name}={value!r} ({source})",
        file=sys.stderr,
    )


__all__ = [
    "FALSY_STRINGS",
    "TRUTHY_STRINGS",
    "Missing",
    "MISSING",
    "coerce_boolean",
    "coerce_number",
    "coerce_string",
    "resolve_slots",
]
