"""Workflow evaluation framework.

Validates the working hypothesis behind the workflow module:

  Structured, rule-driven tasks are more accurate AND more consistent
  when driven by an executable state machine than when expressed as
  prose steps the LLM has to re-derive every turn — and they cost
  fewer tokens because the steps don't have to be repeated in-context.

The framework runs a dataset of cases through two runners on the SAME
inputs and compares them:

  - `runner_workflow` drives the actual STM engine (deterministic).
  - `runner_prompt`   inlines the STM into a system prompt and asks
                      the LLM to drive itself (the non-workflow
                      baseline).

The two are scored on identical accuracy criteria (trace match, final
state, must-contain assertions) and on a consistency metric measured
by repeating each case N times.

Public entry points:

    from botcircuits.agent.workflow.evaluation import (
        run_evaluation, discover_cases, load_cases, render_text,
        write_json_report,
    )
"""

from __future__ import annotations

from botcircuits.agent.workflow.evaluation.dataset import (
    DEFAULT_EVAL_DIR,
    EVAL_DIR_ENV,
    EvalCase,
    EvalDataset,
    EvalDatasetError,
    EvalExpected,
    EvalTurn,
    discover_cases,
    discover_datasets,
    load_cases,
    load_dataset,
    resolve_eval_dir,
)
from botcircuits.agent.workflow.evaluation.harness import run_evaluation, run_evaluation_datasets
from botcircuits.agent.workflow.evaluation.inline_build import (
    InlineBuildError,
    build_inline_workflow,
    cleanup_inline_workflow,
    generate_build_payload,
)
from botcircuits.agent.workflow.evaluation.metrics import (
    CaseAccuracy,
    ConsistencyResult,
    DatasetReport,
    RunnerSummary,
    measure_consistency,
    score_case,
    summarize_runner,
)
from botcircuits.agent.workflow.evaluation.report import render_text, write_json_report
from botcircuits.agent.workflow.evaluation.coding_dataset import (
    CodingCase,
    CodingDataset,
    discover_coding_datasets,
    load_coding_dataset,
)
from botcircuits.agent.workflow.evaluation.coding_metrics import (
    CodingReport,
    ModeSummary,
    run_coding_evaluation,
)
from botcircuits.agent.workflow.evaluation.coding_report import (
    render_coding_report,
    write_coding_report,
)
from botcircuits.agent.workflow.evaluation.coding_runner import (
    MODES as CODING_MODES,
    CodingRunResult,
    run_coding_case,
)
from botcircuits.agent.workflow.evaluation.runner_agent import (
    AgentRunResult,
    run_case_agent_no_workflow,
    run_case_agent_with_workflow,
)
from botcircuits.agent.workflow.evaluation.runner_prompt import PromptRunResult, run_case_prompt
from botcircuits.agent.workflow.evaluation.runner_workflow import WorkflowRunResult, run_case_workflow

__all__ = [
    "DEFAULT_EVAL_DIR",
    "EVAL_DIR_ENV",
    "EvalCase",
    "EvalDataset",
    "EvalDatasetError",
    "EvalExpected",
    "EvalTurn",
    "AgentRunResult",
    "CaseAccuracy",
    "ConsistencyResult",
    "DatasetReport",
    "InlineBuildError",
    "RunnerSummary",
    "PromptRunResult",
    "WorkflowRunResult",
    "build_inline_workflow",
    "cleanup_inline_workflow",
    "discover_cases",
    "discover_datasets",
    "generate_build_payload",
    "load_cases",
    "load_dataset",
    "resolve_eval_dir",
    "measure_consistency",
    "score_case",
    "summarize_runner",
    "render_text",
    "write_json_report",
    "run_evaluation",
    "run_evaluation_datasets",
    "run_case_agent_no_workflow",
    "run_case_agent_with_workflow",
    "run_case_prompt",
    "run_case_workflow",
    "CodingCase",
    "CodingDataset",
    "CodingReport",
    "CodingRunResult",
    "ModeSummary",
    "CODING_MODES",
    "discover_coding_datasets",
    "load_coding_dataset",
    "run_coding_case",
    "run_coding_evaluation",
    "render_coding_report",
    "write_coding_report",
]
