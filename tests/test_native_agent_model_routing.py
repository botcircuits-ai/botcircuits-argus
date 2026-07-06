"""Native runtime honors a workflow step's per-agent `model` binding.

Under the in-process (native) runtime, a segment pinned to a named agent
(`current["agent"]`) must be able to run on that agent's own LLM
provider/model instead of the run's default — the same per-agent routing
the MultiplexRuntime gives the CLI runtimes, but resolved in-process.

These tests pin the seam that previously crashed: the engine passes
`agent=<name>` into the native `run_segment` callback, so that callback MUST
accept `agent` and translate it into a provider override (or fall back to the
default when the binding names something we can't build in-process, e.g. a
claude-code CLI alias).
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

import botcircuits.agent.workflow.local as wf_local
from botcircuits.agent.core import Agent
from botcircuits.agent.tools import ToolRegistry
from botcircuits.agent.workflow import collect_agents_config
from botcircuits.providers.base import LLMProvider
from botcircuits.types import LLMResponse


class _StubProvider(LLMProvider):
    """Minimal provider; identity is its `model` so we can assert which one
    a segment actually used."""

    name = "stub"

    def __init__(self, model: str):
        self.model = model
        self.usage_input_tokens = 0
        self.usage_output_tokens = 0
        self.usage_llm_calls = 0

    async def complete(self, *a, **k) -> LLMResponse:
        return LLMResponse(text="", tool_calls=[], stop_reason="end_turn", raw=None)

    async def stream(self, *a, **k):
        yield ("final", await self.complete())


def _agent(agents_config: dict) -> Agent:
    return Agent(
        provider=_StubProvider("default-model"),
        tools=ToolRegistry(),
        enable_workflows=True,
        local_skills_paths=[],
        agents_config=agents_config,
    )


def test_runner_accepts_agent_kwarg():
    """Regression: the engine calls run_segment(agent=...) for pinned steps.
    The native runner must accept it (previously TypeError)."""
    agent = _agent({})
    runner = agent._make_segment_runner()
    assert "agent" in inspect.signature(runner).parameters


def test_resolve_explicit_in_process_provider_switches():
    agent = _agent({"fast": {"provider": "openai", "model": "gpt-4.1"}})
    p = agent._resolve_segment_provider("fast")
    assert p is not None
    assert p.model == "gpt-4.1"
    assert type(p).__name__ == "OpenAIProvider"


def test_resolve_provider_is_cached():
    agent = _agent({
        "a": {"provider": "openai", "model": "gpt-4.1"},
        "b": {"provider": "openai", "model": "gpt-4.1"},
    })
    assert agent._resolve_segment_provider("a") is agent._resolve_segment_provider("b")


def test_cli_runtime_alias_falls_back_to_default():
    """A claude-code binding (runtime + CLI model alias, no in-process
    `provider`) must NOT switch vendors — native never spawns the CLI here."""
    agent = _agent({"cc": {"runtime": "claude-code", "model": "sonnet-4.6"}})
    assert agent._resolve_segment_provider("cc") is None


def test_unknown_provider_name_falls_back_to_default():
    """`make_provider` maps unknown names to Anthropic; the resolver must
    instead fall back to the default rather than silently switch vendors."""
    agent = _agent({"x": {"provider": "totally-unknown", "model": "m"}})
    assert agent._resolve_segment_provider("x") is None


def test_unpinned_and_unknown_agent_return_default():
    agent = _agent({"fast": {"provider": "openai", "model": "gpt-4.1"}})
    assert agent._resolve_segment_provider(None) is None
    assert agent._resolve_segment_provider("not-declared") is None


def test_collect_agents_config_merges_workflow_agents(tmp_path, monkeypatch):
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    build = tmp_path / ".build"
    build.mkdir(parents=True, exist_ok=True)
    record = {
        "name": "wf_pinned",
        "description": "d",
        "flow": {"start": "start", "steps": {"start": {"type": "start"}}},
        "agents": {
            "claude-backend": {"runtime": "claude-code", "model": "haiku-4.5"},
            "oai": {"provider": "openai", "model": "gpt-4.1"},
        },
    }
    (build / "wf_pinned.json").write_text(json.dumps(record), encoding="utf-8")

    merged = asyncio.run(collect_agents_config())
    assert merged["claude-backend"]["model"] == "haiku-4.5"
    assert merged["oai"]["provider"] == "openai"
