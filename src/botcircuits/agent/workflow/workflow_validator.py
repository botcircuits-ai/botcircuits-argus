"""Validate a generated/authored workflow source and surface fixable problems.

A generator LLM produces structurally-plausible but subtly-wrong workflows that
only fail at RUNTIME — the workflow tool never triggers, the engine crashes on a
mis-shaped field, a step pauses asking the user for data that's on disk, or it
produces no decisions. `generate_workflow` runs these checks each iteration of
its validate→repair loop and feeds the messages back to the model to fix.

Two tiers:
  * `static_issues(doc, sample_input)` — pure, no LLM, no run. Catches the shape
    and wiring mistakes we've actually seen.
  * the caller adds an optional DRY-RUN (build + engine run on a sample) for
    runtime logic errors; that lives with the build/engine, not here.

Each issue is a short imperative string the model can act on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _steps(doc: dict) -> dict:
    flow = doc.get("flow") if isinstance(doc, dict) else None
    s = flow.get("steps") if isinstance(flow, dict) else None
    return s if isinstance(s, dict) else {}


def _declared_var_names(doc: dict) -> set[str]:
    flow = doc.get("flow") or {}
    out: set[str] = set()
    for v in flow.get("variables") or []:
        if isinstance(v, dict) and isinstance(v.get("variableName"), str):
            out.add(v["variableName"])
    # plus per-item variables on listDecision steps
    for step in _steps(doc).values():
        iv = step.get("itemVariables") if isinstance(step, dict) else None
        if isinstance(iv, list):
            for v in iv:
                if isinstance(v, dict) and isinstance(v.get("variableName"), str):
                    out.add(v["variableName"])
        elif isinstance(iv, dict):
            out.update(k for k in iv if isinstance(k, str))
    return out


def _dig(data: Any, path: str) -> Any:
    cur = data
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def dry_run_decisions(built_flow: dict, *, base_dir: Path) -> list[dict] | None:
    """Run the DETERMINISTIC per-item decision path of a BUILT workflow flow
    against whatever input is staged on disk, and return the per-item decisions
    — exactly what the engine would produce — WITHOUT any LLM call.

    Used by a build-time dry-run: stage a sample input, build the draft, call
    this, and compare to the expected decisions. Mismatches are fed back to the
    generator to repair the value-level wiring (e.g. a condition that tests
    `found is 'false'` when the facts carry a boolean) that static checks can't
    see. Returns None if the flow has no exec-backed listDecision step (nothing
    to dry-run deterministically).
    """
    from botcircuits.agent.workflow.engine.item_resolver import resolve_item_facts
    from botcircuits.agent.workflow.engine.runner import _decide_list

    steps = built_flow.get("steps") or {}
    # Find the listDecision that actually prices/decides items (has itemFacts).
    target = None
    for step in steps.values():
        if isinstance(step, dict) and step.get("type") == "listDecision" \
                and step.get("itemFacts") and step.get("choices") is not None:
            target = step
            break
    if target is None:
        return None
    facts = resolve_item_facts(target, base_dir=base_dir)
    if facts is None:
        return None
    return _decide_list("dry_run", target, facts)


def static_issues(doc: dict, *, base_dir: Path | None = None) -> list[str]:
    """Return a list of fixable problems with the workflow `doc` (empty == ok).

    `base_dir` (the run cwd / workspace) lets the check verify that file paths a
    resolver / itemSource references actually exist and that an itemSource path
    points at a list in that file.
    """
    issues: list[str] = []
    flow = doc.get("flow") if isinstance(doc, dict) else None
    if not isinstance(flow, dict):
        return ["The top-level `flow` object is missing."]

    desc = doc.get("description")
    if not isinstance(desc, str) or not desc.strip():
        issues.append(
            "Add a `description` (one line on what the workflow does and when "
            "to run it) — the agent uses it to decide when to call the workflow.")

    steps = _steps(doc)
    if not steps:
        issues.append("`flow.steps` is empty — define the workflow's steps.")
    start = flow.get("start")
    if isinstance(start, str) and start not in steps:
        issues.append(f"`flow.start` ('{start}') is not a defined step.")

    declared = _declared_var_names(doc)
    declared_agents = doc.get("agents")
    declared_agents = declared_agents if isinstance(declared_agents, dict) else {}

    # Valid `runtime`/`provider` values an `agents.<name>` entry may declare.
    # Kept as a local literal (not imported from `botcircuits.runtime.detect` /
    # `botcircuits.providers`) so this pure, no-LLM validator stays decoupled
    # from the runtime layer; update alongside `_REGISTRY` / `make_provider`
    # if either grows a new name.
    _VALID_RUNTIMES = {"native", "self", "claude-code", "codex", "openclaw", "hermes"}
    _VALID_PROVIDERS = {"anthropic", "openai", "gemini", "openrouter"}
    for agent_name, cfg in declared_agents.items():
        if not isinstance(cfg, dict):
            issues.append(
                f"agents.{agent_name} must be an object "
                "({runtime?, provider?, model?}).")
            continue
        rt = cfg.get("runtime")
        if isinstance(rt, str) and rt:
            if rt not in _VALID_RUNTIMES:
                issues.append(
                    f"agents.{agent_name}.runtime '{rt}' is not a supported "
                    f"runtime — use one of {sorted(_VALID_RUNTIMES)}.")
            elif rt == "self":
                # The inline/self runtime always hands segments to the HOST's
                # own model (`InlineRuntime.run_segment` ignores `agent`
                # entirely) — pinning an agent to it can never take effect.
                issues.append(
                    f"agents.{agent_name}.runtime is 'self' (inline) — the "
                    "inline runtime always uses the host's own model and "
                    "has no per-agent overrides; remove this override.")
        provider = cfg.get("provider")
        if isinstance(provider, str) and provider and provider not in _VALID_PROVIDERS:
            issues.append(
                f"agents.{agent_name}.provider '{provider}' is not a "
                f"supported provider — use one of {sorted(_VALID_PROVIDERS)}.")

    def _check_file(rel: str, ctx: str) -> Any:
        if base_dir is None:
            return None
        p = base_dir / rel
        if not p.exists():
            issues.append(f"{ctx}: file '{rel}' does not exist in the workspace.")
            return None
        try:
            return json.loads(p.read_text())
        except Exception:
            return None  # non-JSON (e.g. a .txt blocklist) — fine

    for sid, step in steps.items():
        if not isinstance(step, dict):
            continue
        stype = step.get("type")
        action = (step.get("settings") or {}).get("action", "")

        # A step pinned to a named agent must have that name declared in the
        # top-level `agents` map, or the reference is a typo/leftover that
        # silently falls back to the run's default model.
        step_agent = step.get("agent")
        if isinstance(step_agent, str) and step_agent and step_agent not in declared_agents:
            issues.append(
                f"Step '{sid}' references unknown agent '{step_agent}' — add "
                "it to the top-level `agents` map.")

        # A `question` step pauses for the user. If the data it asks about is on
        # disk, that's almost always wrong — flag it.
        if stype == "question":
            issues.append(
                f"Step '{sid}' is a `question` (it pauses for the user). If the "
                "data it needs is in a workspace file, READ that file in an "
                "agentAction or via a resolver instead of asking.")

        # listDecision wiring.
        if stype == "listDecision":
            iv = step.get("itemVariables")
            if isinstance(iv, dict):
                issues.append(
                    f"Step '{sid}': `itemVariables` must be a LIST of "
                    "{variableName, description} objects, not an object keyed "
                    "by name.")
            elif not isinstance(iv, list) or not iv:
                issues.append(
                    f"Step '{sid}': add `itemVariables` (the per-item facts its "
                    "conditions test).")
            src = step.get("itemSource")
            if not isinstance(src, dict) or "file" not in src:
                issues.append(
                    f"Step '{sid}': add `itemSource` {{file, path}} pointing at "
                    "the list of items to process.")
            else:
                data = _check_file(src["file"], f"Step '{sid}' itemSource")
                if data is not None:
                    lst = _dig(data, src.get("path", "")) if src.get("path") else data
                    if not isinstance(lst, list):
                        issues.append(
                            f"Step '{sid}': itemSource path "
                            f"'{src.get('path')}' is not a list in "
                            f"'{src['file']}'. Use the exact key that holds the "
                            "item array.")

            # On a listDecision, the conditions' `next` and the default `next`
            # are the per-item OUTCOME LABELS — they become the decision word
            # for each item (decisionKey). They must be REAL outcomes
            # (fulfill/backorder/reject/...), not:
            #   * a generic placeholder (completed/done/output/...), or
            #   * a STEP NAME (e.g. 'finalize_output', 'emit_result') — a common
            #     generator slip that conflates "the decision word" with "where
            #     to go next". A listDecision doesn't navigate to a next step per
            #     item; its `next` IS the default decision label.
            _GENERIC = {"completed", "complete", "done", "next", "end",
                        "finish", "finished", "output", "result", "finalize",
                        "emit", "continue", "proceed", "default"}
            labels = [c.get("next") for c in step.get("conditions") or []]
            labels.append(step.get("next"))
            for lbl in labels:
                if not isinstance(lbl, str) or not lbl:
                    continue
                low = lbl.lower()
                if lbl in steps:
                    issues.append(
                        f"Step '{sid}': outcome label '{lbl}' is the name of a "
                        "STEP. On a listDecision, each branch `next` (and the "
                        "default `next`) is the DECISION WORD for the item "
                        "(e.g. fulfill / backorder / review / reject), not a step "
                        "to navigate to. Replace it with the real outcome word.")
                    break
                if low in _GENERIC or any(low.startswith(g) for g in
                                          ("finalize", "emit", "output")):
                    issues.append(
                        f"Step '{sid}': outcome label '{lbl}' is a generic "
                        "placeholder — name each branch (and the default `next`) "
                        "after the actual decision it represents (e.g. fulfill / "
                        "backorder / reject).")
                    break

        # Malformed expressions: a generator sometimes writes the COMPARISON as
        # prose inside the value with operator `is` — e.g.
        # {variable: stock, operator: "is", value: "less than qty"} — instead of
        # {operator: "less than", value: <number>}. Then `is` does string
        # equality, never matches, and the branch silently never fires (every
        # item falls through to the default). Flag it.
        _CMP_WORDS = ("less than", "greater than", "more than", "at least",
                      "at most", "over ", "under ", "above", "below",
                      "fewer than", "exceeds", "not enough", "insufficient")
        for ch in step.get("choices") or []:
            for e in ch.get("expressionList") or []:
                op = str(e.get("operator", "")).lower()
                val = e.get("value")
                if op in ("is", "is not") and isinstance(val, str):
                    low = val.lower()
                    if any(w in low for w in _CMP_WORDS):
                        issues.append(
                            f"Step '{sid}': condition `{e.get('variable')} {op} "
                            f"'{val}'` puts a COMPARISON in the value with "
                            "operator 'is' (string-equality) — it will never "
                            "match. Use a real operator (e.g. 'less than' / "
                            "'greater than') with a numeric value, or write the "
                            "branch as a natural-language `condition` and let "
                            "the builder compile it.")

        # OR-conditions silently drop branches. A natural-language condition that
        # joins alternatives with " or " (e.g. "status is exception or returned or
        # lost") compiles to a SINGLE comparison — only the first alternative
        # matches and the rest vanish with no error, so items that should have hit
        # the branch fall through to the default. Flag it so the author splits it
        # into one entry per alternative, each pointing at the same `next`. (" and "
        # compiles to a real conjunction, so it isn't flagged.)
        for c in step.get("conditions") or []:
            if not isinstance(c, dict):
                continue
            text = c.get("condition")
            if isinstance(text, str) and " or " in text.lower():
                issues.append(
                    f"Step '{sid}': condition \"{text}\" joins alternatives with "
                    "'or' — it compiles to a SINGLE branch and silently drops the "
                    "rest. Split it into one condition per alternative, each with "
                    f"the same `next` ('{c.get('next')}').")

        # resolver file existence
        # (variables carry resolvers; check their files)
    for v in flow.get("variables") or []:
        if not isinstance(v, dict):
            continue
        r = v.get("resolver")
        if isinstance(r, dict):
            for key in ("file",):
                f = r.get(key)
                if isinstance(f, str):
                    _check_file(f, f"Variable '{v.get('variableName')}' resolver")
            vs = r.get("value_source") or r.get("source")
            if isinstance(vs, dict) and isinstance(vs.get("file"), str):
                _check_file(vs["file"], f"Variable '{v.get('variableName')}' resolver source")

    return issues
