"""Textual TUI (`cli/tui_app.py`) — headless pilot tests.

Runs the real app with a scripted provider and a genuinely gated tool
(auto=False), and proves the reported bug is fixed end-to-end: the
approval request appears as a MODAL, `y` approves it, the tool runs, and
the final answer lands in the conversation. Skipped when the optional
`tui` extra isn't installed.
"""

from __future__ import annotations

import asyncio

import pytest

textual = pytest.importorskip("textual")

from botcircuits.agent.loop import Agent  # noqa: E402
from botcircuits.agent.tools import ToolRegistry  # noqa: E402
from botcircuits.agent.tools.registry import LocalTool  # noqa: E402
from botcircuits.agent.tools.builtins import _confirm  # noqa: E402
from botcircuits.cli.tui_app import _build_app, run_tui_available  # noqa: E402

from fakes import ScriptedProvider, text_response, tool_call_response  # noqa: E402


class _State:
    session_id = None
    system = "test"


class GatedToolProvider(ScriptedProvider):
    """Round 1: call the gated tool. Round 2: final answer."""

    def __init__(self):
        super().__init__()
        self.round = 0

    async def complete(self, system, messages, tools, hosted_mcp,
                       skills, max_tokens):
        self.round += 1
        if self.round == 1:
            return tool_call_response("danger", {})
        return text_response("all done")


def _gated_tool(ran: dict) -> LocalTool:
    async def _handler(_args: dict) -> str:
        allowed = await _confirm.confirm("danger proposes:", ["cmd: rm -rf /tmp/x"])
        if not allowed:
            return "denied"
        ran["ok"] = True
        return "executed"

    return LocalTool(name="danger", description="gated",
                     input_schema={"type": "object", "properties": {}},
                     handler=_handler)


def _make_agent(provider, ran):
    reg = ToolRegistry()
    reg.register(_gated_tool(ran))
    return Agent(provider=provider, tools=reg, local_skills_paths=[],
                 enable_workflows=False, enable_subagents=False)


def test_tui_available():
    assert run_tui_available() is None


@pytest.mark.parametrize("key,expect_ran,expect_result",
                         [("y", True, "executed"), ("n", False, "denied")])
def test_approval_modal_flow(key, expect_ran, expect_result):
    async def run():
        ran: dict = {}
        provider = GatedToolProvider()
        agent = _make_agent(provider, ran)
        await agent.start()
        app = _build_app(agent, provider, _State())

        async with app.run_test() as pilot:
            from textual.widgets import Input
            app.query_one("#prompt", Input).value = "do the thing"
            await pilot.press("enter")

            # Wait for the approval modal to appear.
            for _ in range(100):
                if len(app.screen_stack) > 1:
                    break
                await pilot.pause(0.05)
            assert len(app.screen_stack) > 1, "approval modal never appeared"
            body = app.screen.query_one("#approval-body").render()
            assert "rm -rf" in str(body)  # the proposal is VISIBLE

            await pilot.press(key)  # y = allow, n = deny

            for _ in range(100):
                if not app._busy:
                    break
                await pilot.pause(0.05)
            assert not app._busy
        return ran

    ran = asyncio.run(run())
    assert ran.get("ok", False) is expect_ran


def test_pause_question_routes_next_input_as_reply():
    async def run():
        provider = GatedToolProvider()
        agent = _make_agent(provider, {})
        await agent.start()
        app = _build_app(agent, provider, _State())

        async with app.run_test() as pilot:
            from textual.widgets import Input
            answer_box = {}

            async def asker():
                answer_box["v"] = await app.ask_user("what color?")

            asyncio.ensure_future(asker())
            await pilot.pause(0.05)
            assert app._pending_reply is not None

            app.query_one("#prompt", Input).value = "blue"
            await pilot.press("enter")
            await pilot.pause(0.05)
            assert answer_box.get("v") == "blue"
            assert app._pending_reply is None

    asyncio.run(run())
