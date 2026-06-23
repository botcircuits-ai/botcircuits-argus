"""LLM-driven variable normalization for workflow re-entry.

Layer B of the A+B normalization stack (Layer A is deterministic type
coercion in `local.py`). B runs only when an agentAction whose root
carries `choices`/`conditions` is about to be re-entered — i.e., when a
branching decision is imminent. For non-branching actions it is skipped
entirely so the workflow tool costs no extra LLM round-trip.

What B does:

  1. Pick the variables the pending branch actually references (a
     subset of `flow.variables`).
  2. Build a strict-JSON extraction prompt with the variable schema +
     raw tool args + the action text + the last assistant message.
  3. Call the agent's `LLMProvider` (same provider/model the main loop
     uses, no streaming, no tools).
  4. Parse the JSON.
  5. Hallucination guard: drop any extracted value that doesn't appear
     verbatim (case-insensitive) somewhere in the input context.
  6. Return a `{variableName: value}` dict for `local.py` to merge into
     slots before type coercion (Layer A) runs.

Failure policy: never raise to the caller. If the provider errors, the
JSON parse fails, or the response is structurally wrong, return `{}` and
let the workflow continue with raw args. The caller logs the warning.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from botcircuits.providers.base import LLMProvider
from botcircuits.types import Message


# Same regex / fence-stripping shape as the indexer, kept independent so
# the two can evolve apart.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def variables_for_step(flow: dict, step_id: str) -> list[dict]:
    """Filter `flow.variables` down to ones the given step's `choices`
    actually reference. Listing irrelevant variables wastes tokens and
    invites the model to fabricate values for them.
    """
    variables = flow.get("variables") or []
    if not isinstance(variables, list):
        return []
    steps = flow.get("steps") or {}
    step = steps.get(step_id) or {}
    choices = step.get("choices") or []

    referenced: set[str] = set()
    for ch in choices:
        for expr in (ch.get("expressionList") or []):
            var = expr.get("variable")
            if isinstance(var, str):
                referenced.add(var)

    return [
        v for v in variables
        if isinstance(v, dict) and v.get("variableName") in referenced
    ]


def _build_prompt(
    variables: list[dict],
    raw_args: dict,
    action_text: str,
    last_assistant_message: str,
    last_user_message: str,
) -> str:
    """Render the user-side prompt. The system prompt (set in `normalize`)
    fixes the JSON-only contract; this body lists the schema + sources."""
    schema_lines: list[str] = []
    for v in variables:
        name = v.get("variableName")
        dtype = v.get("dataType") or "string"
        desc = v.get("description") or ""
        schema_lines.append(f"  - {name} ({dtype}): {desc}")

    args_json = json.dumps(raw_args or {}, indent=2, default=str)

    return "\n".join([
        "You are extracting and normalizing variables that a workflow "
        "needs in order to branch correctly.",
        "",
        "Variables expected (return ONLY these; do not invent others):",
        "\n".join(schema_lines) if schema_lines else "  (none)",
        "",
        "Raw arguments the agent passed to the workflow tool:",
        args_json,
        "",
        "The action the agent just performed was:",
        action_text or "(no action text)",
        "",
        "Last assistant message before re-entry (may contain values to extract):",
        last_assistant_message or "(no prior assistant message)",
        "",
        "Last user message before re-entry — this is the user's DIRECT "
        "REPLY to the action above. Treat it as the answer the action "
        "was asking for, even if the value looks unusual or unexpected "
        "for the variable's type (e.g. a code-looking id 'sys_10001' is "
        "still a valid string answer):",
        last_user_message or "(no prior user message)",
        "",
        "Return strict JSON of the form:",
        '  {"normalized": {"<variableName>": <value>, ...}}',
        "",
        "Rules:",
        "  - Use the dataType verbatim: numbers as JSON numbers, "
        "booleans as true/false, strings as JSON strings.",
        "  - If the action asked for a value and the user's last message "
        "provides one, capture it verbatim — do NOT second-guess whether "
        "the value 'looks right' for the variable's domain. The workflow "
        "decides what to do with it next.",
        "  - Only OMIT a key when the value is genuinely absent from "
        "both the raw args and the user's last message. Do not guess "
        "values that aren't present.",
        "  - For string-typed variables with an enum hint in the "
        "description (e.g. 'one of: a | b | c'), return one of the listed "
        "values.",
        "  - Do not include any keys other than the listed variables.",
    ])


def _extract_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_OBJECT_RE.search(text)
        if not m:
            raise
        return json.loads(m.group(0))


def _value_present_in_context(value: Any, context_blob: str) -> bool:
    """Hallucination guard: was this value (or something close to it)
    actually present in the input context?

    Case-insensitive substring match on the string form. Booleans always
    pass (true/false are trivially present in any English text and the
    schema constrains them). Numbers are matched both as-typed and
    stripped of trailing `.0` (e.g. `500.0` should match `500`).
    """
    if isinstance(value, bool):
        return True
    haystack = context_blob.lower()
    if isinstance(value, (int, float)):
        candidates = {str(value).lower(), str(value).rstrip("0").rstrip(".").lower()}
        return any(c and c in haystack for c in candidates)
    if isinstance(value, str):
        v = value.strip().lower()
        if not v:
            # Empty string is allowed when the schema's `is empty` check
            # genuinely fires — let A handle that case.
            return True
        return v in haystack
    # Unknown types — let it through and rely on A's coercion to drop bad ones.
    return True


async def normalize(
    *,
    provider: LLMProvider,
    variables: list[dict],
    raw_args: dict,
    action_text: str,
    last_assistant_message: str,
    last_user_message: str = "",
    max_tokens: int = 1024,
) -> dict[str, Any]:
    """Run Layer B. Returns a `{variableName: value}` dict (possibly empty).

    Never raises. On any failure path (provider error, bad JSON, schema
    mismatch) logs a single line to stderr and returns `{}` so the
    caller falls back to raw args + Layer A.
    """
    if not variables:
        return {}

    prompt = _build_prompt(
        variables, raw_args, action_text,
        last_assistant_message, last_user_message,
    )
    messages = [Message(role="user", blocks=[{"type": "text", "text": prompt}])]
    system = (
        "You produce strict JSON that matches the requested schema. "
        "No commentary, no markdown code fences, no extra keys."
    )

    try:
        response = await provider.complete(
            system=system,
            messages=messages,
            tools=[],
            hosted_mcp=[],
            skills=[],
            max_tokens=max_tokens,
        )
    except Exception as e:
        print(
            f"[workflow] variable normalization skipped: provider error "
            f"{type(e).__name__}: {e}",
            file=sys.stderr,
        )
        return {}

    try:
        parsed = _extract_json(response.text or "")
    except json.JSONDecodeError as e:
        print(
            f"[workflow] variable normalization skipped: bad JSON "
            f"({e}). Raw: {(response.text or '')[:200]!r}",
            file=sys.stderr,
        )
        return {}

    normalized = parsed.get("normalized") if isinstance(parsed, dict) else None
    if not isinstance(normalized, dict):
        print(
            "[workflow] variable normalization skipped: response missing "
            "'normalized' object.",
            file=sys.stderr,
        )
        return {}

    # Restrict to declared variable names — drop anything else the model
    # invented.
    allowed = {
        v["variableName"] for v in variables
        if isinstance(v, dict) and isinstance(v.get("variableName"), str)
    }

    # Hallucination guard: every value must appear in the input context.
    context_blob = "\n".join([
        json.dumps(raw_args or {}, default=str),
        action_text or "",
        last_assistant_message or "",
        last_user_message or "",
    ])

    cleaned: dict[str, Any] = {}
    for name, value in normalized.items():
        if name not in allowed:
            continue
        if not _value_present_in_context(value, context_blob):
            print(
                f"[workflow] dropping hallucinated value "
                f"{name}={value!r} (not present in source context)",
                file=sys.stderr,
            )
            continue
        cleaned[name] = value

    return cleaned


__all__ = ["normalize", "variables_for_step"]
