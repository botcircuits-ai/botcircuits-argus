"""Orchestration (`agent/orchestration.py`) — plan, gate, execute, retry.

The orchestrator composes the Agent without touching it: a planner LLM
call splits the task into steps, a fresh worker agent runs them in order
(one session, so later steps see earlier results), an approval callback
gates each step, and a failing step is retried before its error is
recorded.
"""

from __future__ import annotations

import asyncio

from botcircuits.agent.orchestration import Orchestrator
from botcircuits.providers.base import LLMProvider
from botcircuits.types import LLMResponse


class ScriptedProvider(LLMProvider):
    """Plays back one canned LLMResponse per call, in order."""

    name = "scripted"
    model = "test"

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls: list[str] = []

    async def complete(self, system, messages, tools, hosted_mcp,
                       skills, max_tokens) -> LLMResponse:
        self.calls.append(messages[-1].blocks[0]["text"])
        return LLMResponse(text=self.responses.pop(0), tool_calls=[],
                           stop_reason="end_turn", raw=None)

    async def stream(self, system, messages, tools, hosted_mcp,
                     skills, max_tokens):
        resp = await self.complete(system, messages, tools, hosted_mcp,
                                   skills, max_tokens)
        yield "final", resp

    async def aclose(self):
        pass


def test_plan_parses_json_array():
    provider = ScriptedProvider(['["read the file", "fix the bug"]'])
    steps = asyncio.run(Orchestrator(provider).plan("fix it"))
    assert steps == ["read the file", "fix the bug"]


def test_plan_falls_back_to_whole_task():
    provider = ScriptedProvider(["I would suggest starting by..."])
    steps = asyncio.run(Orchestrator(provider).plan("fix it"))
    assert steps == ["fix it"]


def test_run_executes_steps_in_one_worker_session():
    provider = ScriptedProvider([
        '["step one", "step two"]',  # planner
        "did one",                   # worker, step 1
        "did two",                   # worker, step 2
    ])
    result = asyncio.run(Orchestrator(provider).run("task"))
    assert result.plan == ["step one", "step two"]
    assert result.results == ["did one", "did two"]
    assert result.final == "did two"
    # Both steps went through the worker after the plan call.
    assert provider.calls == ["task", "step one", "step two"]


def test_approval_gate_skips_rejected_steps():
    provider = ScriptedProvider([
        '["safe step", "scary step"]',
        "done safe",  # only the approved step reaches the worker
    ])
    result = asyncio.run(
        Orchestrator(provider).run("task", approve=lambda s: "scary" not in s))
    assert result.results == ["done safe", "[skipped] scary step"]


def test_step_failure_is_retried_then_recorded():
    class FlakyProvider(ScriptedProvider):
        def __init__(self):
            super().__init__(['["only step"]'])
            self.worker_calls = 0

        async def complete(self, system, messages, tools, hosted_mcp,
                           skills, max_tokens):
            if self.responses:  # planner call
                return await super().complete(system, messages, tools,
                                              hosted_mcp, skills, max_tokens)
            self.worker_calls += 1
            raise RuntimeError("provider down")

    provider = FlakyProvider()
    result = asyncio.run(Orchestrator(provider).run("task"))
    assert provider.worker_calls == 2  # retried once
    assert result.results == ["error: provider down"]
