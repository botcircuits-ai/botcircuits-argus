"""LLM-judge execution for `type: "llm_judge"` checks.

A judge check has a natural-language `rubric` and an explicit allow-list
of dotted paths (`inputs`) into the completed run record — the judge
only ever sees the values named in `inputs`, never the whole run, so an
author's `inputs` list is also a privacy/scope boundary. One plain
`provider.complete()` call, no tools, no streaming — the same shape
`condition_processor._ask_llm_for_json` already uses. Never raises: any
call or parse failure yields a `CheckResult` with `error` set rather
than propagating, so one bad judge call can't crash the whole gate.
"""

from __future__ import annotations

from typing import Any

from botcircuits.agent.workflow._json_repair import extract_json
from botcircuits.providers.base import LLMProvider
from botcircuits.types import Message

from .types import CheckResult

_SYSTEM = (
    "You are a strict verification judge for an automated workflow run. "
    "Output ONLY strict JSON matching this schema, no commentary, no "
    "markdown fences: {\"passed\": true|false, \"detail\": \"<one "
    "sentence explaining the verdict>\"}. Judge only against the stated "
    "rubric and the given inputs — do not reward confident narration "
    "that isn't backed by the actual input values."
)


def _dig(data: Any, path: str) -> Any:
    cur = data
    for part in path.split("."):
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


def _build_prompt(check: dict, run: dict) -> str:
    inputs = check.get("inputs") or []
    lines = [f"Rubric: {check.get('rubric') or check.get('description') or ''}",
             "", "Inputs:"]
    for path in inputs:
        if not isinstance(path, str):
            continue
        lines.append(f"  {path}: {_dig(run, path)!r}")
    lines.append("")
    lines.append("Judge whether the rubric is satisfied by these inputs.")
    return "\n".join(lines)


async def run_one(check: dict, run: dict, *, provider: LLMProvider) -> CheckResult:
    check_id = check.get("id") or "<unnamed>"
    severity = check.get("severity") or "advisory"
    prompt = _build_prompt(check, run)
    messages = [Message(role="user", blocks=[{"type": "text", "text": prompt}])]
    try:
        response = await provider.complete(
            system=_SYSTEM, messages=messages, tools=[], hosted_mcp=[],
            skills=[], max_tokens=1024,
        )
        parsed = extract_json(response.text or "")
    except Exception as e:  # noqa: BLE001 - a judge failure must never crash the gate
        return CheckResult(
            check_id=check_id, passed=False, severity=severity,
            error=f"judge call failed: {type(e).__name__}: {e}",
        )

    if not isinstance(parsed, dict) or "passed" not in parsed:
        return CheckResult(
            check_id=check_id, passed=False, severity=severity,
            error=f"judge returned malformed JSON: {parsed!r}",
        )
    return CheckResult(
        check_id=check_id,
        passed=bool(parsed.get("passed")),
        severity=severity,
        detail=str(parsed.get("detail") or ""),
    )
