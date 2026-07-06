"""ANSI color helpers — auto-disabled if stdout isn't a TTY or NO_COLOR is set."""

from __future__ import annotations

import asyncio
import os
import sys


class C:
    _on = sys.stdout.isatty() and os.getenv("NO_COLOR") is None

    @classmethod
    def _w(cls, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if cls._on else s

    @classmethod
    def dim(cls, s): return cls._w("2", s)
    @classmethod
    def bold(cls, s): return cls._w("1", s)
    @classmethod
    def cyan(cls, s): return cls._w("36", s)
    @classmethod
    def green(cls, s): return cls._w("32", s)
    @classmethod
    def yellow(cls, s): return cls._w("33", s)
    @classmethod
    def red(cls, s): return cls._w("31", s)
    @classmethod
    def magenta(cls, s): return cls._w("35", s)


def out(*parts, end="\n", flush=True) -> None:
    print(*parts, end=end, flush=flush)


_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


class Spinner:
    """A small terminal spinner shown while waiting for the first byte of
    a response. No-op when stdout isn't a TTY (piped/CI output stays
    clean and machine-parseable).

    Usage:
        async with Spinner():
            await something_slow()

    Or manually via `start()`/`stop()` when the call site needs to clear
    the spinner partway through (e.g. right before streaming begins)."""

    def __init__(self, interval: float = 0.08) -> None:
        self._interval = interval
        self._task: asyncio.Task | None = None
        # Raw \r writes here bypass prompt_toolkit's patch_stdout cursor
        # tracking and corrupt/hide the pinned input prompt + concurrent
        # background-task output. Disable whenever a TUISession is driving
        # the terminal — its prompt redraw already signals liveness.
        tui_active = False
        try:
            from botcircuits.cli.tui import get_tui_session
            tui_active = get_tui_session() is not None
        except ImportError:
            pass
        self._active = C._on and sys.stdout.isatty() and not tui_active

    async def _spin(self) -> None:
        i = 0
        try:
            while True:
                frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
                sys.stdout.write(f"\r{frame} ")
                sys.stdout.flush()
                i += 1
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            pass

    def start(self) -> None:
        if not self._active or self._task is not None:
            return
        self._task = asyncio.ensure_future(self._spin())

    def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        self._task = None
        if self._active:
            sys.stdout.write("\r\033[K")  # clear the spinner line
            sys.stdout.flush()

    async def __aenter__(self) -> "Spinner":
        self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.stop()
