"""Orchestration — plan a task into steps, run them with checkpoints.

A single model turn is not a workflow. The orchestrator plans a task
into 2-4 imperative steps, drives them in order through a worker agent,
gates each step behind an approval callback, and retries on failure —
work moving through time with checkpoints, not one shot.

It composes the `Agent` without touching it: a thin planner plus driver
wrapped around the existing loop. This is the *lightweight* end of the
spectrum — for durable, branching, deterministic multi-step processes,
BotCircuits workflows (`agent/workflow/`) are the heavyweight sibling:
there the state machine (not a plan string) owns advancement.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Awaitable, Callable, Union

from botcircuits.providers.base import LLMProvider
from botcircuits.types import Message
from botcircuits.agent.tools import ToolRegistry

_PLANNER = (
    "You are a planner. Break the task into 2-4 short imperative steps. "
    "Return ONLY a JSON array of step strings, nothing else."
)

WORKER_SYSTEM = "Execute each step using tools when needed. Be concise."

#: Retries per step before recording the failure and moving on.
STEP_ATTEMPTS = 2

#: An approval gate: sync or async callable, step -> run it?
ApproveFn = Callable[[str], Union[bool, Awaitable[bool]]]


@dataclass
class OrchestratorResult:
    plan: list[str]
    results: list[str]
    final: str


class Orchestrator:
    """Plan → gate → execute → retry, over a fresh worker agent.

    The worker shares the caller's provider and tool registry but runs
    its own conversation, with workflows and subagent spawning disabled —
    a focused executor, not a second full agent.
    """

    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry | None = None,
        max_tokens: int = 4096,
    ) -> None:
        self.provider = provider
        self.tools = tools or ToolRegistry()
        self.max_tokens = max_tokens

    async def plan(self, task: str) -> list[str]:
        """Ask the model for a 2-4 step plan; fall back to the whole task
        as one step when the reply isn't a parseable JSON array."""
        self.provider.usage_purpose = "plan"
        resp = await self.provider.complete(
            system=_PLANNER,
            messages=[Message(role="user",
                              blocks=[{"type": "text", "text": task}])],
            tools=[], hosted_mcp=[], skills=[],
            max_tokens=400,
        )
        text = resp.text.strip()
        try:
            arr = json.loads(text[text.index("["): text.rindex("]") + 1])
            steps = [str(s) for s in arr if str(s).strip()]
            if steps:
                return steps
        except (ValueError, json.JSONDecodeError):
            pass
        return [task]  # fallback: treat the whole task as one step

    async def run(self, task: str,
                  approve: ApproveFn | None = None) -> OrchestratorResult:
        """Plan the task, then drive the steps in order through one worker
        session (so later steps see earlier steps' results). `approve`,
        when given, gates each step; a rejected step is recorded as
        skipped, not silently dropped."""
        from botcircuits.agent.loop import Agent  # lazy: avoids an import cycle

        plan = await self.plan(task)
        worker = Agent(
            provider=self.provider,
            tools=self.tools,
            local_skills_paths=[],
            max_tokens=self.max_tokens,
            enable_workflows=False,
            enable_subagents=False,
        )
        await worker.start()

        results: list[str] = []
        session_id: str | None = None
        for step in plan:
            allowed = True
            if approve is not None:
                verdict = approve(step)
                if hasattr(verdict, "__await__"):
                    verdict = await verdict
                allowed = bool(verdict)
            if not allowed:
                results.append(f"[skipped] {step}")
                continue
            result, session_id = await self._run_with_retry(
                worker, step, session_id)
            results.append(result)
        return OrchestratorResult(
            plan=plan, results=results,
            final=results[-1] if results else "",
        )

    @staticmethod
    async def _run_with_retry(worker, step: str, session_id: str | None,
                              attempts: int = STEP_ATTEMPTS) -> tuple[str, str | None]:
        last = ""
        for _ in range(attempts):
            try:
                reply, session_id = await worker.chat(
                    step, session_id=session_id, system=WORKER_SYSTEM)
                return reply, session_id
            except Exception as exc:  # noqa: BLE001 — retry on any execution failure
                last = f"error: {exc}"
        return last, session_id
