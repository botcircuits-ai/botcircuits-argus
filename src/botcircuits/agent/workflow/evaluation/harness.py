"""Evaluation harness — runs datasets through both Agent modes, scores, reports.

Public surface:

    run_evaluation_datasets(datasets, provider=..., repeats=3)
        -> DatasetReport

For each dataset:
  1. If the dataset is inline (carries `workflow_spec`), build the
     workflow ONCE at the start via build_workflow + indexer, then
     reuse the generated workflow across every case in the dataset.
     If the build fails, every case in that dataset is marked errored
     and the harness moves to the next dataset.
  2. Drive each case through the REAL `Agent` `repeats` times in two
     Agent modes, plus a third engine-only mode for the §7 comparison:
       - workflow_on  : Agent with the workflow tool registered. The
                        agent calls it; the ENGINE then owns the loop,
                        driving the workflow per branch-delimited segment
                        (the engine-driven mode under test).
       - workflow_off : Agent with the workflow tool disabled (via
                        `Agent.enable_workflows=False`) and the
                        dataset's `workflow_spec` appended to the
                        system prompt as plain instructions (the
                        prompt-driven baseline).
       - workflow_as_tool : the legacy per-step `run_workflow` driver
                        (`run_case_workflow`) run in isolation — the
                        pre-inversion "workflow-as-tool" path. Measured
                        standalone (not through a real Agent, since the
                        engine now intercepts advancement), so its
                        numbers are indicative, not apples-to-apples.
     The two Agent modes share the same provider + other tools. The
     third is the historical baseline the inversion replaced.
  3. Both modes run `repeats` times so the consistency metric uses
     the same denominator on both sides.
  4. Score each run with `score_case` against the dataset's
     `must_contain` / `final_state` expectations on the agent's final
     reply.
  5. After every case in the dataset has run (or failed), optionally
     clean up the inline workflow files (kept by default).

The legacy engine-only / prompt-only runners (`runner_workflow`,
`runner_prompt`) are still importable from the package for callers
who want to test the engine in isolation, but the harness's default
path drives a real Agent end-to-end — that's the comparison the
original "workflow vs prompt-only" hypothesis was really about.

The harness does not write files — that's `report.py`'s job. Keeping
I/O separate makes the harness importable from tests.
"""

from __future__ import annotations

from dataclasses import asdict, replace

from botcircuits.providers.base import LLMProvider
from botcircuits.agent.workflow.evaluation.dataset import EvalCase, EvalDataset
from botcircuits.agent.workflow.evaluation.inline_build import (
    InlineBuildError,
    build_inline_workflow,
    cleanup_inline_workflow as _cleanup_inline_workflow,
)
from botcircuits.agent.workflow.evaluation.metrics import (
    DatasetReport,
    measure_consistency,
    score_case,
    summarize_runner,
)
from botcircuits.agent.workflow.evaluation.runner_agent import (
    AgentRunResult,
    run_case_agent_no_workflow,
    run_case_agent_with_workflow,
)
from botcircuits.agent.workflow.evaluation.runner_workflow import (
    run_case_workflow,
)


async def _run_agent_repeats(
    runner,
    case: EvalCase,
    provider: LLMProvider,
    repeats: int,
    *,
    spec: str = "",
) -> list[AgentRunResult]:
    """Run an agent-mode runner N times. Each call constructs its own
    Agent (fresh registry, fresh session store) so repeats don't share
    state.

    Runs SEQUENTIALLY because each repeat already fires many provider
    calls inside Agent.chat() — running multiple repeats concurrently
    multiplies that fan-out by N and reliably trips rate limits +
    transient API connection errors on the back end.
    """
    out: list[AgentRunResult] = []
    for _ in range(repeats):
        if spec:
            out.append(await runner(case, provider, spec))
        else:
            out.append(await runner(case, provider))
    return out


class _Accumulators:
    """Holds the metric accumulator lists so `_evaluate_case` can append
    to one place. Saves us from threading nine list arguments through
    every helper signature."""

    def __init__(self) -> None:
        self.wf_acc: list = []
        self.wf_cons: list = []
        self.pr_acc: list = []
        self.pr_cons: list = []


async def run_evaluation_datasets(
    datasets: list[EvalDataset],
    *,
    provider: LLMProvider | None,
    repeats: int = 3,
    run_prompt_baseline: bool = True,
    cleanup_inline_workflow: bool = False,
) -> DatasetReport:
    """Run every dataset's cases through both Agent modes.

    Inline datasets are built once at the start of their case loop.
    The generated source + indexed-build files are KEPT on disk by
    default; pass `cleanup_inline_workflow=True` to delete them. The
    workflow-on mode reads the on-disk build artifact and registers
    the workflow tool on a fresh agent registry per case.

    `provider=None` is rejected — both modes need a real LLM. Inline
    datasets additionally need it for the build step.

    `run_prompt_baseline=False` suppresses the workflow_off (baseline)
    Agent runs while keeping workflow_on. Useful for cheaper smoke
    tests that only need to verify the workflow path still works.
    """
    acc = _Accumulators()
    per_case: list[dict] = []
    for ds in datasets:
        per_case.extend(await _evaluate_dataset(
            ds, provider=provider, repeats=repeats, acc=acc,
            run_prompt_baseline=run_prompt_baseline,
            cleanup_inline_workflow=cleanup_inline_workflow,
        ))

    wf_summary = summarize_runner(
        "workflow",
        acc.wf_acc, acc.wf_cons,
        hallucinated_flags=[], parse_errors=[],
        input_tokens=0, output_tokens=0,
    )
    pr_summary = summarize_runner(
        "prompt",
        acc.pr_acc, acc.pr_cons,
        hallucinated_flags=[], parse_errors=[],
        input_tokens=0, output_tokens=0,
    )
    return DatasetReport(
        workflow_summary=wf_summary,
        prompt_summary=pr_summary,
        per_case=per_case,
    )


async def run_evaluation(
    cases_or_datasets,
    *,
    provider: LLMProvider | None,
    repeats: int = 3,
    run_prompt_baseline: bool = True,
    cleanup_inline_workflow: bool = False,
) -> DatasetReport:
    """Compatibility shim accepting either an `EvalDataset` list (the new
    shape) or a flat `EvalCase` list (the old shape). Bare-case lists
    are wrapped in a synthetic referenced-mode dataset so the harness
    has a uniform entry point.
    """
    if not cases_or_datasets:
        return await run_evaluation_datasets(
            [], provider=provider, repeats=repeats,
            run_prompt_baseline=run_prompt_baseline,
            cleanup_inline_workflow=cleanup_inline_workflow,
        )
    first = cases_or_datasets[0]
    if isinstance(first, EvalDataset):
        return await run_evaluation_datasets(
            cases_or_datasets, provider=provider, repeats=repeats,
            run_prompt_baseline=run_prompt_baseline,
            cleanup_inline_workflow=cleanup_inline_workflow,
        )
    # Wrap bare cases. Group by `workflow` so cases that share a
    # workflow share a (referenced-mode) dataset.
    by_workflow: dict[str, list[EvalCase]] = {}
    for case in cases_or_datasets:
        by_workflow.setdefault(case.workflow, []).append(case)
    datasets = [
        EvalDataset(name=wf or "ad_hoc", workflow=wf, cases=cs)
        for wf, cs in by_workflow.items()
    ]
    return await run_evaluation_datasets(
        datasets, provider=provider, repeats=repeats,
        run_prompt_baseline=run_prompt_baseline,
        cleanup_inline_workflow=cleanup_inline_workflow,
    )


async def _evaluate_dataset(
    ds: EvalDataset,
    *,
    provider: LLMProvider | None,
    repeats: int,
    acc: _Accumulators,
    run_prompt_baseline: bool,
    cleanup_inline_workflow: bool,
) -> list[dict]:
    """Build (if inline) -> run all cases through Agent modes ->
    optional cleanup. One pass per dataset. Cleanup runs in a
    `finally` so a runner raising mid-loop still tears the inline
    files down when cleanup was requested.
    """
    inline_name: str | None = None
    build_error: str | None = None
    cleaned: list[str] = []

    if ds.is_inline:
        if provider is None:
            build_error = (
                "inline dataset requires a provider for the build step"
            )
        else:
            try:
                inline_name = await build_inline_workflow(
                    ds.workflow_spec, provider,
                )
            except InlineBuildError as e:
                build_error = str(e)

    if provider is None and not build_error:
        # Both Agent modes need a provider; record once at the
        # dataset level so the per-case loop can mark everyone failed.
        build_error = "agent-mode evaluation requires a provider"

    runtime_workflow = inline_name or ds.workflow
    per_case: list[dict] = []
    try:
        for case in ds.cases:
            runtime_case = (
                replace(case, workflow=runtime_workflow)
                if inline_name else case
            )
            per_case.append(await _evaluate_case(
                case=case,
                runtime_case=runtime_case,
                provider=provider,
                repeats=repeats,
                acc=acc,
                build_error=build_error,
                dataset_mode="inline" if ds.is_inline else "referenced",
                workflow_spec=ds.workflow_spec,
                run_prompt_baseline=run_prompt_baseline,
            ))
    finally:
        if inline_name and cleanup_inline_workflow:
            cleaned = _cleanup_inline_workflow(inline_name)

    if ds.is_inline and per_case:
        per_case[0].setdefault("inline", {})
        per_case[0]["inline"].update({
            "dataset": ds.name,
            "workflow_spec": ds.workflow_spec,
            "built_as": inline_name,
            "build_error": build_error,
            "cleaned_paths": cleaned,
            "kept": bool(inline_name) and not cleanup_inline_workflow,
        })
    return per_case


async def _evaluate_case(
    *,
    case: EvalCase,
    runtime_case: EvalCase,
    provider: LLMProvider | None,
    repeats: int,
    acc: _Accumulators,
    build_error: str | None,
    dataset_mode: str,
    workflow_spec: str,
    run_prompt_baseline: bool,
) -> dict:
    """Drive both Agent modes for one case; append metrics to `acc`.

    `case` is the as-authored case (used for scoring + reporting).
    `runtime_case` carries the workflow name the runners should
    target — different from `case.workflow` in inline mode where the
    dataset's generated name is bound in.
    """
    wf_runs: list[AgentRunResult] = []
    pr_runs: list[AgentRunResult] = []
    legacy_run = None
    _legacy_err = build_error

    if not build_error and provider is not None:
        wf_runs = await _run_agent_repeats(
            run_case_agent_with_workflow,
            runtime_case, provider, repeats,
        )
        if run_prompt_baseline:
            pr_runs = await _run_agent_repeats(
                run_case_agent_no_workflow,
                runtime_case, provider, repeats,
                spec=workflow_spec,
            )
        # Third column (§7): the legacy per-step workflow-as-tool driver,
        # run once in isolation. Failures here never abort the case — the
        # column is informational, so we swallow and record the error.
        try:
            legacy_run = await run_case_workflow(runtime_case, provider=provider)
        except Exception as e:  # pragma: no cover - defensive
            legacy_run = None
            _legacy_err = f"{type(e).__name__}: {e}"
        else:
            _legacy_err = legacy_run.error

    # Score the workflow_on side. On any failure that left wf_runs
    # empty, score zero across the declared signals.
    if wf_runs:
        wf_first = wf_runs[0]
        wf_score = score_case(case, [], wf_first.final_text)
        acc.wf_cons.append(measure_consistency(
            case.id,
            [[r.final_text] for r in wf_runs if r.error is None],
            sum(1 for r in wf_runs if r.error is not None),
        ))
    else:
        wf_first = AgentRunResult(
            case_id=case.id, workflow=runtime_case.workflow,
            mode="workflow_on", error=build_error,
        )
        wf_score = score_case(case, [], "")
        acc.wf_cons.append(measure_consistency(case.id, [], failures=repeats))
    acc.wf_acc.append(wf_score)

    pr_first_payload: dict | None = None
    if pr_runs:
        pr_first = pr_runs[0]
        pr_score = score_case(case, [], pr_first.final_text)
        acc.pr_acc.append(pr_score)
        acc.pr_cons.append(measure_consistency(
            case.id,
            [[r.final_text] for r in pr_runs if r.error is None],
            sum(1 for r in pr_runs if r.error is not None),
        ))
        pr_first_payload = {
            "accuracy": asdict(acc.pr_acc[-1]),
            "consistency": asdict(acc.pr_cons[-1]),
            "final_text": pr_first.final_text,
            "tool_calls": pr_first.tool_calls,
            "error": pr_first.error,
            "usage": _usage_payload(pr_first),
        }
    elif build_error and run_prompt_baseline:
        # Keep both summary columns symmetric on build failures.
        acc.pr_acc.append(score_case(case, [], ""))
        acc.pr_cons.append(measure_consistency(case.id, [], failures=repeats))

    return {
        "case_id": case.id,
        "workflow": runtime_case.workflow,
        "description": case.description,
        "mode": dataset_mode,
        "expected": {
            "trace": case.expected.trace,
            "final_state": case.expected.final_state,
            "must_contain": case.expected.must_contain,
        },
        "workflow_run": {
            "accuracy": asdict(wf_score),
            "consistency": asdict(acc.wf_cons[-1]),
            "final_text": wf_first.final_text,
            "tool_calls": wf_first.tool_calls,
            "workflow_invocations": getattr(wf_first, "workflow_invocations", 0),
            "error": wf_first.error,
            # Engine-driven mode: per-purpose token breakdown so the report
            # can compare against the prompt baseline (§7).
            "usage": _usage_payload(wf_first),
        },
        "prompt_run": pr_first_payload,
        # Third column: the legacy per-step workflow-as-tool path, measured
        # standalone. `score` mirrors the must_contain/final_state scoring
        # the other columns use, against the engine's final action text.
        "workflow_as_tool_run": (
            {
                "final_action": legacy_run.final_action,
                "done": legacy_run.done,
                "invocations": legacy_run.invocations,
                "score": asdict(score_case(case, [], legacy_run.final_action)),
                "error": _legacy_err,
            }
            if legacy_run is not None else (
                {"error": _legacy_err} if not build_error else None
            )
        ),
    }


def _usage_payload(run: AgentRunResult) -> dict:
    """Token usage for one agent run, with the per-purpose breakdown that
    backs the §5/§7 cost comparison."""
    return {
        "input_tokens": getattr(run, "input_tokens", 0),
        "output_tokens": getattr(run, "output_tokens", 0),
        "cache_read_tokens": getattr(run, "cache_read_tokens", 0),
        "llm_calls": getattr(run, "llm_calls", 0),
        "by_purpose": getattr(run, "usage_by_purpose", {}) or {},
    }
