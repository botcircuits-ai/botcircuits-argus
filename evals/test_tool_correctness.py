"""Tool Correctness — verify the workflow guardrail forces the right tool.

Unlike Task Completion, Tool Correctness is *reference-based*: you assert which
tools the agent should have called. Here we use it to check the guardrail
described in IMPLEMENTATION.md §5.4 / §8.6 — when the user's request matches a
workflow, the model MUST call that workflow tool as its first action rather
than improvising with shell/write_file.

We don't need DeepEval tracing for this one: we observe the calls directly by
sniffing ToolRegistry.run, build an LLMTestCase with tools_called /
expected_tools, and let ToolCorrectnessMetric judge.

Run:  deepeval test run evals/test_tool_correctness.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from botcircuits import Agent, AnthropicProvider, default_registry
from botcircuits.agent.tools.registry import ToolRegistry
from botcircuits.agent.workflow import register_workflows, active_workflow_names

_WORKFLOW = "workflow_demo"


async def _run_and_record(prompt: str) -> list[str]:
    """Run a task, returning the ordered list of tool names actually called."""
    provider = AnthropicProvider(
        model=os.getenv("BOTCIRCUITS_EVAL_MODEL", "claude-opus-4-7"))
    registry = default_registry(
        {"write_file": {"auto": True}, "shell_exec": {"auto": True}},
        provider=provider,
    )
    register_workflows(registry, provider=provider, normalize_enabled=True)

    called: list[str] = []
    orig = ToolRegistry.run

    async def sniff(self, name, args, context=None):
        called.append(name)
        return await orig(self, name, args, context)

    ToolRegistry.run = sniff
    try:
        async with Agent(provider=provider, tools=registry) as agent:
            text, sid = await agent.chat(prompt)
            turns = 0
            while active_workflow_names(agent.tools) and turns < 30:
                text, sid = await agent.chat("continue", session_id=sid)
                turns += 1
        return called
    finally:
        ToolRegistry.run = orig


def test_workflow_guardrail_calls_workflow_tool():
    """The matching workflow tool must be the FIRST tool called."""
    from deepeval import assert_test
    from deepeval.metrics import ToolCorrectnessMetric
    from deepeval.test_case import LLMTestCase, ToolCall

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.environ["BOTCIRCUITS_WORKFLOWS_DIR"] = os.path.join(
        repo, ".botcircuits", "workflows")

    prompt = "Run the workflow_demo workflow and stop at step 2."
    with tempfile.TemporaryDirectory() as tmp:
        prev = os.getcwd()
        os.chdir(tmp)
        try:
            called = asyncio.run(_run_and_record(prompt))
        finally:
            os.chdir(prev)

    # Sanity: the guardrail's whole point is that the workflow tool is invoked
    # before any improvised shell/write_file call.
    assert _WORKFLOW in called, (
        f"workflow tool '{_WORKFLOW}' never called; tools were: {called}")
    assert called[0] == _WORKFLOW, (
        f"workflow tool was not the first action; order was: {called}")

    # And express it as a Tool Correctness assertion for the DeepEval report.
    test_case = LLMTestCase(
        input=prompt,
        actual_output="(workflow run)",
        tools_called=[ToolCall(name=n) for n in called],
        expected_tools=[ToolCall(name=_WORKFLOW)],
    )
    assert_test(test_case, [ToolCorrectnessMetric(threshold=0.5)])


if __name__ == "__main__":
    test_workflow_guardrail_calls_workflow_tool()
    print("ok")
