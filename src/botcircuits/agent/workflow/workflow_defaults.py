"""Build-time defaults inference — keep workflow SOURCES at intent-only altitude.

The runtime understands a number of mechanical fields (the `deterministic` skip
flag, a listDecision's `decisionKey` / `collectInto` / `emit` / `nullOn`, and a
`flow.result` shape). An author shouldn't have to write these by hand — they
follow predictably from intent. This pass fills the sensible default for any the
author omitted, so a source file carries only *what* the workflow does, not the
plumbing.

Everything here is deterministic and conservative: it only ADDS a field when the
author left it absent (author always wins), and it infers nothing it can't infer
safely. Run it during `workflow build`, after condition indexing (so `choices`
and `variables` exist) and before segmentation.

What it fills:

  • `variables[].dataType` — already defaulted to "string" by the indexer; here
    we upgrade to "boolean"/"number" when a resolver makes the type obvious.

  • step `deterministic: true` — a single branch step whose every branch variable
    has a `resolver` is a pure read-and-decide; mark it so the engine skips its
    LLM call (S4). Only set when ALL its branch vars resolve.

  • listDecision defaults:
      - `decisionKey`  → "decision"
      - `collectInto`  → "decisions"
      - `emit`         → the item's variable names + the decision key
      - `nullOn`       → {} (author opts in explicitly; we don't guess nulling)

  • `flow.result` — if absent and a listDecision collects a list, default a
    template returning that collected list (plus a customer-ish id slot if one
    is present), so the engine renders the answer without an emit step (S2).
"""

from __future__ import annotations

from typing import Any


def _branch_var_names(step: dict) -> list[str]:
    out: list[str] = []
    for c in step.get("choices") or []:
        for e in c.get("expressionList") or []:
            v = e.get("variable")
            if isinstance(v, str) and v not in out:
                out.append(v)
    return out


def _resolver_dtype(resolver: dict) -> str | None:
    kind = resolver.get("kind")
    if kind in ("enum_check", "file_membership"):
        # emits one of two sentinel strings (true/false labels)
        return "string"
    if kind == "range":
        return "string"
    return None  # jsonpath: type unknown, leave as-is


def apply_defaults(flow: dict) -> dict:
    """Fill omitted mechanical fields in `flow`, in place. Returns a small
    summary for the CLI."""
    steps = flow.get("steps") or {}
    variables = flow.get("variables") or []
    by_name = {v.get("variableName"): v for v in variables
               if isinstance(v, dict)}

    filled = {"deterministic": 0, "listDecision_defaults": 0, "result": 0,
              "hoisted": 0}

    # 0. Hoist known step fields a generator may have nested under `settings`.
    #    The engine reads these at the STEP root; a generated workflow sometimes
    #    tucks them inside `settings` next to `action`. Move them up so the step
    #    works regardless of where they were authored. (`action` stays in
    #    settings.)
    _STEP_LEVEL = ("itemSource", "itemFacts", "itemVariables", "choices",
                   "conditions", "next", "decisionKey", "collectInto", "emit",
                   "nullOn", "deterministic")
    for step in steps.values():
        sc = step.get("settings") if isinstance(step, dict) else None
        if not isinstance(sc, dict):
            continue
        for key in _STEP_LEVEL:
            if key in sc and key not in step:
                step[key] = sc.pop(key)
                filled["hoisted"] += 1

    # 0b. Normalize `itemVariables` shape. The engine expects a LIST of
    #     {variableName, ...}; a generator sometimes emits a DICT keyed by name
    #     (conflating it with itemFacts.derive). Coerce dict → list so the engine
    #     doesn't crash with "'str' object has no attribute 'get'".
    for step in steps.values():
        if not isinstance(step, dict):
            continue
        iv = step.get("itemVariables")
        if isinstance(iv, dict):
            step["itemVariables"] = [
                {"variableName": name,
                 **(v if isinstance(v, dict) else {})}
                for name, v in iv.items()
                if isinstance(name, str)
            ]
            filled["hoisted"] += 1

    # 1. dataType upgrades from resolver kind (only when author left "string").
    for v in variables:
        if not isinstance(v, dict):
            continue
        r = v.get("resolver")
        if isinstance(r, dict) and (v.get("dataType") in (None, "", "string")):
            dt = _resolver_dtype(r)
            if dt:
                v["dataType"] = dt

    # 2. deterministic flag: a single branch step whose every branch var has a
    #    resolver. (A step's segment is the step itself here; segmentation later
    #    may merge it, but the per-step flag still drives the engine's skip.)
    for sid, step in steps.items():
        if not isinstance(step, dict):
            continue
        if step.get("type") != "agentAction":
            continue
        if "deterministic" in step:
            continue
        names = _branch_var_names(step)
        if names and all(
            isinstance(by_name.get(n), dict)
            and isinstance(by_name[n].get("resolver"), dict)
            for n in names
        ):
            step["deterministic"] = True
            filled["deterministic"] += 1

    # 3. listDecision defaults.
    for sid, step in steps.items():
        if not isinstance(step, dict) or step.get("type") != "listDecision":
            continue
        changed = False
        if "decisionKey" not in step:
            step["decisionKey"] = "decision"; changed = True
        if "collectInto" not in step:
            step["collectInto"] = "decisions"; changed = True
        if "emit" not in step:
            item_names = [iv.get("variableName")
                          for iv in step.get("itemVariables") or []
                          if isinstance(iv, dict)
                          and isinstance(iv.get("variableName"), str)]
            step["emit"] = item_names + [step["decisionKey"]]
            changed = True
        if "nullOn" not in step:
            step["nullOn"] = {}; changed = True
        if changed:
            filled["listDecision_defaults"] += 1

    # 4. flow.result default: render the collected decision list.
    if "result" not in flow:
        collected = None
        for step in steps.values():
            if isinstance(step, dict) and step.get("type") == "listDecision":
                ci = step.get("collectInto")
                if isinstance(ci, str):
                    collected = ci
                    break
        if collected:
            value: dict[str, Any] = {}
            # include a customer/id-ish slot if one exists, by convention
            for cand in ("customer_id", "customer", "id"):
                if cand in by_name:
                    value["customer"] = "{" + cand + "}"
                    break
            value[collected] = "{" + collected + "}"
            flow["result"] = {"kind": "template", "value": value}
            filled["result"] = 1

    return filled
