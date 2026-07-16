"""Build a real Agent and run a single task to completion as one trace.

Used by both the pytest suite (`test_task_completion.py`) and as a CLI
(`python -m evals.harness "<task>"`) for ad-hoc scoring.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import evals.instrument  # noqa: F401  — side-effecting: patches Agent/Registry

from deepeval.tracing import observe, update_current_trace

from botcircuits import Agent, AnthropicProvider, default_registry
from botcircuits.agent.workflow import register_workflows

# Cap workflow drive-loops so a misbehaving run can't spin forever. A 10-step
# workflow needs ~12 chat calls (one per step + the user kickoff); 30 is slack.
_MAX_WORKFLOW_TURNS = 30


def build_provider():
    """The agent-under-test provider. Anthropic by default; override with
    BOTCIRCUITS_EVAL_MODEL. Reads ANTHROPIC_API_KEY from the project .env
    (loaded on `import botcircuits`)."""
    model = os.getenv("BOTCIRCUITS_EVAL_MODEL", "claude-opus-4-7")
    return AnthropicProvider(model=model)


def build_agent(*, enable_workflows: bool = True) -> Agent:
    """Construct an Agent wired the way the eval needs it:

    - all builtins in auto mode (no human at the y/N gate during eval)
    - workflows discovered + registered from .botcircuits/workflows/.build/
    """
    provider = build_provider()
    # auto=True on every gated builtin — there is no interactive stdin here.
    registry = default_registry(
        {
            "shell_exec": {"auto": True},
            "write_file": {"auto": True},
            "edit_file": {"auto": True},
            "shell_stop": {"auto": True},
            "plan_and_confirm": {"auto": True},
        },
        provider=provider,
    )
    if enable_workflows:
        register_workflows(registry, provider=provider, normalize_enabled=True)
    return Agent(provider=provider, tools=registry,
                 enable_workflows=enable_workflows)


async def _drive(agent: Agent, prompt: str) -> str:
    """Run one task. If the first reply leaves a workflow mid-execution, keep
    re-calling on the same session_id until the workflow finishes — all of it
    inside the trace opened by the caller, so the judge sees the whole run."""
    from botcircuits.agent.workflow import active_workflow_names

    text, sid = await agent.chat(prompt)
    turns = 0
    while active_workflow_names(agent.tools) and turns < _MAX_WORKFLOW_TURNS:
        # Empty nudge: the [Active workflow] reminder tells the model to
        # re-call the workflow tool to advance to the next step.
        text, sid = await agent.chat("continue", session_id=sid)
        turns += 1
    return text


async def run_task(prompt: str, *, task: str | None = None,
                   cwd: str | Path | None = None,
                   enable_workflows: bool = True) -> str:
    """Execute `prompt` end to end as a single DeepEval trace and return the
    final assistant text.

    `task` sets the explicit task description on the trace for Task Completion
    (omit to let the metric infer it from the trace). `cwd` runs the task in a
    different working directory so file-writing tasks don't litter the repo.
    """

    @observe(name="task")
    async def _root():
        if task:
            update_current_trace(input=prompt, metadata={"task": task})
        else:
            update_current_trace(input=prompt)
        async with build_agent(enable_workflows=enable_workflows) as agent:
            out = await _drive(agent, prompt)
        update_current_trace(output=out)
        return out

    if cwd is None:
        return await _root()

    prev = os.getcwd()
    os.chdir(cwd)
    try:
        return await _root()
    finally:
        os.chdir(prev)


def main() -> None:
    if len(sys.argv) < 2:
        print('usage: python -m evals.harness "<task prompt>"', file=sys.stderr)
        raise SystemExit(2)
    prompt = sys.argv[1]
    out = asyncio.run(run_task(prompt))
    print("\n=== final ===")
    print(out)


if __name__ == "__main__":
    main()
