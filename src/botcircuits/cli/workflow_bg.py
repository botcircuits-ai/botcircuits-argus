"""Background workflow task registry.

A workflow launched via `/workflow run` (or `run <name>` in chat) runs in a
background asyncio task so the main CLI thread returns to the `you>` prompt
immediately.  When the engine hits a pause point (human_feedback, permission
request, plan_and_confirm) it blocks on the TUISession pause channel; the main
loop surfaces the question as the input prompt and routes the reply back.

Usage
-----
1. Caller creates a `WorkflowTask` and registers it::

       wt = WorkflowTask(session_id="abc123", name="deep_research_assistant")
       REGISTRY.add(wt)

2. Caller launches the coroutine with `asyncio.create_task`, passing
   `wt.context_extras()` into the tool's context dict so `human_feedback`
   (and other pause-aware tools) can reach the pause channel.

3. Inside a background task, calling `wt.pause(question)` delegates to the
   active `TUISession` — the input prompt becomes the question and the task
   blocks until the user types a reply.

The registry is process-wide and intentionally simple.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class WorkflowTask:
    """State carrier for one background workflow execution."""

    name: str
    session_id: str

    # Set when the task's coroutine completes (success, error, or last pause
    # was answered and the engine ran to completion afterwards).
    done: bool = False
    error: Optional[str] = None
    # Final summary line the engine emitted on completion.
    summary: Optional[str] = None

    # The asyncio.Task handle, set by the launcher after create_task().
    task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Channel API used by the human_feedback bridge inside the engine
    # ------------------------------------------------------------------

    async def pause(self, question: str) -> str:
        """Block the background coroutine until the user replies.

        Delegates to the active TUISession so all pause requests (workflow
        human_feedback, plan_and_confirm, permission gates) go through a
        single channel — the input prompt at the bottom of the terminal.

        Falls back to the legacy dual-queue approach when no TUI session is
        active (e.g. non-interactive / piped mode).
        """
        from botcircuits.cli.tui import get_tui_session
        tui = get_tui_session()
        if tui is not None:
            return await tui.pause(question)
        # Legacy fallback for non-TUI (piped) mode: print question, read reply.
        loop = asyncio.get_event_loop()
        from botcircuits.cli.ansi import C, out
        out(C.bold(C.cyan("argus> ")) + question)
        reply: str = await loop.run_in_executor(
            None, lambda: input(C.bold(C.green("you> ")))
        )
        return reply

    def context_extras(self) -> dict:
        """A dict to merge into a tool's `context` arg so `human_feedback`
        (and other pause-aware tools) can reach the pause channel."""
        return {"_workflow_bg": self}


class _Registry:
    """Process-wide registry of running workflow background tasks."""

    def __init__(self) -> None:
        self._tasks: dict[str, WorkflowTask] = {}  # keyed by session_id

    def add(self, wt: WorkflowTask) -> None:
        self._tasks[wt.session_id] = wt

    def remove(self, session_id: str) -> None:
        self._tasks.pop(session_id, None)

    def get(self, session_id: str) -> Optional[WorkflowTask]:
        return self._tasks.get(session_id)

    def active(self) -> list[WorkflowTask]:
        """All tasks that are still running (not done)."""
        return [wt for wt in self._tasks.values() if not wt.done]

    def all_done(self) -> bool:
        return all(wt.done for wt in self._tasks.values())


# Process-wide singleton.
REGISTRY = _Registry()
