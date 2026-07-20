"""Scorers for coding-eval runs.

Two signals, matching the plan:

  1. OBJECTIVE (primary) — run the case's target test command in the sandbox
     after the agent has edited it. `tests_pass` is exit==0. Optionally re-run
     the `guard_command` to confirm pre-existing behavior still passes
     (`no_regressions`). This is the headline metric.

  2. LLM JUDGE (secondary) — an optional quality score in [0,1] for nuance the
     pass/fail can't capture (over-reach, partial credit, style). Uses a plain
     provider call with a strict-JSON rubric, so it works without pulling in
     the heavier deepeval stack; deepeval's TaskCompletionMetric can be swapped
     in later if desired.

Both are pure functions of (sandbox path / transcript) so they can be unit
tested without an agent run.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from botcircuits.providers.base import LLMProvider
from botcircuits.runtime.cli_exec import CliExecError, run_cli
from botcircuits.types import Message


@dataclass
class ObjectiveScore:
    tests_pass: bool
    no_regressions: bool | None  # None when the case declares no guard_command
    target_output: str = ""
    guard_output: str = ""
    error: str | None = None


def _split_command(command: str) -> list[str]:
    """Turn a shell-ish test command string into an argv for run_cli.

    Test commands are trusted dataset content (e.g. `python -m pytest -q
    tests/test_x.py`), so a simple whitespace split is enough; run_cli execs
    the argv directly (no shell), keeping it injection-safe."""
    return command.split()


async def run_objective(
    sandbox: Path,
    *,
    test_command: str,
    guard_command: str | None,
    timeout: float = 300.0,
) -> ObjectiveScore:
    """Run the target (and optional guard) test commands in `sandbox`.

    Never raises on a failing test — a non-zero exit just means `tests_pass`
    is False. Only an un-runnable command (missing binary) surfaces as an
    error string on the score."""
    try:
        target = await run_cli(
            _split_command(test_command), prompt="",
            cwd=str(sandbox), timeout=timeout,
        )
    except CliExecError as e:
        return ObjectiveScore(
            tests_pass=False, no_regressions=None, error=str(e))

    tests_pass = target.ok
    target_out = (target.stdout or "") + (target.stderr or "")

    no_regressions: bool | None = None
    guard_out = ""
    if guard_command:
        try:
            guard = await run_cli(
                _split_command(guard_command), prompt="",
                cwd=str(sandbox), timeout=timeout,
            )
            no_regressions = guard.ok
            guard_out = (guard.stdout or "") + (guard.stderr or "")
        except CliExecError as e:
            no_regressions = False
            guard_out = str(e)

    return ObjectiveScore(
        tests_pass=tests_pass,
        no_regressions=no_regressions,
        target_output=target_out[-4000:],
        guard_output=guard_out[-2000:],
    )


_JUDGE_SYSTEM = (
    "You are a strict code-review judge. Given a coding task's goal and a "
    "record of what an agent did, rate how well the task was accomplished. "
    "Return ONLY strict JSON: {\"score\": <0..1 float>, \"reason\": \"<one "
    "sentence>\"}. Reward a correct, minimal, on-target change; penalize "
    "over-reach, unrelated edits, and incomplete work. Do NOT reward "
    "confident narration that isn't backed by actual changes."
)


def _judge_prompt(goal: str, transcript: str, diff: str,
                  objective: ObjectiveScore) -> str:
    return "\n".join([
        f"TASK GOAL:\n{goal}",
        "",
        "OBJECTIVE TEST RESULT (ground truth — weigh heavily):",
        f"  target tests passed: {objective.tests_pass}",
        f"  no regressions: {objective.no_regressions}",
        "",
        "AGENT TRANSCRIPT (final replies across turns):",
        transcript[:6000] or "(none)",
        "",
        "CODE DIFF the run produced:",
        diff[:6000] or "(no diff captured)",
        "",
        "Return the JSON score now.",
    ])


def _extract_score(text: str) -> tuple[float, str]:
    """Pull {score, reason} out of the judge reply. Tolerates fences / prose
    around the JSON. Clamps score to [0,1]; returns (0.0, reason) on parse
    failure so a flaky judge never crashes the run."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return 0.0, "judge returned no JSON"
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return 0.0, "judge JSON parse error"
    try:
        score = float(obj.get("score", 0.0))
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))
    reason = str(obj.get("reason", ""))[:300]
    return score, reason


@dataclass
class JudgeScore:
    score: float
    reason: str = ""


async def run_judge(
    provider: LLMProvider,
    *,
    goal: str,
    transcript: str,
    diff: str,
    objective: ObjectiveScore,
) -> JudgeScore:
    """LLM-judge the run for quality in [0,1]. Best-effort: any failure yields
    a 0.0 score with the reason, never an exception."""
    try:
        prev_purpose = getattr(provider, "usage_purpose", None)
        try:
            provider.usage_purpose = "coding_eval_judge"
        except Exception:
            pass
        resp = await provider.complete(
            system=_JUDGE_SYSTEM,
            messages=[Message(role="user", blocks=[{
                "type": "text",
                "text": _judge_prompt(goal, transcript, diff, objective),
            }])],
            tools=[], hosted_mcp=[], skills=[], max_tokens=512,
        )
        try:
            provider.usage_purpose = prev_purpose
        except Exception:
            pass
        score, reason = _extract_score(resp.text or "")
        return JudgeScore(score=score, reason=reason)
    except Exception as e:  # pragma: no cover - defensive
        return JudgeScore(score=0.0, reason=f"judge error: {type(e).__name__}: {e}")


__all__ = [
    "ObjectiveScore",
    "JudgeScore",
    "run_objective",
    "run_judge",
]
