"""Verification (`agent/verification.py`) — no receipt, no acceptance.

Covers the standalone oracle (run_python, nonce-hardened), the AGENTS.md
`## Testing` convention, the transcript checks, and the enforced-run gate
in the agent loop: a code-changing turn is not accepted until a real
passing shell_exec run of the declared test command is observed.
"""

from __future__ import annotations

import asyncio
import json

from botcircuits.agent.loop import Agent
from botcircuits.agent.tools import ToolRegistry
from botcircuits.agent.tools.registry import LocalTool
from botcircuits.agent.verification import (
    changed_code,
    extract_code,
    observed_pass,
    run_python,
    test_command,
)
from botcircuits.providers.base import LLMProvider
from botcircuits.types import LLMResponse, Message, ToolCall


# -- standalone oracle --------------------------------------------------------


def test_extract_code_pulls_fenced_block():
    text = "Here you go:\n```python\ndef f():\n    return 1\n```\ndone"
    assert extract_code(text) == "def f():\n    return 1"
    assert extract_code("plain code") == "plain code"


def test_run_python_pass_and_fail():
    ok = run_python("def add(a, b):\n    return a + b", "assert add(2, 3) == 5")
    assert ok.passed
    bad = run_python("def add(a, b):\n    return a - b", "assert add(2, 3) == 5")
    assert not bad.passed
    assert "AssertionError" in bad.output


def test_run_python_early_exit_cannot_forge_a_pass():
    # Printing something and exiting 0 before the check must not pass:
    # success needs the per-run nonce, printed only after the check.
    forged = run_python(
        "import sys\nprint('VERIFICATION_OK')\nsys.exit(0)",
        "assert False",
    )
    assert not forged.passed


# -- AGENTS.md test command ----------------------------------------------------


def test_test_command_parses_testing_block(tmp_path):
    (tmp_path / "AGENTS.md").write_text(
        "# proj\n\n## Testing\n\n```\nuv run pytest -q\n```\n")
    assert test_command(tmp_path) == "uv run pytest -q"


def test_test_command_none_without_file_or_heading(tmp_path):
    assert test_command(tmp_path) is None
    (tmp_path / "AGENTS.md").write_text("# proj\nno testing section\n")
    assert test_command(tmp_path) is None


# -- transcript checks ----------------------------------------------------------


def _tool_call_msg(name: str, args: dict, call_id: str = "t1") -> Message:
    return Message(role="assistant", blocks=[
        {"type": "tool_call", "id": call_id, "name": name, "arguments": args},
    ])


def _tool_result_msg(call_id: str, content: dict, is_error=False) -> Message:
    return Message(role="user", blocks=[
        {"type": "tool_result", "tool_call_id": call_id, "name": "shell_exec",
         "content": json.dumps(content), "is_error": is_error},
    ])


def test_changed_code_triggers_only_on_code_extensions():
    code = [_tool_call_msg("write_file", {"path": "src/x.py"})]
    prose = [_tool_call_msg("write_file", {"path": "notes.txt"})]
    assert changed_code(code, 0)
    assert not changed_code(prose, 0)


def test_observed_pass_pairs_run_to_exit_code():
    cmd = "pytest -q"
    ran = _tool_call_msg("shell_exec", {"argv": ["pytest", "-q"]}, "s1")
    passed = _tool_result_msg("s1", {"exit_code": 0, "stdout": "ok"})
    failed = _tool_result_msg("s1", {"exit_code": 1, "stdout": "boom"},
                              is_error=True)
    assert observed_pass([ran, passed], 0, cmd)
    assert not observed_pass([ran, failed], 0, cmd)
    assert not observed_pass([passed], 0, cmd)  # a result with no matching run


# -- the enforced-run gate in the loop ------------------------------------------


class GatedProvider(LLMProvider):
    """Turn 1: writes code and claims done. After a nudge: runs the test,
    then claims done again. Proves the gate injects the nudge and accepts
    only after an observed pass."""

    name = "scripted"
    model = "test"

    def __init__(self):
        self.step = 0

    async def complete(self, system, messages, tools, hosted_mcp,
                       skills, max_tokens) -> LLMResponse:
        self.step += 1
        if self.step == 1:  # write code, no verification
            return LLMResponse(text="", stop_reason="tool_use", raw=None,
                               tool_calls=[ToolCall(id="w1", name="write_file",
                                                    arguments={"path": "x.py"})])
        if self.step == 2:  # claim done without running the test
            return LLMResponse(text="done, it works", tool_calls=[],
                               stop_reason="end_turn", raw=None)
        if self.step == 3:  # the nudge arrives — run the test
            assert "passing run" in messages[-1].blocks[0]["text"]
            return LLMResponse(text="", stop_reason="tool_use", raw=None,
                               tool_calls=[ToolCall(id="s1", name="shell_exec",
                                                    arguments={"argv": ["pytest", "-q"]})])
        return LLMResponse(text="verified done", tool_calls=[],
                           stop_reason="end_turn", raw=None)

    async def stream(self, system, messages, tools, hosted_mcp,
                     skills, max_tokens):
        yield "final", await self.complete(system, messages, tools,
                                           hosted_mcp, skills, max_tokens)

    async def aclose(self):
        pass


def _fake_tools() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(LocalTool(name="write_file", description="t",
                           input_schema={"type": "object", "properties": {}},
                           handler=lambda _a: "written"))
    reg.register(LocalTool(name="shell_exec", description="t",
                           input_schema={"type": "object", "properties": {}},
                           handler=lambda _a: {"exit_code": 0, "stdout": "1 passed"}))
    return reg


def _agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text("## Testing\n```\npytest -q\n```\n")


def test_gate_demands_and_accepts_observed_pass(tmp_path):
    _agents_md(tmp_path)

    async def run():
        agent = Agent(provider=GatedProvider(), tools=_fake_tools(),
                      local_skills_paths=[], enable_workflows=False,
                      enable_subagents=False, agents_dir=tmp_path)
        return await agent.chat("fix the bug")

    reply, _sid = asyncio.run(run())
    assert reply == "verified done"


def test_gate_enforced_on_streaming_path_too(tmp_path):
    _agents_md(tmp_path)

    async def run():
        agent = Agent(provider=GatedProvider(), tools=_fake_tools(),
                      local_skills_paths=[], enable_workflows=False,
                      enable_subagents=False, agents_dir=tmp_path)
        done = None
        async for ev in agent.chat_stream("fix the bug"):
            if ev.type == "done":
                done = ev.text
        return done

    assert asyncio.run(run()) == "verified done"


def test_gate_absent_without_agents_md(tmp_path):
    class NoRunProvider(GatedProvider):
        pass

    async def run():
        agent = Agent(provider=NoRunProvider(), tools=_fake_tools(),
                      local_skills_paths=[], enable_workflows=False,
                      enable_subagents=False, agents_dir=tmp_path)
        return await agent.chat("fix the bug")

    reply, _sid = asyncio.run(run())
    assert reply == "done, it works"  # no declared command -> no gate


def test_gate_opt_out_with_require_run_false(tmp_path):
    _agents_md(tmp_path)

    async def run():
        agent = Agent(provider=GatedProvider(), tools=_fake_tools(),
                      local_skills_paths=[], enable_workflows=False,
                      enable_subagents=False, agents_dir=tmp_path,
                      require_run=False)
        return await agent.chat("fix the bug")

    reply, _sid = asyncio.run(run())
    assert reply == "done, it works"


def test_gate_gives_up_after_verify_attempts(tmp_path):
    _agents_md(tmp_path)

    class StubbornProvider(LLMProvider):
        """Writes code, then keeps claiming done without ever running tests."""
        name = "scripted"
        model = "test"

        def __init__(self):
            self.claims = 0

        async def complete(self, system, messages, tools, hosted_mcp,
                           skills, max_tokens) -> LLMResponse:
            if self.claims == 0 and not any(
                    b.get("type") == "tool_call"
                    for m in messages for b in m.blocks):
                return LLMResponse(text="", stop_reason="tool_use", raw=None,
                                   tool_calls=[ToolCall(id="w1", name="write_file",
                                                        arguments={"path": "x.py"})])
            self.claims += 1
            return LLMResponse(text="trust me", tool_calls=[],
                               stop_reason="end_turn", raw=None)

        async def stream(self, system, messages, tools, hosted_mcp,
                         skills, max_tokens):
            yield "final", await self.complete(system, messages, tools,
                                               hosted_mcp, skills, max_tokens)

        async def aclose(self):
            pass

    async def run():
        provider = StubbornProvider()
        agent = Agent(provider=provider, tools=_fake_tools(),
                      local_skills_paths=[], enable_workflows=False,
                      enable_subagents=False, agents_dir=tmp_path,
                      verify_attempts=2)
        reply, _sid = await agent.chat("fix the bug")
        return reply, provider.claims

    reply, claims = asyncio.run(run())
    assert reply == "trust me"   # attempts exhausted -> last reply returned
    assert claims == 3           # initial claim + 2 nudged retries
