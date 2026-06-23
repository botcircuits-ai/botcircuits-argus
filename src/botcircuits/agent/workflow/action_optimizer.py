"""Build-time *action optimizer* — Pass 1 of `workflow build`.

Workflows are authored in natural language. A human writes a step's `action`
as prose ("build an explicit validation checklist restating each field, then
mark each PRESENT&VALID or MISSING/INVALID..."). At *runtime* the engine hands
each segment's action text to the LLM, which then tends to mirror that verbose,
explanatory tone back as output — and on a per-segment, every-run basis that
prose is the dominant token cost (output is billed several× input on most
providers).

This pass rewrites each step's `settings.action` into a terse, **tool-directed**
instruction that means the same thing but tells the model to *act*, not narrate
— so the author keeps writing naturally and the *built* workflow runs lean. It
is the build-time analogue of the runtime "be terse" rule in
`engine/segment_exec.py:ENGINE_SYSTEM_PROMPT`: the prompt keeps the model terse
in general; this keeps each specific instruction terse.

Design contract (why this is safe to run on any workflow):

  * **Only `settings.action` strings change.** The step graph, `choices`,
    `conditions`, `variables`, `next`, types, and ids are never touched.
  * **Meaning-preserving, not creative.** The rewrite must keep every concrete
    requirement: explicit file paths, tool/script names, exact literal values
    (slot sentinel words like 'valid'/'blocked', numeric thresholds), the exact
    shape of any required output (e.g. a fenced-JSON answer block), and which
    branch variables the step sets. The prompt forbids dropping any of these.
  * **Conservative fallback.** One LLM call for the whole flow; if it fails or
    returns nothing usable for a step, that step keeps its original action. A
    rewrite that looks lossy (lost a path/tool/literal the original had) is
    rejected per-step, so a bad optimization degrades to the author's text
    rather than to a broken workflow.
  * **Idempotent.** Re-running on an already-built flow re-optimizes from the
    *current* action text; terse text optimizes to itself.

Callers run this AFTER `generate_expressions_and_variables` (so the structural
build is settled) and BEFORE `compute_segments` (segmentation is unaffected
either way, but ordering keeps the artifact's action text final before it's
partitioned). It is opt-out via the `--no-optimize` build flag.
"""

from __future__ import annotations

import re

from botcircuits.providers.base import LLMProvider
from botcircuits.types import Message

from .condition_processor import _extract_json

# A rewrite must not silently drop a concrete requirement the author encoded.
# We extract these "anchors" from the original action and require each to
# survive into the rewrite; if any is missing, we keep the original for that
# step. Cheap, deterministic, and provider-independent.
_PATH_RE = re.compile(r"[\w./-]+\.(?:json|txt|py|md|csv|ya?ml)\b")
_QUOTED_RE = re.compile(r"'([^']{1,40})'|\"([^\"]{1,40})\"")


def _anchors(action: str) -> set[str]:
    """Concrete tokens a faithful rewrite must preserve: file paths/scripts
    and single/double-quoted literals (slot sentinels, exact words)."""
    anchors: set[str] = set(_PATH_RE.findall(action))
    for a, b in _QUOTED_RE.findall(action):
        lit = a or b
        if lit:
            anchors.add(lit)
    return anchors


def _optimizable_steps(flow: dict) -> list[tuple[str, dict, str]]:
    """`(step_id, step, action)` for every step carrying a non-empty action."""
    out: list[tuple[str, dict, str]] = []
    for step_id, step in (flow.get("steps") or {}).items():
        if not isinstance(step, dict):
            continue
        action = (step.get("settings") or {}).get("action")
        if isinstance(action, str) and action.strip():
            out.append((step_id, step, action))
    return out


def _build_prompt(entries: list[tuple[str, dict, str]]) -> str:
    blocks: list[str] = []
    for step_id, step, action in entries:
        blocks.append(
            f"step_id={step_id} type={step.get('type', '')}\n"
            f"action:\n{action}"
        )
    return "\n".join([
        "You optimize the per-step instructions of a deterministic workflow "
        "for token efficiency. At runtime each instruction is given to an LLM "
        "that performs the step with tools; verbose, explanatory wording makes "
        "it narrate instead of act, which wastes tokens on every run.",
        "",
        "Rewrite each step's `action` to be TERSE and TOOL-DIRECTED: imperative "
        "tool steps, no restating of the rationale, no 'first/then' narration, "
        "no examples unless essential. Tell the model what to DO, not what to "
        "explain.",
        "",
        "HARD RULES — a rewrite that breaks any of these is wrong:",
        "  - Preserve meaning exactly. Same tools, same files, same branch "
        "outcome.",
        "  - Keep EVERY concrete token: file paths (e.g. data/x.json), "
        "tool/script names (read_file, shell_exec, bin/price.py), exact literal "
        "values in quotes (e.g. 'valid', 'blocked'), and numeric thresholds.",
        "  - Keep any instruction that produces a required OUTPUT (e.g. 'end "
        "with a fenced json block of the form ...') verbatim in intent — the "
        "downstream consumer depends on that exact output shape.",
        "  - Keep which variables the step sets and the rule for each value.",
        "  - Do NOT add new steps, tools, or requirements. Only compress.",
        "",
        "Steps to optimize:",
        "",
        "\n\n".join(blocks),
        "",
        "Respond with a JSON object (no commentary, no markdown fence):",
        "{",
        '  "actions": [',
        '    { "step_id": "<id>", "action": "<terse rewritten action>" }',
        "  ]",
        "}",
    ])


async def _ask_llm_for_json(provider: LLMProvider, prompt: str) -> str:
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
        max_tokens=4096,
    )
    return response.text


def _accept_rewrite(original: str, rewritten: str) -> bool:
    """Keep the rewrite only if it's shorter AND preserves every anchor token
    from the original. Otherwise the author's text wins (safe degradation)."""
    rewritten = rewritten.strip()
    if not rewritten or len(rewritten) >= len(original):
        return False
    return _anchors(original) <= _anchors(rewritten)


async def optimize_actions(flow: dict, provider: LLMProvider) -> dict:
    """Mutate `flow` in place, rewriting verbose step `action` text into terse,
    tool-directed instructions. Returns a summary dict for the CLI to log.

    Conservative by construction: one LLM call; per-step rewrites are accepted
    only when they're shorter and keep every concrete anchor (paths, tool names,
    quoted literals). Any failure leaves the original action untouched."""
    entries = _optimizable_steps(flow)
    if not entries:
        return {"steps_optimized": 0, "chars_before": 0, "chars_after": 0}

    prompt = _build_prompt(entries)
    try:
        raw = await _ask_llm_for_json(provider, prompt)
        parsed = _extract_json(raw)
    except Exception:
        # Optimization is best-effort: a failure must never fail the build.
        return {"steps_optimized": 0, "chars_before": 0, "chars_after": 0,
                "skipped": "optimizer call failed"}

    rewrites: dict[str, str] = {}
    for item in (parsed.get("actions") if isinstance(parsed, dict) else None) or []:
        if not isinstance(item, dict):
            continue
        sid, act = item.get("step_id"), item.get("action")
        if isinstance(sid, str) and isinstance(act, str):
            rewrites[sid] = act

    optimized = 0
    chars_before = 0
    chars_after = 0
    for step_id, step, action in entries:
        chars_before += len(action)
        new = rewrites.get(step_id)
        if new is not None and _accept_rewrite(action, new):
            step["settings"]["action"] = new.strip()
            chars_after += len(new.strip())
            optimized += 1
        else:
            chars_after += len(action)

    return {
        "steps_optimized": optimized,
        "chars_before": chars_before,
        "chars_after": chars_after,
    }
