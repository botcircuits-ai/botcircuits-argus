"""The generic verification-gate executor — `run_gate`.

This is the ONE fixed entry point every workflow's gate runs through.
It is deliberately NOT workflow-specific: the only thing that varies
per-workflow is the `gate_spec` (the check list — script paths, judge
rubrics), which is generated at build time (see `generator.py`). The
executor itself never changes shape to accommodate a particular
workflow's checks.
"""

from __future__ import annotations

from pathlib import Path

from botcircuits.providers.base import LLMProvider

from . import judge_check, script_check
from .types import CheckResult, GateResult, VerificationError

_KNOWN_TYPES = {"script", "llm_judge"}


async def run_gate(
    gate_spec: dict,
    run: dict,
    *,
    provider: LLMProvider | None = None,
    base_dir: Path,
) -> GateResult:
    """Run every check in `gate_spec["checks"]` against the completed
    `run` record and return the aggregate `GateResult`.

    `run` is the completed-run record the checks are evaluated against:
    `{workflow_name, slots, summary, decisions, done}` (built by the
    caller from the engine's `EngineResult`).

    `provider` is required only if the gate has any `llm_judge` check;
    its absence raises `VerificationError` up front rather than failing
    deep inside the check loop once judge checks are reached.

    `base_dir` is the directory `script` paths in the gate spec are
    resolved relative to (the workflow's build folder).
    """
    workflow_name = gate_spec.get("workflow_name") or ""
    checks = gate_spec.get("checks") or []

    if provider is None and any(
        isinstance(c, dict) and c.get("type") == "llm_judge" for c in checks
    ):
        raise VerificationError(
            f"gate for {workflow_name!r} has an llm_judge check but no "
            "provider was supplied"
        )

    results: list[CheckResult] = []
    for check in checks:
        if not isinstance(check, dict):
            results.append(CheckResult(
                check_id="<malformed>", passed=False, severity="blocking",
                error=f"check entry is not an object: {check!r}",
            ))
            continue
        ctype = check.get("type")
        if ctype == "script":
            results.append(await script_check.run_one(check, run, base_dir=base_dir))
        elif ctype == "llm_judge":
            results.append(await judge_check.run_one(check, run, provider=provider))
        else:
            results.append(CheckResult(
                check_id=check.get("id") or "<unnamed>",
                passed=False, severity="blocking",
                error=(
                    f"unknown check type {ctype!r}; expected one of "
                    f"{sorted(_KNOWN_TYPES)}"
                ),
            ))

    passed = not any(c.is_blocking_failure for c in results)
    return GateResult(workflow_name=workflow_name, passed=passed, checks=results)
