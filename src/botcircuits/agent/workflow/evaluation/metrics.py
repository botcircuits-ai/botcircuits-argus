"""Accuracy and consistency scoring.

We score two things, on the same set of cases, for both the workflow
runner and the prompt-only runner:

  - Accuracy: did the run reach the *expected* outcome? Split into:
      * trace_match    — 1 iff the produced trace equals expected.trace
      * final_match    — 1 iff the final paused state equals
                         expected.final_state (a softer signal for cases
                         where intermediate steps don't need to be
                         pinned)
      * contains_match — 1 iff all expected.must_contain substrings
                         appear in final_action text
    The aggregate score per case is the average of the three signals
    that were declared in `expected` (signals with no expectation are
    skipped, not penalized).

  - Consistency: for runs of the same case repeated N times, what
    fraction produced the same trace as the modal trace? Workflow
    runs are deterministic so this should be 1.0; prompt-only runs
    surface the model's variance.

The summary aggregates per workflow and across the whole dataset.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from statistics import mean
from typing import Any

from botcircuits.agent.workflow.evaluation.dataset import EvalCase


@dataclass
class CaseAccuracy:
    case_id: str
    trace_match: bool | None
    final_match: bool | None
    contains_match: bool | None
    # Per-case 0..1 score: average over the signals that were actually
    # checked. None when no signal was declared (rare; eval cases
    # should always declare something).
    score: float | None


@dataclass
class ConsistencyResult:
    case_id: str
    runs: int
    modal_trace: list[str]
    consistency: float          # fraction of runs matching modal_trace
    unique_traces: int
    failures: int               # runs that errored out before producing a trace


@dataclass
class RunnerSummary:
    runner: str                  # "workflow" | "prompt"
    accuracy_mean: float
    trace_match_rate: float
    final_match_rate: float
    contains_match_rate: float
    consistency_mean: float
    hallucination_rate: float    # fraction of cases with >=1 hallucinated state
    parse_error_rate: float      # prompt-only: fraction with un-parseable replies
    total_input_tokens: int
    total_output_tokens: int
    cases: int


@dataclass
class DatasetReport:
    workflow_summary: RunnerSummary
    prompt_summary: RunnerSummary
    per_case: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "workflow": asdict(self.workflow_summary),
            "prompt": asdict(self.prompt_summary),
            "per_case": self.per_case,
        }


def score_case(case: EvalCase, trace: list[str], final_action: str) -> CaseAccuracy:
    """Score one run against the case's expectations.

    Signals are only checked when the case actually declares them. This
    lets dataset authors pin only the outcome they care about (e.g.
    just `final_state`) without being forced to write a full trace.
    """
    exp = case.expected
    trace_match: bool | None = None
    final_match: bool | None = None
    contains_match: bool | None = None

    if exp.trace:
        trace_match = list(trace) == list(exp.trace)
    if exp.final_state is not None:
        # Prefer the explicit trace tail if we have one; otherwise the
        # expected final state must equal the last state in the run.
        final_match = bool(trace) and trace[-1] == exp.final_state
    if exp.must_contain:
        contains_match = all(s in final_action for s in exp.must_contain)

    signals = [v for v in (trace_match, final_match, contains_match) if v is not None]
    score = (sum(1 for v in signals if v) / len(signals)) if signals else None
    return CaseAccuracy(
        case_id=case.id,
        trace_match=trace_match,
        final_match=final_match,
        contains_match=contains_match,
        score=score,
    )


def measure_consistency(case_id: str, traces: list[list[str]],
                        failures: int) -> ConsistencyResult:
    """Given N traces from repeating the same case, return how consistent
    they were. Empty traces (errors) are counted separately so a runner
    that errors out doesn't accidentally look 'consistent' just because
    every failure produced an empty list.
    """
    if not traces:
        return ConsistencyResult(
            case_id=case_id,
            runs=failures,
            modal_trace=[],
            consistency=0.0,
            unique_traces=0,
            failures=failures,
        )
    keyed = [tuple(t) for t in traces]
    counts = Counter(keyed)
    modal, modal_count = counts.most_common(1)[0]
    total = len(traces) + failures
    return ConsistencyResult(
        case_id=case_id,
        runs=total,
        modal_trace=list(modal),
        consistency=modal_count / total if total else 0.0,
        unique_traces=len(counts),
        failures=failures,
    )


def _rate(numerators: list[bool | None]) -> float:
    """Average over the not-None entries. Returns 0.0 for an all-None
    list rather than NaN so report consumers can sort/compare safely."""
    vals = [1 if v else 0 for v in numerators if v is not None]
    return mean(vals) if vals else 0.0


def summarize_runner(
    runner: str,
    accuracies: list[CaseAccuracy],
    consistencies: list[ConsistencyResult],
    hallucinated_flags: list[bool],
    parse_errors: list[bool],
    input_tokens: int,
    output_tokens: int,
) -> RunnerSummary:
    scores = [a.score for a in accuracies if a.score is not None]
    return RunnerSummary(
        runner=runner,
        accuracy_mean=mean(scores) if scores else 0.0,
        trace_match_rate=_rate([a.trace_match for a in accuracies]),
        final_match_rate=_rate([a.final_match for a in accuracies]),
        contains_match_rate=_rate([a.contains_match for a in accuracies]),
        consistency_mean=(
            mean(c.consistency for c in consistencies) if consistencies else 0.0
        ),
        hallucination_rate=(
            sum(1 for f in hallucinated_flags if f) / len(hallucinated_flags)
            if hallucinated_flags else 0.0
        ),
        parse_error_rate=(
            sum(1 for f in parse_errors if f) / len(parse_errors)
            if parse_errors else 0.0
        ),
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        cases=len(accuracies),
    )
