"""Engine-mode segment execution helpers.

The constant engine-mode system prompt and the synthetic `record_slots`
tool that together let `Agent._run_segment` run one branch-delimited
segment with a CACHE-STABLE prefix:

  - `ENGINE_SYSTEM_PROMPT` is fixed for the whole run. Nothing per-step
    or per-state goes here — the variable part (the segment's actions and
    branch-variable schema) rides the user message after this cached
    prefix, so every segment call hits the provider prompt cache.
  - `RECORD_SLOTS_TOOL` is the Tier-1 slot-capture surface (§3.2). The
    model calls it with the branch variables once the segment's actions
    are done; the engine reads those args instead of paying a separate
    normalization round-trip. We expose it as an ordinary tool rather
    than extend every provider with a structured-output / forced-tool
    parameter.

Both are built per segment only to the extent the branch-variable schema
changes; the prose prompt itself never does.
"""

from __future__ import annotations

import json
from typing import Any

from botcircuits.agent.tools.registry import LocalTool

#: Name the engine matches on to intercept slot capture. Underscore form
#: satisfies every provider's tool-name regex.
RECORD_SLOTS_TOOL = "record_slots"

#: S3 — name the engine matches to intercept a per-item FACT LIST for a
#: `listDecision` step. The model reports one fact-set per list element; the
#: engine decides each element deterministically. One call, N decisions.
RECORD_ITEM_LIST_TOOL = "record_item_list"

#: Static, cache-stable system prompt for engine-driven segment execution.
#: MUST NOT be mutated per step/state — that is the whole point.
ENGINE_SYSTEM_PROMPT = (
    "You are executing one segment of a deterministic workflow. A "
    "workflow engine — not you — decides what runs next; your job is only "
    "to perform the actions for THIS segment and report any requested "
    "values.\n\n"
    "Rules:\n"
    "  - Perform each action in the segment, in order, using your tools, "
    "skills, and MCP servers exactly as you would normally.\n"
    "  - If an action needs information only the user can provide AND that "
    "value was not already given (in the user's messages or the workflow's "
    "input arguments), call the 'human_feedback' tool with the question. "
    "The workflow pauses until the user replies. Never call 'human_feedback' "
    "to re-ask for a value you already have.\n"
    "  - When the segment lists branch variables, call the 'record_slots' "
    "tool ONCE after completing the actions, passing the values you "
    "actually observed. These decide the engine's next step. Omit any you "
    "do not genuinely have — never invent a value.\n"
    "  - Do not ask what to do next, do not summarize the workflow, and "
    "do not call any workflow tool. Just do this segment's work.\n"
    "  - SILENT MODE (S1). Produce NO assistant prose at all — no narration, "
    "no checklists, no restating tool output, no explanation, no final "
    "message. Communicate ONLY through tool calls and the 'record_slots' "
    "tool. The engine reports outcomes and renders the final answer from its "
    "own state; you never write the answer. Any assistant text you emit is "
    "wasted tokens the engine discards. Act, record the requested values, "
    "stop."
)

# flow.variables dataType → JSON-schema type. Mirrors the mapping in
# workflow/__init__.py; kept local so this module has no import cycle.
_JSON_TYPE_BY_DATATYPE = {"number": "number", "boolean": "boolean"}


def build_record_slots_tool(
    branch_variables: list[dict],
    sink: dict[str, Any],
) -> LocalTool:
    """A synthetic tool the model calls to report branch slots (Tier 1).

    `sink` is a dict the handler writes captured values into — the engine
    reads it back after the segment call. The schema advertises only the
    segment's branch variables, so the model isn't invited to fabricate
    values for irrelevant ones.
    """
    properties: dict[str, dict] = {}
    for v in branch_variables:
        name = v.get("variableName")
        if not isinstance(name, str) or not name:
            continue
        dtype = (v.get("dataType") or "string").lower()
        prop: dict = {"type": _JSON_TYPE_BY_DATATYPE.get(dtype, "string")}
        desc = v.get("description")
        if isinstance(desc, str) and desc:
            prop["description"] = desc
        properties[name] = prop

    def _handler(args: dict) -> dict:
        if isinstance(args, dict):
            for k, val in args.items():
                if val is not None and val != "":
                    sink[k] = val
        return {"recorded": sorted(sink.keys())}

    return LocalTool(
        name=RECORD_SLOTS_TOOL,
        description=(
            "Report the workflow branch variables you observed while "
            "performing this segment's actions. Call once, after the "
            "actions are done. Pass only values you genuinely have."
        ),
        input_schema={"type": "object", "properties": properties},
        handler=_handler,
    )


def _loads_lenient(text: str):
    """Parse a list the model serialized as a string. Tries JSON first, then a
    Python literal (single-quoted dict repr — a common provider quirk). Returns
    the parsed object or None."""
    import ast

    for parse in (json.loads, ast.literal_eval):
        try:
            return parse(text)
        except (ValueError, SyntaxError, TypeError):
            continue
    return None


def build_record_item_list_tool(
    item_variables: list[dict],
    sink: dict[str, Any],
) -> LocalTool:
    """S3 — a synthetic tool the model calls ONCE to report a list of per-item
    fact-sets for a `listDecision` step. Each element advertises only the
    step's `itemVariables` (facts, never decision words), so the model reports
    observations and the engine decides each item deterministically.

    `sink["items"]` receives the reported list.
    """
    item_props: dict[str, dict] = {}
    for v in item_variables:
        name = v.get("variableName")
        if not isinstance(name, str) or not name:
            continue
        dtype = (v.get("dataType") or "string").lower()
        prop: dict = {"type": _JSON_TYPE_BY_DATATYPE.get(dtype, "string")}
        desc = v.get("description")
        if isinstance(desc, str) and desc:
            prop["description"] = desc
        item_props[name] = prop

    def _handler(args: dict) -> dict:
        items = args.get("items") if isinstance(args, dict) else None
        # Some providers serialize the array as a JSON STRING rather than a
        # native array — coerce it so a stringified list still captures (this
        # was a silent "decisions:[]" failure mode otherwise).
        if isinstance(items, str):
            items = _loads_lenient(items)
        if isinstance(items, list):
            parsed = [it for it in items if isinstance(it, dict)]
            # Only overwrite a previously-captured non-empty list with another
            # non-empty one — a stray empty/garbled re-call must not wipe a good
            # capture from earlier in the same segment.
            if parsed or not sink.get("items"):
                sink["items"] = parsed
        return {"recorded_items": len(sink.get("items", []))}

    return LocalTool(
        name=RECORD_ITEM_LIST_TOOL,
        description=(
            "Report the per-item facts you observed, ONE object per list "
            "element in order. Report FACTS ONLY — never a decision/outcome; "
            "the engine decides each item from these facts. Call once, after "
            "running the per-item tools."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "object", "properties": item_props},
                }
            },
        },
        handler=_handler,
    )


def _schema_of(variables: list[dict]) -> list[dict]:
    return [
        {
            "variableName": v.get("variableName"),
            "dataType": v.get("dataType") or "string",
            "description": v.get("description") or "",
        }
        for v in variables
        if isinstance(v.get("variableName"), str)
    ]


def build_segment_user_message(
    actions: list[str],
    branch_variables: list[dict],
    system_notes: list[str],
    item_variables: list[dict] | None = None,
    data_variables: list[dict] | None = None,
) -> str:
    """The per-segment variable payload that rides AFTER the cached system
    prefix. Terse by design — the engine holds all the state."""
    parts: list[str] = []
    if system_notes:
        parts.append("Engine notes:\n" + "\n".join(f"- {n}" for n in system_notes))
    if actions:
        numbered = "\n".join(f"{i}. {a}" for i, a in enumerate(actions, 1))
        parts.append(f"Perform these action(s) in order:\n{numbered}")
    else:
        parts.append("This segment has no action to perform.")
    if branch_variables:
        parts.append(
            "After completing the action(s), call 'record_slots' with these "
            "branch variables (omit any you don't have):\n"
            + json.dumps(_schema_of(branch_variables), ensure_ascii=False)
        )
    if data_variables:
        # Carried key-value memory: declared non-branch variables. Whatever a
        # segment produces here is persisted into slots and handed to later
        # (stateless) segments, so a "scrape" step can pass its data to a
        # "save" step. Reported alongside branch slots, never used to branch.
        parts.append(
            "Also record (in the SAME 'record_slots' call / `slots` object) "
            "any of these DATA variables you produced, so later steps can use "
            "them — store the full value (e.g. a JSON list), omit ones you "
            "didn't produce:\n"
            + json.dumps(_schema_of(data_variables), ensure_ascii=False)
        )
    if item_variables:
        # S3 — listDecision: the model reports a LIST of per-item FACTS; the
        # engine decides each. Facts only, never an outcome word.
        parts.append(
            "After running the per-item tools, call 'record_item_list' ONCE "
            "with `items` = one object per list element (in order), each "
            "carrying ONLY these fact fields (never a decision/outcome — the "
            "engine decides):\n"
            + json.dumps(_schema_of(item_variables), ensure_ascii=False)
        )
    return "\n\n".join(parts)
