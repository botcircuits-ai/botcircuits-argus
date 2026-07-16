"""NL → workflow generator — author a workflow SOURCE from plain instructions.

This is the front of the authoring pipeline: given a natural-language
description of a process (e.g. a use case's `Instructions.md`), it produces a
*draft, intent-only* workflow source file — the same shape an advanced user
would hand-write — which `workflow build` then compiles and optimizes.

    instructions (NL text)  ──►  generate_workflow(...)  ──►  <name>.json
                                  (one LLM call)              (intent-only source)
                                                              then `workflow build`

The generator deliberately emits ONLY intent (steps, NL `conditions`, variable
names + descriptions, optional `resolver` / listDecision `itemSource`+`itemFacts`
when the instructions describe a deterministic file/script lookup). It does NOT
emit compiled mechanics (`choices`/`expressionList`, `dataType`, `segments`,
`flow.result`, the `deterministic` flag) — `workflow build` generates those.

It is best-effort: the produced draft is meant to be reviewed and is then run
through the normal build, which validates it. A malformed LLM response raises so
the caller can surface it.
"""

from __future__ import annotations

import json

from botcircuits.providers.base import LLMProvider
from botcircuits.types import Message

from .condition_processor import _extract_json

_SYSTEM = (
    "You are a workflow author. You convert a natural-language description of a "
    "repeatable process into a BotCircuits workflow SOURCE file (JSON). Emit "
    "ONLY intent — never compiled mechanics. Return strict JSON, no prose, no "
    "markdown fences."
)


def _prompt(instructions: str, name: str, resources: str = "") -> str:
    return "\n".join([
        f"Produce a workflow named '{name}' from the process described below.",
        "",
        (("WORKSPACE RESOURCES the workflow can read/run (wire your resolvers, "
          "itemSource, and itemFacts to these EXACT paths):\n" + resources + "\n")
         if resources.strip() else ""),
        "OUTPUT a JSON object of this shape (intent only):",
        "{",
        '  "name": "' + name + '",',
        '  "description": "<one line: what it does and when to run it>",',
        '  "flow": {',
        '    "start": "<first step id>",',
        '    "variables": [',
        '      { "variableName": "<snake_case>", "description": "<plain '
        'language; state the exact value words if it is a fixed set>", '
        '"input": "<true ONLY for values the USER must supply to start '
        '(the topic, the id, …); omit for variables the workflow produces '
        'or reads from files>" }',
        "    ],",
        '    "steps": {',
        '      "<step_id>": {',
        '        "type": "agentAction | question | listDecision",',
        '        "settings": { "action": "<plain-language instruction>" },',
        '        "conditions": [ { "condition": "<plain-language branch '
        'rule>", "next": "<step id or outcome>" } ],',
        '        "next": "<default next step or outcome>"',
        "      }",
        "    }",
        "  }",
        "}",
        "",
        "RULES:",
        "- Write branch logic ONLY as natural-language `conditions` — a list of "
        "{condition: \"<plain English>\", next: \"<outcome word>\"}. The builder "
        "compiles them into rule expressions. NEVER write `choices`, "
        "`expressionList`, `expCondition`, or operators/values yourself: a "
        "hand-written comparison like {operator:\"is\", value:\"less than qty\"} "
        "silently never matches and the branch is dead. Just say "
        "{condition: \"there is not enough stock\", next: \"backorder\"}.",
        "- On a `listDecision`, each condition's `next` (and the step's default "
        "`next`) is the DECISION WORD for the item (e.g. fulfill / backorder / "
        "review / reject) — NOT the name of another step to go to.",
        "- Do NOT write dataType, segments, flow.result, or a `deterministic` "
        "flag — the builder fills those.",
        "- Keep step actions terse and imperative.",
        "",
        "DESIGN PRINCIPLE — PREFER DETERMINISM, NEVER ASK THE USER FOR DATA THAT "
        "EXISTS. The input data the process needs (the order, the applicant, "
        "inventory, blocklists, prices) lives in workspace FILES and SCRIPTS — "
        "the instructions describe the POLICY, but you must wire it to read "
        "those files/run those scripts, not converse:",
        "- Do NOT use a `question` step to gather, validate, or 'ask for' input "
        "that is in a file. A `question` PAUSES the whole workflow waiting on a "
        "human and is almost always wrong here. Use `question` ONLY when the "
        "instructions explicitly require asking a person something no file holds.",
        "- Mark variables the USER must supply to start (and only those) with "
        '`"input": true`. The ENGINE collects them deterministically before the '
        "first step: it extracts values already present in the conversation and "
        "asks ONE question (built from your descriptions) for the rest — so "
        "never author a step that asks for them.",
        "- When a fact is a deterministic lookup (a value in a file, membership "
        "in a list, a number in a range), give its variable a `resolver` so the "
        "ENGINE computes it with NO AI call:",
        '    { "variableName": "header_status", "description": "...",',
        '      "resolver": { "kind": "enum_check", "source": {"file": '
        '"data/order.json", "path": "region"}, "allowed": ["US","EU"], '
        '"true": "valid", "false": "invalid" } }',
        "    resolver kinds: jsonpath {file,path}; enum_check {source,allowed,"
        "true,false}; file_membership {file,value_source,true,false,"
        "ignore_comments}; range {source,min,max,true,false}.",
        "- When the process decides an outcome for EVERY item in a list, use a "
        "`listDecision` step with `itemSource` {file, path} (the list) and "
        "`itemVariables` (the per-item facts its `conditions` test). If each "
        "item's facts come from running a script, add `itemFacts` so the ENGINE "
        "runs it per item with NO AI:",
        '    "itemFacts": { "kind": "exec", "command": ["python3", '
        '"bin/price.py", "{sku}", "{qty}"], "parse": "json", "derive": { '
        '"sku": {"from_item":"sku"}, "in_stock": {"from_output":"found"}, '
        '"total": {"from_output":"line_total","default":0}, "enough": '
        '{"ge":["output.stock","item.qty"]} } }',
        "    derive rules: from_item:<k>; from_output:<k>[,default]; literal:<v>;"
        " ge:[<ref>,<ref>] where a ref is 'item.x' / 'output.y' / a literal.",
        "- A listDecision may set `nullOn` {field:[decisionLabels]} to blank a "
        "field for certain outcomes (e.g. a rejected item has no total: "
        '{"line_total": ["reject"]}).',
        "",
        "So: read header/screening facts via resolvers, process line items via a "
        "listDecision with itemFacts — aim for a workflow that runs WITHOUT "
        "pausing and WITHOUT the model deciding outcomes itself.",
        "",
        "Process description:",
        instructions,
    ])


_MAX_ATTEMPTS = 3


async def generate_workflow(
    instructions: str,
    name: str,
    provider: LLMProvider,
    resources: str = "",
    *,
    validate_loop: int = 0,
    base_dir=None,
    dry_run=None,
) -> dict:
    """Generate an intent-only workflow source dict from NL `instructions`.

    `resources` (optional) is a manifest of workspace files/scripts the workflow
    may read or run — wiring resolvers/itemSource/itemFacts to these paths keeps
    the generated workflow deterministic instead of pausing to ask the user.

    Validate→repair loop. Beyond the JSON-parse retries (`_MAX_ATTEMPTS`), set
    `validate_loop > 0` to additionally CHECK each produced draft and, if it has
    fixable problems, feed them back to the model to repair — up to that many
    rounds. Checks:
      * static (`workflow_validator.static_issues`, using `base_dir` to verify
        file paths / item-list shapes), always run when validate_loop > 0;
      * dry-run (optional `dry_run(doc) -> list[str]` callback supplied by the
        caller — it builds + runs the draft on a sample input and returns any
        runtime problems, e.g. "produced no decisions"). The agent generator
        stays engine-agnostic; the eval/CLI provides the dry-run.
    The loop stops as soon as a draft has zero issues, and returns the best
    (fewest-issues) draft if none come back clean.

    Returns the parsed workflow JSON. Raises RuntimeError if no attempt yields
    valid JSON of the expected shape."""
    from .workflow_validator import static_issues

    prompt = _prompt(instructions, name, resources)
    last_err = ""
    last_raw = ""
    best_doc: dict | None = None
    best_issues: list[str] | None = None
    # JSON-parse retries get _MAX_ATTEMPTS; the validate loop adds repair rounds.
    max_rounds = _MAX_ATTEMPTS + max(0, validate_loop)
    repair_feedback = ""

    for attempt in range(max_rounds):
        messages = [Message(role="user", blocks=[{"type": "text", "text": prompt}])]
        if attempt > 0 and repair_feedback:
            messages.append(Message(role="user", blocks=[{"type": "text", "text":
                repair_feedback}]))
        resp = await provider.complete(
            system=_SYSTEM, messages=messages, tools=[], hosted_mcp=[],
            skills=[], max_tokens=8192,
        )
        last_raw = resp.text
        try:
            doc = _extract_json(resp.text)
        except (json.JSONDecodeError, ValueError) as e:
            last_err = str(e)
            repair_feedback = (
                f"Your previous response was not valid JSON ({e}). Return ONLY "
                "the corrected, strict JSON object — no prose, no markdown fences.")
            continue
        if not (isinstance(doc, dict) and isinstance(doc.get("flow"), dict)):
            last_err = "output missing a `flow` object"
            repair_feedback = (
                "Your previous response had no top-level `flow` object. Return "
                "ONLY the corrected, strict JSON workflow object.")
            continue

        doc["name"] = name  # force the requested name (file + tool name)

        # Validate (only when asked). Static first; then the caller's dry-run.
        if validate_loop > 0:
            issues = static_issues(doc, base_dir=base_dir)
            if not issues and dry_run is not None:
                try:
                    issues = list(await dry_run(doc) or [])
                except Exception as e:  # a dry-run crash is itself an issue
                    issues = [f"Dry run failed: {type(e).__name__}: {e}"]
            if not issues:
                return doc  # clean draft
            # Track the best-so-far in case we never get a clean one.
            if best_issues is None or len(issues) < len(best_issues):
                best_doc, best_issues = doc, issues
            last_err = "; ".join(issues)
            repair_feedback = (
                "Your previous workflow draft has these problems — fix ALL of "
                "them and return ONLY the corrected, strict JSON workflow "
                "object:\n- " + "\n- ".join(issues))
            continue

        # No validation requested: keep the original light description gate.
        desc = doc.get("description")
        if not isinstance(desc, str) or not desc.strip():
            last_err = "output missing a `description`"
            repair_feedback = (
                "Add a non-empty `description`. Return ONLY the corrected JSON.")
            continue
        return doc

    if best_doc is not None:
        # Validation never fully passed; return the closest draft (the caller
        # may still build it — the build's defaults/optimizers tolerate a lot).
        return best_doc
    raise RuntimeError(
        f"generator did not return valid workflow JSON after "
        f"{max_rounds} attempts: {last_err}\nLast raw: {last_raw[:500]}")
