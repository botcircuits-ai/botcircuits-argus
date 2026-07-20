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
from dataclasses import dataclass, field
from typing import Coroutine, Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout

from botcircuits.agent.option_select import (
    OTHER_LABEL,
    is_other_reply,
    map_option_reply,
)
from botcircuits.cli.ansi import C, out


@dataclass
class _Pause:
    """One pending pause: a question, its optional predefined answers, and
    the future the caller is blocked on."""

    question: str
    fut: "asyncio.Future[str]"
    options: list[str] = field(default_factory=list)
    #: Which option the selector highlights initially (and thus what a bare
    #: Enter picks) — callers put the safe answer here (e.g. "no" for y/N).
    default_index: int = 0
    #: Current highlight position, mutated by the arrow-key bindings. The
    #: index len(options) is the synthetic "Other" entry.
    selected: int = 0
    #: True once the user picked "Other": the selector hides and the pause
    #: reads one plain typed line, passed through raw (no option mapping).
    typing: bool = False


def _make_key_bindings(session: "TUISession") -> KeyBindings:
    kb = KeyBindings()

    @kb.add("c-x")
    @kb.add("c-d")
    def _exit(event):
        event.app.exit(exception=EOFError())

    @kb.add("c-c")
    def _interrupt(event):
        event.app.exit(exception=KeyboardInterrupt())

    # ------------------------------------------------------------------
    # Option selector: while a pause with predefined options is active and
    # the input buffer is empty, ↑/↓ move the highlight and Enter submits
    # the highlighted option as if the user had typed it. As soon as the
    # user types anything, the bindings step aside — free-form answers
    # (resolved semantically downstream) always stay possible.
    # ------------------------------------------------------------------

    selecting = Condition(lambda: session._selector_active())

    @kb.add("up", filter=selecting)
    def _up(event):
        if not event.current_buffer.text:
            session._move_selection(-1)

    @kb.add("down", filter=selecting)
    def _down(event):
        if not event.current_buffer.text:
            session._move_selection(+1)

    @kb.add("enter", filter=selecting)
    def _pick(event):
        buf = event.current_buffer
        if not buf.text:
            opt = session._selected_option()
            if opt is None:
                # "Other" highlighted: switch this pause to typing mode
                # instead of submitting anything.
                session._enter_typing_mode()
                return
            buf.text = opt
            buf.cursor_position = len(opt)
        buf.validate_and_handle()

    return kb


class TUISession:
    """Manages the prompt-toolkit session and background task lifecycle."""

    def __init__(self, interactive: bool = True) -> None:
        self._interactive = interactive
        self._session: Optional[PromptSession] = None
        self._patch_ctx = None

        # Queue of pauses — tasks blocked waiting for user input. Processed
        # one at a time.
        self._pause_queue: asyncio.Queue[_Pause] = asyncio.Queue()

        # When a pause is active, this is the pending `_Pause`.
        self._active_pause: Optional[_Pause] = None

        # Normal prompt text and pause-override text.
        self._normal_prompt = (
            C.dim("| > ") if interactive else ""
        )
        self._current_prompt = self._normal_prompt

        # All submitted tasks.
        self._tasks: list[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "TUISession":
        if self._interactive and sys.stdin.isatty():
            self._session = PromptSession(key_bindings=_make_key_bindings(self))
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

        # A background task may have queued a pause while we were waiting for
        # input. Activate it now so the caller's dispatch_reply() sees it
        # without needing another read_message() cycle.
        await self._maybe_activate_next_pause()

        if first.strip() == '"""':
            lines: list[str] = []
            while True:
                ln = await self._read_continuation()
                if ln is None or ln.strip() == '"""':
                    break
                lines.append(ln)
            return "\n".join(lines)
        return first

    def _prompt_text(self) -> str:
        """The prompt to render RIGHT NOW — pause question or normal prompt.

        Evaluated lazily on every prompt redraw (see `_read_pt`), so a pause
        that arrives while `prompt_async` is already pending changes the
        visible prompt in place instead of being invisible until the next
        `read_message()` cycle."""
        p = self._active_pause
        if p is not None:
            text = C.bold(C.cyan("argus> ")) + p.question + "\n"
            if p.options and p.typing:
                text += C.dim("  (type your answer)") + "\n"
            elif p.options:
                rows = list(p.options) + [OTHER_LABEL]
                for i, opt in enumerate(rows):
                    if i == p.selected:
                        text += C.bold(C.cyan(f"  ❯ {i + 1}. {opt}")) + "\n"
                    else:
                        text += C.dim(f"    {i + 1}. {opt}") + "\n"
                text += C.dim(
                    "  (↑/↓ + Enter or a number to pick — or type your own answer)"
                ) + "\n"
            return text + C.bold(C.green("you> "))
        return self._current_prompt

    # ------------------------------------------------------------------
    # Option-selector state (read by the key bindings in _make_key_bindings)
    # ------------------------------------------------------------------

    def _selector_active(self) -> bool:
        p = self._active_pause
        return p is not None and bool(p.options) and not p.typing

    def _selected_option(self) -> Optional[str]:
        """The highlighted REAL option, or None when "Other" is highlighted
        (the last row, index len(options))."""
        p = self._active_pause
        if p is None or not p.options or p.selected >= len(p.options):
            return None
        return p.options[max(0, p.selected)]

    def _move_selection(self, delta: int) -> None:
        p = self._active_pause
        if p is None or not p.options:
            return
        p.selected = (p.selected + delta) % (len(p.options) + 1)
        self._invalidate_prompt()

    def _enter_typing_mode(self) -> None:
        p = self._active_pause
        if p is not None:
            p.typing = True
            self._invalidate_prompt()

    def _invalidate_prompt(self) -> None:
        """Force the pending prompt (if any) to redraw with `_prompt_text()`."""
        if self._session is None:
            return
        app = self._session.app
        if app is not None and app.is_running:
            app.invalidate()

    async def _read_one_line(self) -> Optional[str]:
        """Read one line with the active prompt (pause or normal)."""
        if self._session is not None:
            return await self._read_pt(self._prompt_text)
        return await self._read_stdin(self._prompt_text())

    async def _read_continuation(self) -> Optional[str]:
        """Read a continuation line for multi-line `\"\"\"` blocks."""
        cont = C.dim("... ")
        if self._session is not None:
            return await self._read_pt(cont)
        return await self._read_stdin(cont)

    async def _read_pt(self, prompt_text) -> Optional[str]:
        """Read via prompt_toolkit's asyncio-native prompt.

        Must use `prompt_async`, NOT `prompt()` in a thread executor — the
        sync `prompt()` spins its own blocking event loop internally, which
        fights the real asyncio loop for the terminal and freezes/garbles
        output from background tasks running concurrently under
        `patch_stdout`. `prompt_async` runs cooperatively on this loop.

        `prompt_text` may be a str or a zero-arg callable returning str; the
        callable form is re-evaluated on every redraw, which is what lets a
        mid-prompt pause (`pause()` + `_invalidate_prompt()`) surface its
        question without restarting the prompt.
        """
        if callable(prompt_text):
            message = lambda: ANSI(prompt_text())  # noqa: E731 - re-evaluated per redraw
        else:
            message = ANSI(prompt_text)
        try:
            raw = await self._session.prompt_async(message)  # type: ignore[union-attr]
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
        p = self._active_pause
        if p is None:
            return False
        if p.options and not p.typing and is_other_reply(msg, p.options):
            # "Other" picked by number/word: keep the pause active and read
            # the actual answer as plain typed input.
            self._enter_typing_mode()
            return True
        self._active_pause = None
        self._current_prompt = self._normal_prompt
        if not p.fut.done():
            # Digit shortcut / exact label -> the option's canonical text;
            # anything else stays free-form for semantic resolution. After
            # an "Other" pick the reply is passed through raw — a typed "2"
            # is the answer "2", not option #2.
            p.fut.set_result(msg if p.typing else map_option_reply(msg, p.options))
        # Surface the next queued pause (if any) right away so its question
        # replaces the prompt without waiting for another input cycle.
        await self._maybe_activate_next_pause()
        self._invalidate_prompt()
        return True

    async def _maybe_activate_next_pause(self) -> None:
        """Pop the next pending pause from the queue (if any) and make it active."""
        if self._active_pause is not None:
            return
        try:
            self._active_pause = self._pause_queue.get_nowait()
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

    async def pause(
        self,
        question: str,
        options: list[str] | None = None,
        default_index: int = 0,
    ) -> str:
        """Block the calling coroutine until the user answers *question*.

        Called from inside a background task (tool handler, workflow engine).
        When no other pause is active, the question becomes the visible
        prompt IMMEDIATELY — the pending `prompt_async` is invalidated so it
        redraws with the question (this is what makes a y/N confirmation
        visible while the user is sitting at the normal prompt). Concurrent
        pauses queue behind the active one and surface as each is answered.

        `options`, when given, are the question's predefined answers: the
        prompt renders them as an arrow-key/number selector (highlight starts
        at `default_index`) and a pick returns the option text verbatim. The
        user can always type a free-form answer instead — that text passes
        through untouched for the caller to resolve semantically.
        """
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[str] = loop.create_future()
        opts = [str(o) for o in (options or []) if str(o).strip()]
        idx = default_index if 0 <= default_index < len(opts) else 0
        p = _Pause(question=question, fut=fut, options=opts,
                   default_index=idx, selected=idx)
        if self._active_pause is None:
            self._active_pause = p
            self._invalidate_prompt()
        else:
            await self._pause_queue.put(p)
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
