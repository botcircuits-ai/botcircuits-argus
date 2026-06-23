"""Task Completion + Tool Correctness over the seed tasks.

Run with DeepEval's runner (it sets up tracing + judge plumbing):

    deepeval test run evals/test_task_completion.py

Or run this file directly to score every task and print the dataset summary:

    python -m evals.test_task_completion

The judge model defaults to OpenAI (OPENAI_API_KEY). Override per-metric with
the `model=` arg or globally via `deepeval set-azure-openai` / config.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import evals.instrument  # noqa: F401  — patches Agent/Registry before any run

from deepeval.dataset import EvaluationDataset, Golden
from deepeval.metrics import TaskCompletionMetric

from evals.harness import run_task
from evals.tasks import TASKS

# Map each Golden back to the seed Task so the runner can set cwd + task text.
_BY_PROMPT = {t.prompt: t for t in TASKS}


def _judge_model() -> str:
    return os.getenv("DEEPEVAL_JUDGE_MODEL", "gpt-4o")


async def _run_golden(golden: Golden) -> None:
    """Drive one task. File-writing / workflow tasks run in a throwaway cwd so
    they don't litter the repo; the .botcircuits/workflows dir is found via an
    absolute env path, not cwd, so workflows still resolve."""
    task = _BY_PROMPT[golden.input]
    if task.needs_workflow:
        # Point the loader at the repo's built workflows even though we run in
        # a temp cwd (file writes land in the temp dir, workflow defs come from
        # the repo).
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        os.environ["BOTCIRCUITS_WORKFLOWS_DIR"] = os.path.join(
            repo, ".botcircuits", "workflows")
        with tempfile.TemporaryDirectory() as tmp:
            await run_task(task.prompt, task=task.task, cwd=tmp)
    else:
        await run_task(task.prompt, task=task.task)


def build_dataset() -> EvaluationDataset:
    return EvaluationDataset(
        goldens=[Golden(input=t.prompt) for t in TASKS]
    )


def run() -> None:
    """Score every seed task with Task Completion via evals_iterator."""
    dataset = build_dataset()
    metric = TaskCompletionMetric(
        threshold=0.7,
        model=_judge_model(),
        include_reason=True,
    )
    # evals_iterator yields each golden; running the observed task inside the
    # loop body produces one trace per golden, which the iterator scores.
    for golden in dataset.evals_iterator(metrics=[metric]):
        asyncio.run(_run_golden(golden))


# ---- pytest entry --------------------------------------------------------
# `deepeval test run` collects `test_*` functions. One parametrized test per
# seed task keeps per-task pass/fail visible in the report.

try:
    import pytest

    @pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
    def test_task_completion(task):
        from deepeval import assert_test
        from deepeval.test_case import LLMTestCase

        # Run the task, capture the final output, and judge the resulting
        # trace. We pass the explicit task text so scoring doesn't depend on
        # the judge inferring intent from a possibly-terse trace.
        async def _go():
            if task.needs_workflow:
                repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                os.environ["BOTCIRCUITS_WORKFLOWS_DIR"] = os.path.join(
                    repo, ".botcircuits", "workflows")
                with tempfile.TemporaryDirectory() as tmp:
                    return await run_task(task.prompt, task=task.task, cwd=tmp)
            return await run_task(task.prompt, task=task.task)

        output = asyncio.run(_go())
        metric = TaskCompletionMetric(
            threshold=0.7, model=_judge_model(), task=task.task,
        )
        assert_test(
            LLMTestCase(input=task.prompt, actual_output=output),
            [metric],
        )

except ImportError:  # pytest not installed — `run()` still works standalone
    pass


if __name__ == "__main__":
    run()
