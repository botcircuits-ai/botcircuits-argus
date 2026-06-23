"""Seed tasks for the Task Completion suite.

Each task is (id, prompt, task_description). `task_description` is the explicit
goal handed to TaskCompletionMetric; keep it outcome-focused (what success
looks like), not a list of steps.

Keep this list small — every task is a full agent run plus a judge call.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    id: str
    prompt: str
    task: str
    needs_workflow: bool = False  # drives a multi-turn workflow to completion


TASKS: list[Task] = [
    Task(
        id="arithmetic",
        prompt="Add 17 and 25, then tell me the result.",
        task="Compute 17 + 25 and report the correct sum (42) to the user.",
    ),
    Task(
        id="read_file",
        prompt="How many dependencies are listed in pyproject.toml? "
               "Read the file to answer.",
        task="Read pyproject.toml and report the number of entries in the "
             "[project] dependencies list.",
    ),
    Task(
        id="workflow_demo",
        prompt="Run the workflow_demo workflow and stop at step 3.",
        task="Drive the workflow_demo workflow to completion: it should create "
             "step_1.md, step_2.md and step_3.md, terminate early at step 3, "
             "and write end.md containing END.",
        needs_workflow=True,
    ),
]
