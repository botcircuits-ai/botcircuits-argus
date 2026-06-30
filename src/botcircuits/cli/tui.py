"""Terminal UI session — concurrent input + streaming output.

Uses prompt_toolkit's `patch_stdout` to keep the input prompt pinned at the
bottom of the terminal while all background tasks, tool calls, and LLM streams
print freely above it via normal `print()` / `out()` calls.

The public API is a single `TUISession` object:

    async with TUISession(interactive=True) as tui:
        while True:
            msg = await tui.read_message()   # blocks on user input
            if msg is None:
                break
            tui.submit(coroutine)           # queue a bg job; returns immediately

Every coroutine passed to `submit()` runs as an asyncio task. If the task
calls `tui.pause(question)`, the input prompt is temporarily replaced with the
question text and the task blocks until the user replies, then resumes.

Multiple tasks can run concurrently; each gets its own output stream.  Only one
task can be paused at a time — a second pause queues behind the first.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Coroutine, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout

from botcircuits.cli.ansi import C, out


def _make_key_bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("c-x")
    @kb.add("c-d")
    def _exit(event):
        event.app.exit(exception=EOFError())

    @kb.add("c-c")
    def _interrupt(event):
        event.app.exit(exception=KeyboardInterrupt())

    return kb


# Sentinel returned by read_message() on EOF.
_EOF = None


class TUISession:
    """Manages the prompt-toolkit session and background task lifecycle."""

    def __init__(self, interactive: bool = True) -> None:
        self._interactive = interactive
        self._session: Optional[PromptSession] = None
        self._patch_ctx = None

        # Queue of (question, reply_future) pairs — tasks that are paused
        # waiting for user input. Processed one at a time.
        self._pause_queue: asyncio.Queue[tuple[str, asyncio.Future[str]]] = (
            asyncio.Queue()
        )

        # When a pause is active, this is the pending (question, future).
        self._active_pause: Optional[tuple[str, asyncio.Future[str]]] = None

        # Normal prompt text and pause-override text.
        self._normal_prompt = C.bold(C.green("you> ")) if interactive else ""
        self._current_prompt = self._normal_prompt

        # All submitted tasks.
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "TUISession":
        if self._interactive and sys.stdin.isatty():
            self._session = PromptSession(key_bindings=_make_key_bindings())
            self._patch_ctx = patch_stdout(raw=True)
            self._patch_ctx.__enter__()
        return self

    async def __aexit__(self, *_) -> None:
        if self._patch_ctx is not None:
            self._patch_ctx.__exit__(None, None, None)
            self._patch_ctx = None
        # Cancel lingering tasks on exit.
        for t in self._tasks:
            if not t.done():
                t.cancel()

    # ------------------------------------------------------------------
    # Input reading
    # ------------------------------------------------------------------

    async def read_message(self) -> Optional[str]:
        """Read the next user message.

        Before blocking on input, drains any pending pause requests from
        background tasks so the question is shown as the prompt.  Returns
        None on EOF (Ctrl-D).

        Supports multi-line blocks: if the user types a bare `\"\"\"` line, keep
        reading until a closing `\"\"\"` and join with newlines.
        """
        # Activate any pending background pause so the prompt shows the question.
        await self._maybe_activate_next_pause()

        first = await self._read_one_line()
        if first is None:
            return None
        if first.strip() == '"""':
            lines: list[str] = []
            while True:
                ln = await self._read_continuation()
                if ln is None or ln.strip() == '"""':
                    break
                lines.append(ln)
            return "\n".join(lines)
        return first

    async def _read_one_line(self) -> Optional[str]:
        """Read one line with the active prompt (pause or normal)."""
        prompt_text = self._current_prompt
        if self._active_pause is not None:
            question, _ = self._active_pause
            prompt_text = (
                C.bold(C.cyan("argus> ")) + question + "\n"
                + C.bold(C.green("you> "))
            )
        if self._session is not None:
            return await self._read_pt(prompt_text)
        return await self._read_stdin(prompt_text)

    async def _read_continuation(self) -> Optional[str]:
        """Read a continuation line for multi-line `\"\"\"` blocks."""
        cont = C.dim("... ")
        if self._session is not None:
            return await self._read_pt(cont)
        return await self._read_stdin(cont)

    async def _read_pt(self, prompt_text: str) -> Optional[str]:
        """Read via prompt_toolkit's asyncio-native prompt.

        Must use `prompt_async`, NOT `prompt()` in a thread executor — the
        sync `prompt()` spins its own blocking event loop internally, which
        fights the real asyncio loop for the terminal and freezes/garbles
        output from background tasks running concurrently under
        `patch_stdout`. `prompt_async` runs cooperatively on this loop.
        """
        try:
            raw = await self._session.prompt_async(ANSI(prompt_text))  # type: ignore[union-attr]
            return raw
        except EOFError:
            return None
        except KeyboardInterrupt:
            out()
            return ""

    async def _read_stdin(self, prompt_text: str) -> Optional[str]:
        """Fallback for non-TTY / piped input."""
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None, lambda: input(prompt_text)
            )
        except EOFError:
            return None
        except KeyboardInterrupt:
            out()
            return ""

    # ------------------------------------------------------------------
    # Message routing (pause vs normal)
    # ------------------------------------------------------------------

    def is_paused(self) -> bool:
        """True when a background task is blocked waiting for user input."""
        return self._active_pause is not None

    async def dispatch_reply(self, msg: str) -> bool:
        """If a task is paused, send msg as the reply and return True.

        The caller should skip normal chat processing when True is returned.
        """
        if self._active_pause is None:
            return False
        _, fut = self._active_pause
        self._active_pause = None
        self._current_prompt = self._normal_prompt
        if not fut.done():
            fut.set_result(msg)
        return True

    async def _maybe_activate_next_pause(self) -> None:
        """Pop the next pending pause from the queue (if any) and make it active."""
        if self._active_pause is not None:
            return
        try:
            item = self._pause_queue.get_nowait()
            self._active_pause = item
        except asyncio.QueueEmpty:
            pass

    # ------------------------------------------------------------------
    # Background task submission
    # ------------------------------------------------------------------

    def submit(self, coro: Coroutine) -> asyncio.Task:
        """Schedule *coro* as a background asyncio task and return the handle."""
        task = asyncio.ensure_future(coro)
        self._tasks.append(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task) -> None:
        self._reap()
        # Surface a task that died without the caller's coroutine catching
        # the error itself — otherwise it's silently dropped (asyncio only
        # logs "exception was never retrieved" at GC time) and the terminal
        # looks hung with no explanation.
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            out(C.red(f"[bg task error] {type(exc).__name__}: {exc}"))

    def _reap(self) -> None:
        self._tasks = [t for t in self._tasks if not t.done()]

    # ------------------------------------------------------------------
    # Pause channel (used by tools that need human input)
    # ------------------------------------------------------------------

    async def pause(self, question: str) -> str:
        """Block the calling coroutine until the user answers *question*.

        Called from inside a background task (tool handler, workflow engine).
        Puts the question on the pause queue; the main loop picks it up on the
        next `read_message()` call and routes the reply back here.
        """
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[str] = loop.create_future()
        await self._pause_queue.put((question, fut))
        # Yield so the main loop can see the pause before we block.
        await asyncio.sleep(0)
        return await fut

    def active_tasks(self) -> list[asyncio.Task]:
        return [t for t in self._tasks if not t.done()]


# ---------------------------------------------------------------------------
# Module-level singleton — set by amain() before the REPL loop starts.
# Tools and the workflow engine reach it via get_tui_session().
# ---------------------------------------------------------------------------

_SESSION: Optional[TUISession] = None


def set_tui_session(s: TUISession) -> None:
    global _SESSION
    _SESSION = s


def get_tui_session() -> Optional[TUISession]:
    return _SESSION
