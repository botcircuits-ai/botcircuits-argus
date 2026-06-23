"""S4-exec — deterministic per-item fact gathering (no LLM).

The listDecision primitive (S3) already makes the per-item DECISION deterministic
(`evaluate_choices` per item). But the per-item FACTS were still gathered by the
model (read the order, run the pricer, report sku/stock/line_total) — and a model
occasionally misreports a fact (wrong SKU, garbled list), which is the residual
accuracy gap.

This module lets the ENGINE gather those facts in code: read the item list from a
file, run a deterministic command (e.g. the pricer script) per item, parse its
output, and derive each fact field with small declarative rules. When a
listDecision step carries both `itemSource` and `itemFacts`, the engine skips the
LLM entirely for that segment — the whole order is processed with zero LLM calls,
fully deterministically.

Step fields consumed (all optional; absent → fall back to the model/Tier-1 path):

    "itemSource": {"file": "data/current_order.json", "path": "items"}
        Where the list of input items comes from (each an object, e.g.
        {"sku": ..., "qty": ...}).

    "itemFacts": {
        "kind": "exec",
        "command": ["python3", "bin/price.py", "{sku}", "{qty}"],
        "parse": "json",                       # parse stdout as JSON
        "derive": {
            "sku":              {"from_item": "sku"},
            "sku_found":        {"from_output": "found"},
            "line_total":       {"from_output": "line_total", "default": 0},
            "stock_sufficient": {"ge": ["output.stock", "item.qty"]}
        }
    }

`derive` value rules (deterministic, tiny by design):
    {"from_item": "k"}             — the item's field k
    {"from_output": "k", "default": d} — the parsed command output's field k
    {"literal": v}                 — a constant
    {"ge": [a, b]}                 — a >= b, where a/b are "item.x" / "output.y"
                                     refs or literals (numeric compare)

Execution is sandboxed to the run cwd and best-effort: a command failure or
parse error yields `found=false`-style facts for that item (engine still decides
deterministically), and any structural problem returns None so the caller falls
back to the model path. Never raises into the run.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

_EXEC_TIMEOUT_S = 30


def _read_items(spec: dict, base: Path) -> list[dict] | None:
    src = spec.get("itemSource")
    if not isinstance(src, dict):
        return None
    f = src.get("file")
    if not isinstance(f, str):
        return None
    try:
        raw = (base / f).read_text()
    except Exception:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        # Not JSON: treat as a plain one-item-per-line text file (the SKILL's
        # documented `path: ""` source — e.g. a tracking-ids.txt of one id per
        # line). Each non-empty line becomes an item dict carrying the line under
        # `value`, so an itemFacts `command` can interpolate `{value}` and
        # `derive` can read it via `{"from_item": "value"}`. Without this a text
        # source returned None and the listDecision silently fell back to the
        # non-deterministic model path.
        return [{"value": ln.strip()} for ln in raw.splitlines() if ln.strip()]
    path = src.get("path")
    cur = data
    if path:
        for part in str(path).split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                return None
    return cur if isinstance(cur, list) else None


def _fmt(token: str, item: dict) -> str:
    """Interpolate `{field}` from the item into a command token."""
    out = token
    for k, v in item.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _ref(ref: Any, item: dict, output: dict) -> Any:
    """Resolve an "item.x" / "output.y" reference, or pass a literal through."""
    if isinstance(ref, str) and ref.startswith("item."):
        return item.get(ref[len("item."):])
    if isinstance(ref, str) and ref.startswith("output."):
        return output.get(ref[len("output."):])
    return ref


def _derive_field(rule: dict, item: dict, output: dict) -> Any:
    if "from_item" in rule:
        return item.get(rule["from_item"], rule.get("default"))
    if "from_output" in rule:
        val = output.get(rule["from_output"])
        return val if val is not None else rule.get("default")
    if "literal" in rule:
        return rule["literal"]
    if "ge" in rule:
        a, b = rule["ge"]
        try:
            return float(_ref(a, item, output)) >= float(_ref(b, item, output))
        except (TypeError, ValueError):
            return False
    return None


def _project_item_fields(step: dict, items: list) -> list[dict]:
    """One fact dict per item carrying the step's declared `itemVariables`
    fields, read straight from the item. Used when a listDecision step sources
    items from a file but needs no computed (exec) facts. Falls back to the
    whole item when no `itemVariables` are declared."""
    names = [
        v.get("variableName")
        for v in (step.get("itemVariables") or [])
        if isinstance(v, dict) and isinstance(v.get("variableName"), str)
    ]
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        out.append({n: item.get(n) for n in names} if names else dict(item))
    return out


def resolve_item_facts(
    step: dict,
    *,
    base_dir: Path,
    on_exec: "Callable[[list[str], str, int, bool], None] | None" = None,
) -> list[dict] | None:
    """Deterministically gather the per-item fact list for a listDecision step.

    Returns one fact dict per input item (ready for `_decide_list`), or None
    when the step declares neither an exec `itemFacts` nor an `itemSource`
    (caller then runs the model path). Never raises.

    `on_exec`, when given, is called once per subprocess the resolver runs with
    `(argv, stdout, returncode, is_error)`. The engine uses this to surface each
    deterministic exec as a tool_call/tool_result on the stream, so a workflow
    that prices items inside the engine is still observable (and scoreable by
    Tool Correctness) — without it those execs were invisible."""
    facts_spec = step.get("itemFacts")
    items = _read_items(step, base_dir)
    # No exec spec: if the step still names an `itemSource`, read the items from
    # the file and project each one's declared `itemVariables` fields directly —
    # no script, no model. This covers decision branches that need no computed
    # facts (e.g. the fraud-reject path: it rejects every line item regardless of
    # price/stock, so it only needs the items' own fields). Without this the
    # engine fell back to the model, which hallucinated a single `UNKNOWN` item
    # instead of reading the real SKUs.
    if not isinstance(facts_spec, dict) or facts_spec.get("kind") != "exec":
        if items is None:
            return None
        return _project_item_fields(step, items)
    if items is None:
        return None
    command = facts_spec.get("command")
    derive = facts_spec.get("derive")
    if not isinstance(command, list) or not isinstance(derive, dict):
        return None

    out_facts: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        argv = [_fmt(tok, item) for tok in command]
        output: dict = {}
        stdout, returncode, is_error = "", 0, False
        try:
            proc = subprocess.run(
                argv, cwd=str(base_dir), capture_output=True, text=True,
                timeout=_EXEC_TIMEOUT_S,
            )
            stdout, returncode = proc.stdout, proc.returncode
            is_error = returncode != 0
            if facts_spec.get("parse") == "json" and proc.stdout.strip():
                parsed = json.loads(proc.stdout)
                if isinstance(parsed, dict):
                    output = parsed
        except Exception as exc:
            output, is_error = {}, True
            stdout = stdout or str(exc)
        if on_exec is not None:
            try:
                on_exec(argv, stdout, returncode, is_error)
            except Exception:
                pass  # observability must never break resolution
        fact = {name: _derive_field(rule, item, output)
                for name, rule in derive.items()
                if isinstance(rule, dict)}
        out_facts.append(fact)
    return out_facts
