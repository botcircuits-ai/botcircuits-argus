"""Condition evaluation for agentAction branching.

There is no `choice` step type. Conditions are authored on `agentAction`
steps and evaluated on re-entry (after the LLM has had a chance to fill
variables). The executor calls `evaluate_choices` with the step's
`choices` list and the current session context, and uses the returned
next-step id to override the static `next`.

When no choice matches, the evaluator returns the supplied fallback
rather than calling an LLM — the engine itself is deterministic; the
surrounding agent loop is where any LLM-driven recovery happens.
"""

from __future__ import annotations

from botcircuits.agent.workflow.engine.utils import (
    coerce_for_compare,
    fill_text_with_slots,
    to_number,
)

_BOOL_STR = {"true": True, "false": False, "yes": True, "no": False}


def _equal_coerced(a, b) -> bool:
    """Equality for the `is`/`is not` operators that tolerates the type
    mismatches a workflow author (or generator) commonly produces: a slot
    holding the boolean `False` compared against the string ``"false"``, or a
    number against its string form. Falls back to case-insensitive string
    comparison.

    Without this, `found is 'false'` (string value) against a boolean `found`
    slot is never equal, so the branch silently never fires.
    """
    if a == b:
        return True
    # Boolean <-> string ("true"/"false"/"yes"/"no").
    for x, y in ((a, b), (b, a)):
        if isinstance(x, bool) and isinstance(y, str):
            return _BOOL_STR.get(y.strip().lower()) is x
    # Number <-> numeric string.
    na, nb = to_number(a), to_number(b)
    if na is not None and nb is not None:
        return na == nb
    # Case-insensitive string compare.
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return False


def evaluate_choices(
    choices: list[dict],
    message: dict,
    default_next: str | None,
) -> str | None:
    """Walk `choices` in order; return the `next` of the first match, or
    `default_next` if none match (or `None` to end the workflow).
    """
    for choice in choices or []:
        if _evaluate_choice(choice, message):
            return choice.get("next")
    return default_next


def _evaluate_choice(choice: dict, message: dict) -> bool:
    operator = choice.get("operator")
    expressions = choice.get("expressionList", [])
    if operator == "OR":
        return any(_evaluate_condition(c, message) for c in expressions)
    if operator == "AND":
        return all(_evaluate_condition(c, message) for c in expressions)
    return False


def _evaluate_condition(condition: dict, message: dict) -> bool:
    if "variable" not in condition:
        return False

    session_context = message["data"]["sessionContext"]
    variable = condition["variable"]
    if variable == "{sys_input_text}":
        variable_value: object = message.get("inputText", "")
    elif variable == "{sys_channel}":
        variable_value = message.get("channel", "")
    else:
        variable_value = (session_context.get("slots") or {}).get(variable, "")

    variable_value = coerce_for_compare(variable_value)
    return _evaluate_operator(condition, variable_value, session_context)


def _evaluate_operator(condition: dict, variable_value, session_context: dict) -> bool:
    operator = condition.get("operator")
    raw_value = condition.get("value", "")
    # Only string `value`s carry slot placeholders; typed values from the
    # indexer (numbers, booleans) flow through unchanged.
    check_value = (
        fill_text_with_slots(raw_value, session_context)
        if isinstance(raw_value, str) else raw_value
    )
    # Ordered comparisons coerce *both* sides to a number so an unfilled slot
    # (`None`), a string slot (`"640"`), or an indexer-typed value all compare
    # safely. If either side isn't numeric the condition doesn't match and the
    # engine falls through to the default branch — never raising TypeError.
    if operator in ("greater than", "greater than or equal",
                    "less than", "less than or equal"):
        a, b = to_number(variable_value), to_number(check_value)
        if a is None or b is None:
            return False
        if operator == "greater than":
            return a > b
        if operator == "greater than or equal":
            return a >= b
        if operator == "less than":
            return a < b
        return a <= b

    if operator == "is":
        return _equal_coerced(check_value, variable_value)
    if operator == "is not":
        return not _equal_coerced(check_value, variable_value)
    if operator == "contains":
        return isinstance(variable_value, str) and check_value in variable_value
    if operator == "not contains":
        return isinstance(variable_value, str) and check_value not in variable_value
    if operator == "starts with":
        return isinstance(variable_value, str) and variable_value.startswith(check_value)
    if operator == "ends with":
        return isinstance(variable_value, str) and variable_value.endswith(check_value)
    if operator == "is empty":
        return variable_value == ""
    if operator == "is not empty":
        return variable_value != ""
    return False
