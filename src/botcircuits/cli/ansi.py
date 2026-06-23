"""ANSI color helpers — auto-disabled if stdout isn't a TTY or NO_COLOR is set."""

from __future__ import annotations

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
