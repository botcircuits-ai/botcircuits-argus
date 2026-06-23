"""Convert natural-language conditions in a workflow's choice steps into
rule-engine expressions, and derive the set of variables those expressions
reference.

Uses the agent's pluggable `LLMProvider` so the same provider/model the
main Agent uses also powers indexing. Output is `choices[].expressionList[]`
entries the choice handler already understands (see `engine/handlers/choice.py`).
"""

from __future__ import annotations

import json
import re
from typing import Any

from botcircuits.providers.base import LLMProvider
from botcircuits.types import Message


# Mirrors the operators the choice handler supports
# (engine/handlers/choice.py::_evaluate_operator).
SUPPORTED_OPERATORS = [
    "is", "is not",
    "greater than", "greater than or equal",
    "less than", "less than or equal",
    "contains", "not contains",
    "starts with", "ends with",
    "is empty", "is not empty",
]


def _dedupe_conditions(conditions: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for c in conditions:
        key = f"{c.get('next') or ''}::{(c.get('condition') or '').strip()}"
        if key in seen:
            continue
        seen.add(key)
        result.append(c)
    return result


#: Step types whose natural-language `conditions` the builder compiles into
#: rule-engine `choices`. `listDecision` is included: its conditions are the
#: per-item decision rules (evaluated against each item's facts).
_BRANCHABLE_TYPES = ("agentAction", "question", "systemAction", "listDecision")


def _collect_condition_steps(flow: dict) -> list[dict]:
    """Find every branchable step that carries natural-language `conditions`
    at the step root. Filters out empty entries and dedupes per step.

    Branching lives on the step (via `step.conditions` → `step.choices`) and is
    evaluated on re-entry, after variables are filled (for a `question` step,
    after the user's reply; for a `listDecision` step, against each item).
    """
    steps = flow.get("steps") or {}
    entries: list[dict] = []
    for step_id, step in steps.items():
        if not isinstance(step, dict):
            continue
        if step.get("type") not in _BRANCHABLE_TYPES:
            continue
        raw = step.get("conditions")
        if not isinstance(raw, list):
            continue

        filtered = [
            c for c in raw
            if isinstance(c, dict)
            and isinstance(c.get("condition"), str)
            and c["condition"].strip() != ""
        ]
        deduped = _dedupe_conditions(filtered)
        step["conditions"] = deduped
        if deduped:
            entries.append({"stepId": step_id, "step": step})
    return entries


def _build_step_summary(flow: dict) -> str:
    """One line per step so the LLM has the surrounding context when
    choosing variable names."""
    steps = flow.get("steps") or {}
    lines: list[str] = []
    for step_id, step in steps.items():
        if not isinstance(step, dict):
            continue
        sc = step.get("settings") or {}
        parts = [f"id={step_id}", f"type={step.get('type', '')}"]
        if sc.get("name"):
            parts.append(f"name={sc['name']}")
        if sc.get("action"):
            parts.append(f"action={sc['action']}")
        if sc.get("intentPrompt"):
            parts.append(f"intentPrompt={sc['intentPrompt']}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def _existing_variable_lines(flow: dict, condition_entries: list[dict]) -> list[str]:
    """Author-declared facts the indexer should REUSE rather than reinvent: the
    flow-level `variables` (often resolver-backed) and each listDecision step's
    `itemVariables` (the per-item facts its conditions test). Reusing these names
    is what lets a resolver-backed variable stay deterministic — an invented
    synonym would have no resolver and force an LLM round-trip at runtime."""
    lines: list[str] = []
    for v in flow.get("variables") or []:
        if isinstance(v, dict) and isinstance(v.get("variableName"), str):
            desc = v.get("description") or ""
            lines.append(f'  - {v["variableName"]}: {desc}')
    seen_item: set[str] = set()
    for entry in condition_entries:
        for iv in entry["step"].get("itemVariables") or []:
            name = iv.get("variableName") if isinstance(iv, dict) else None
            if isinstance(name, str) and name not in seen_item:
                seen_item.add(name)
                lines.append(f'  - {name}: {iv.get("description") or ""} (per-item)')
    return lines


def _build_prompt(flow: dict, condition_entries: list[dict]) -> str:
    step_summary = _build_step_summary(flow)
    existing_vars = _existing_variable_lines(flow, condition_entries)

    condition_lines: list[str] = []
    for entry in condition_entries:
        step_id = entry["stepId"]
        step = entry["step"]
        sc = step.get("settings") or {}
        action = sc.get("action") or sc.get("name") or step.get("type") or step_id
        condition_lines.append(f"step_id={step_id} (action: {action}):")
        for idx, c in enumerate(step.get("conditions") or []):
            condition_lines.append(
                f'  - idx={idx} condition="{c.get("condition", "")}" '
                f"next={c.get('next') or ''}"
            )

    operators = ", ".join(f'"{o}"' for o in SUPPORTED_OPERATORS)

    return "\n".join([
        "You convert natural-language branching conditions in a state "
        "machine into expressions evaluable by a rule engine.",
        "",
        f"Supported operators (use ONLY these): {operators}",
        "",
        "Expression syntax: `<variable_name> <operator> <value>`",
        "  - variable_name: snake_case identifier representing a fact "
        "captured earlier in the workflow.",
        "  - String literal values must be wrapped in single quotes, e.g. "
        "`readme_found is 'yes'`.",
        "  - Use `is empty` / `is not empty` with no value, e.g. "
        "`readme_found is not empty`.",
        "",
        "For each condition you must:",
        "  1. Choose a stable variable_name that represents the underlying "
        "fact (reuse the SAME variable_name across conditions that test "
        "the same fact).",
        "  2. Produce an expression using one of the supported operators.",
        "  3. Define each unique variable once with a clear description and "
        'dataType ("string", "boolean", "number").',
        "",
        "IMPORTANT — REUSE existing variables. The workflow already declares the "
        "facts below. When a condition tests one of them, your expression MUST "
        "use that exact variable_name and its stated value vocabulary (do NOT "
        "invent a synonym). Only introduce a new variable for a fact not listed "
        "here.",
        ("Existing facts:\n" + "\n".join(existing_vars)) if existing_vars
        else "Existing facts: (none)",
        "",
        "Workflow overview:",
        step_summary,
        "",
        "Conditions to convert:",
        "\n".join(condition_lines),
        "",
        "Respond with a JSON object (no commentary, no markdown fence) of "
        "the form:",
        "{",
        '  "expressions": [',
        '    { "step_id": "<step id>", "idx": <index>, '
        '"expCondition": "<expression>" }',
        "  ],",
        '  "variables": [',
        '    { "variableName": "<snake_case>", '
        '"dataType": "string|boolean|number", '
        '"description": "<short description>" }',
        "  ]",
        "}",
    ])


# Operators ordered LONGEST-first so the alternation is greedy on the operator,
# never on the value: without this, `stock is less than qty` matches the shorter
# `is` first and captures `less than qty` as the (uncomparable) value. An
# optional `is `/`is not ` prefix is allowed before the comparison/text operators
# because the indexer (and authors) naturally phrase them as `X is less than Y`
# or `X is greater than 5000`; we normalize that back to the bare operator the
# choice handler implements.
_PREFIXABLE_OPS = (
    "greater than or equal", "greater than",
    "less than or equal", "less than",
    "not contains", "contains",
    "starts with", "ends with",
)
_BARE_OPS = ("is not empty", "is empty", "is not", "is")
_OP_ALTERNATION = "|".join(
    re.escape(o)
    for o in sorted(_PREFIXABLE_OPS + _BARE_OPS, key=len, reverse=True)
)
_EXPRESSION_RE = re.compile(
    r"^\s*(?P<var>[A-Za-z_][A-Za-z0-9_]*)\s+"
    r"(?:is\s+not\s+|is\s+)?"           # optional `is`/`is not` linking verb
    rf"(?P<op>{_OP_ALTERNATION})"
    r"(?:\s+(?P<val>.+))?\s*$"
)

#: A bare snake_case RHS on an ORDERED comparison is a reference to another fact
#: (e.g. `stock less than qty`), not a literal. The choice handler resolves
#: `{slot}` placeholders, so we wrap it. We do this ONLY for ordered operators:
#: for `is`/`contains`/etc. an unquoted word is a string literal (`status is
#: blocked`), and wrapping it would turn a real literal into a dangling slot ref.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ORDERED_OPS = frozenset((
    "greater than", "greater than or equal", "less than", "less than or equal",
))


def _parse_expression(exp: str) -> dict | None:
    """Parse `<variable> [is|is not] <operator> <value>` into the
    `{variable, operator, value}` shape the local choice handler reads.
    Returns None if the expression doesn't match.
    """
    m = _EXPRESSION_RE.match(exp)
    if not m:
        return None
    op = m.group("op")
    # An `is not <comparison>` prefix means negate — but the only negatable
    # comparison we model is `is not` itself (handled as a bare op); for the
    # ordered/text operators the `is`/`is not` is just a linking verb, so we
    # keep the bare operator. (Authors don't write `is not less than`.)
    val_raw = (m.group("val") or "").strip()
    if op in ("is empty", "is not empty"):
        value: Any = ""
    else:
        # Strip a single pair of surrounding single OR double quotes.
        if (len(val_raw) >= 2
                and val_raw[0] == val_raw[-1]
                and val_raw[0] in ("'", '"')):
            value = val_raw[1:-1]
        elif val_raw.lower() in ("true", "false"):
            value = val_raw.lower() == "true"
        else:
            try:
                value = int(val_raw)
            except ValueError:
                try:
                    value = float(val_raw)
                except ValueError:
                    # A bare identifier on an ordered comparison references
                    # another fact -> resolve via its slot. Elsewhere it's a
                    # string literal.
                    if op in _ORDERED_OPS and _IDENT_RE.match(val_raw):
                        value = f"{{{val_raw}}}"
                    else:
                        value = val_raw
    return {"variable": m.group("var"), "operator": op, "value": value}


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _repair_json(text: str) -> str:
    """Light, safe repairs for the JSON slips models make most often: a missing
    comma between adjacent array/object members, and a trailing comma before a
    closer. Only edits whitespace between two structural tokens, so it can't
    corrupt string contents."""
    # comma between a value-closer and the next value-opener across a newline
    text = re.sub(r'([}\]"0-9eltn])\s*\n(\s*)([{\[\"])', r"\1,\n\2\3", text)
    # drop trailing commas before a closer
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return text


def _extract_json(raw: str) -> dict:
    """Strip code fences / leading prose and parse the first JSON object.
    Falls back to a light comma-repair for the common model JSON slips before
    giving up."""
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
        for variant in (cand, _repair_json(cand)):
            try:
                return json.loads(variant)
            except json.JSONDecodeError:
                continue
    # Re-raise the original error on the primary text for a clear message.
    return json.loads(text)


async def _ask_llm_for_json(provider: LLMProvider, prompt: str) -> str:
    """Call the provider with a strict-JSON system message and return the
    raw assistant text. We do NOT use streaming or tools — this is a single
    deterministic call."""
    system = (
        "You produce strict JSON that matches the requested schema. "
        "Do not include commentary, prose, or markdown code fences."
    )
    messages = [Message(role="user", blocks=[{"type": "text", "text": prompt}])]
    response = await provider.complete(
        system=system,
        messages=messages,
        tools=[],
        hosted_mcp=[],
        skills=[],
        max_tokens=8192,
    )
    return response.text


async def generate_expressions_and_variables(
    flow: dict,
    provider: LLMProvider,
) -> dict:
    """Mutate `flow` in place:

      - For each choice step with NL `conditions`, populate
        `expCondition` on each condition and build a `choices` array the
        runtime engine understands.
      - Write the aggregated variable catalogue to
        `flow['variables']`.

    Returns a small summary dict for the CLI to log.
    """
    condition_entries = _collect_condition_steps(flow)
    if not condition_entries:
        flow["variables"] = flow.get("variables") or []
        return {"steps_processed": 0, "expressions": 0, "variables": 0}

    prompt = _build_prompt(flow, condition_entries)
    raw = await _ask_llm_for_json(provider, prompt)
    try:
        parsed = _extract_json(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"LLM did not return valid JSON for condition indexing: {e}\n"
            f"Raw response: {raw[:500]}"
        ) from e

    expressions = parsed.get("expressions") if isinstance(parsed, dict) else None
    variables = parsed.get("variables") if isinstance(parsed, dict) else None
    if not isinstance(expressions, list):
        expressions = []
    if not isinstance(variables, list):
        variables = []

    exp_by_key: dict[str, str] = {}
    for e in expressions:
        if not isinstance(e, dict):
            continue
        sid = e.get("step_id")
        idx = e.get("idx")
        exp = e.get("expCondition")
        if sid is None or idx is None or not isinstance(exp, str):
            continue
        exp_by_key[f"{sid}::{idx}"] = exp.strip()

    missing: list[str] = []
    expression_count = 0
    for entry in condition_entries:
        step_id = entry["stepId"]
        step = entry["step"]
        # Replace, don't append — re-indexing the same file must be
        # idempotent. If the author wants hand-written `choices` to
        # survive, they shouldn't use NL `conditions` on that step.
        new_choices: list[dict] = []

        for idx, c in enumerate(step.get("conditions") or []):
            exp = exp_by_key.get(f"{step_id}::{idx}")
            if not exp:
                missing.append(f'{step_id}[{idx}] "{c.get("condition", "")}"')
                continue
            c["expCondition"] = exp
            parsed_exp = _parse_expression(exp)
            if parsed_exp is None:
                missing.append(
                    f'{step_id}[{idx}] unparseable expression {exp!r}'
                )
                continue
            new_choices.append({
                "operator": "AND",
                "expressionList": [parsed_exp],
                "next": c.get("next"),
            })
            expression_count += 1

        step["choices"] = new_choices

    if missing:
        raise RuntimeError(
            "LLM did not produce usable expressions for: "
            + ", ".join(missing)
        )

    # Aggregate variables, preserving first-seen order, dropping duplicates.
    # The author's existing `flow.variables` are seeded FIRST so hand-authored
    # variables always survive an index — including those referenced only by
    # hand-written `choices` (which the LLM indexer never sees and so never
    # re-declares). Without this, re-indexing silently drops them and the
    # runtime's Layer A/B normalization has no schema to coerce their slots
    # against, so those branches mis-fire. The author wins on a name collision:
    # they declared the dataType deliberately, and the indexer's guess for a
    # same-named variable shouldn't override it.
    seen_names: set[str] = set()
    aggregated: list[dict] = []
    for v in list(flow.get("variables") or []) + list(variables):
        if not isinstance(v, dict):
            continue
        name = v.get("variableName")
        if not isinstance(name, str) or not name or name in seen_names:
            continue
        seen_names.add(name)
        # Preserve the author's variable verbatim (so a hand-declared `resolver`,
        # `allowed`, etc. survive a rebuild), only filling defaults for the two
        # mechanical fields. Spread first so canonical keys take precedence.
        merged = {**v,
                  "variableName": name,
                  "dataType": v.get("dataType") or "string",
                  "description": v.get("description") or ""}
        aggregated.append(merged)
    flow["variables"] = aggregated

    return {
        "steps_processed": len(condition_entries),
        "expressions": expression_count,
        "variables": len(aggregated),
    }


__all__ = [
    "SUPPORTED_OPERATORS",
    "generate_expressions_and_variables",
]
