"""Aggregation + orchestration for the coding evaluation.

`run_coding_evaluation` loops cases × modes × repeats (SERIAL — the agent
modes mutate the process cwd, so they can't overlap), collecting
`CodingRunResult`s, then `summarize_mode` rolls them into a per-mode summary.
`render_coding_report` prints the three-column comparison.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import mean

from botcircuits.providers.base import LLMProvider
from botcircuits.agent.workflow.evaluation.coding_dataset import CodingDataset
from botcircuits.agent.workflow.evaluation.coding_runner import (
    MODES,
    CodingRunResult,
    run_coding_case,
)


@dataclass
class ModeSummary:
    mode: str
    runs: int = 0
    errors: int = 0
    tests_pass_rate: float = 0.0        # fraction of runs with green target tests
    no_regression_rate: float = 0.0     # among runs that declared a guard
    judge_mean: float = 0.0
    consistency_mean: float = 0.0       # mean per-case agreement of tests_pass
    avg_input_tokens: float = 0.0
    avg_output_tokens: float = 0.0
    avg_llm_calls: float = 0.0
    avg_elapsed_s: float = 0.0


@dataclass
class CodingReport:
    dataset: str
    repeats: int
    mode_summaries: dict = field(default_factory=dict)   # mode -> ModeSummary
    per_run: list = field(default_factory=list)          # list[dict]

    def to_dict(self) -> dict:
        return {
            "dataset": self.dataset,
            "repeats": self.repeats,
            "modes": {m: asdict(s) for m, s in self.mode_summaries.items()},
            "per_run": self.per_run,
        }


def _case_consistency(pass_flags: list[bool]) -> float:
    """Agreement of tests_pass across a case's repeats: fraction matching the
    majority verdict. 1.0 when all repeats agree (all pass or all fail)."""
    if not pass_flags:
        return 0.0
    trues = sum(1 for p in pass_flags if p)
    modal = max(trues, len(pass_flags) - trues)
    return modal / len(pass_flags)


def summarize_mode(mode: str, runs: list[CodingRunResult],
                   *, case_ids: list[str], repeats: int) -> ModeSummary:
    """Roll up all runs for one mode into a ModeSummary. `case_ids` + `repeats`
    let consistency be computed per case (agreement across repeats)."""
    s = ModeSummary(mode=mode, runs=len(runs))
    if not runs:
        return s
    s.errors = sum(1 for r in runs if r.error)

    pass_flags = [bool(r.objective and r.objective.tests_pass) for r in runs]
    s.tests_pass_rate = mean(1.0 if p else 0.0 for p in pass_flags)

    guard_runs = [r for r in runs
                  if r.objective and r.objective.no_regressions is not None]
    if guard_runs:
        s.no_regression_rate = mean(
            1.0 if r.objective.no_regressions else 0.0 for r in guard_runs)

    judged = [r.judge.score for r in runs if r.judge is not None]
    if judged:
        s.judge_mean = mean(judged)

    # Per-case consistency across repeats, then averaged over cases.
    by_case: dict[str, list[bool]] = {cid: [] for cid in case_ids}
    for r in runs:
        by_case.setdefault(r.case_id, []).append(
            bool(r.objective and r.objective.tests_pass))
    consist = [_case_consistency(flags) for flags in by_case.values() if flags]
    if consist:
        s.consistency_mean = mean(consist)

    s.avg_input_tokens = mean(r.input_tokens for r in runs)
    s.avg_output_tokens = mean(r.output_tokens for r in runs)
    s.avg_llm_calls = mean(r.llm_calls for r in runs)
    s.avg_elapsed_s = mean(r.elapsed_s for r in runs)
    return s


async def run_coding_evaluation(
    dataset: CodingDataset,
    provider: LLMProvider | None,
    *,
    modes: tuple[str, ...] = MODES,
    repeats: int = 3,
    judge: bool = True,
) -> CodingReport:
    """Run every case in every mode `repeats` times (serial) and summarize.

    Runs are serial because the agent modes chdir into their sandbox — a
    concurrent run would see the wrong cwd. Each individual run still isolates
    its own file changes in its own sandbox copy.
    """
    report = CodingReport(dataset=dataset.name, repeats=repeats)
    runs_by_mode: dict[str, list[CodingRunResult]] = {m: [] for m in modes}
    case_ids = [c.id for c in dataset.cases]

    for case in dataset.cases:
        for mode in modes:
            for _ in range(repeats):
                res = await run_coding_case(
                    case, dataset, provider, mode=mode, judge=judge)
                runs_by_mode[mode].append(res)
                report.per_run.append(res.to_dict())

    for mode in modes:
        report.mode_summaries[mode] = summarize_mode(
            mode, runs_by_mode[mode], case_ids=case_ids, repeats=repeats)
    return report


__all__ = [
    "ModeSummary",
    "CodingReport",
    "summarize_mode",
    "run_coding_evaluation",
]
