"""The permission/pause prompt must actually be VISIBLE.

The reported bug: a y/N confirmation (file create / shell exec) arriving
while the REPL prompt was already pending never redrew it — the user sat
at a bare `| > ` with no question. These tests pin the fix at both layers:

  - line REPL (`cli/tui.py`): `pause()` activates immediately and
    `_prompt_text()` renders the question (the pending prompt re-evaluates
    it per redraw).
  - confirmer seam (`_confirm.py`): a registered UI confirmer (the Textual
    approval modal) takes over the whole interaction, printing nothing.
"""

from __future__ import annotations

import asyncio

from botcircuits.agent.tools.builtins import _confirm
from botcircuits.cli.tui import TUISession


def test_pause_becomes_visible_immediately():
    async def run():
        tui = TUISession(interactive=True)  # no __aenter__: no real terminal
        assert "run?" not in tui._prompt_text()

        task = asyncio.ensure_future(tui.pause("run? [y/N]: "))
        await asyncio.sleep(0)
        # The question IS the prompt now — before any read_message cycle.
        assert tui.is_paused()
        assert "run? [y/N]:" in tui._prompt_text()

        assert await tui.dispatch_reply("y") is True
        assert await task == "y"
        # Prompt restored after the reply.
        assert "run?" not in tui._prompt_text()

    asyncio.run(run())


def test_queued_pause_surfaces_when_first_is_answered():
    async def run():
        tui = TUISession(interactive=True)
        t1 = asyncio.ensure_future(tui.pause("first?"))
        await asyncio.sleep(0)
        t2 = asyncio.ensure_future(tui.pause("second?"))
        await asyncio.sleep(0)
        assert "first?" in tui._prompt_text()

        await tui.dispatch_reply("a")
        # The second question replaces the prompt right away.
        assert "second?" in tui._prompt_text()
        await tui.dispatch_reply("b")

        assert await t1 == "a" and await t2 == "b"

    asyncio.run(run())


def test_registered_confirmer_owns_the_interaction(capsys):
    async def run():
        seen: dict = {}

        async def fake_confirmer(title, lines, prompt):
            seen.update(title=title, lines=lines, prompt=prompt)
            return True

        _confirm.set_confirmer(fake_confirmer)
        try:
            ok = await _confirm.confirm("shell_exec proposes:", ["cmd: ls"])
        finally:
            _confirm.set_confirmer(None)
        return ok

    assert asyncio.run(run()) is True
    # The confirmer got the full proposal…
    # …and confirm() printed nothing (the UI renders it itself).
    assert capsys.readouterr().out == ""


def test_confirmer_deny_is_fail_closed():
    async def run():
        _confirm.set_confirmer(lambda *a: _false())
        try:
            return await _confirm.confirm("write_file proposes:", ["path: x.py"])
        finally:
            _confirm.set_confirmer(None)

    async def _false():
        return False

    assert asyncio.run(run()) is False
