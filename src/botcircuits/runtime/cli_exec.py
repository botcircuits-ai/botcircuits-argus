"""OS-isolated subprocess runner for CLI agent providers.

Every CLI agent invocation goes through `run_cli`. Keeping it in one place
means:

  - **Injection safety.** We build argv as a list and call
    `asyncio.create_subprocess_exec` (never a shell string), so a prompt that
    contains shell metacharacters is just data.
  - **Working directory.** A caller may pin `cwd` (e.g. the main agent's
    project dir, so the spawned CLI inherits that project's permission
    settings). When `cwd` is omitted, the invocation runs in a fresh
    temporary directory, torn down after — an isolated session context where
    one segment never sees another's scratch files.
  - **One OS seam.** This stage targets Linux command interfaces only. The
    `_supported_platform()` check is the single documented place a Windows /
    macOS path would branch later; everything above this layer is
    platform-agnostic.

The argv TEMPLATE comes from `RuntimeConfig.command`; the literal token
``{prompt}`` in it is replaced with the actual prompt. Nothing else in the
template is interpolated.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from dataclasses import dataclass


#: Token in a command template replaced by the actual prompt text.
PROMPT_TOKEN = "{prompt}"


@dataclass
class CliResult:
    """Outcome of one CLI invocation."""
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class CliExecError(RuntimeError):
    """The configured CLI could not be executed at all (missing binary, bad
    template). Distinct from a non-zero exit, which is reported via
    `CliResult.returncode` so the caller can still read partial stdout."""


def _supported_platform() -> bool:
    """This stage supports Linux-style command interfaces only.

    macOS (`darwin`) shares the POSIX exec/argv model, so it works in
    practice and is allowed; Windows is the case that needs a different
    code path and is rejected here until that seam is filled in.
    """
    return not sys.platform.startswith("win")


def build_argv(command: list[str], prompt: str) -> list[str]:
    """Render a command template into a concrete argv.

    The single token equal to `PROMPT_TOKEN` is replaced by `prompt`; a token
    that merely contains it (e.g. ``--arg={prompt}``) is substituted inline.
    A template with no prompt token gets the prompt appended as a final arg,
    so a bare ``["claude", "-p"]`` still works.
    """
    if not command:
        raise CliExecError("empty command template; set runtime.command")
    out: list[str] = []
    saw_token = False
    for tok in command:
        if tok == PROMPT_TOKEN:
            out.append(prompt)
            saw_token = True
        elif PROMPT_TOKEN in tok:
            out.append(tok.replace(PROMPT_TOKEN, prompt))
            saw_token = True
        else:
            out.append(tok)
    if not saw_token:
        out.append(prompt)
    return out


async def run_cli(
    command: list[str],
    prompt: str,
    *,
    timeout: float = 600.0,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> CliResult:
    """Run a CLI agent once with `prompt`, returning captured output.

    Runs in a fresh temp directory (auto-removed) unless `cwd` is given, so
    each segment gets an isolated session context. Never raises on a non-zero
    exit — that surfaces in `CliResult.returncode`; only an un-runnable
    command (missing binary, unsupported platform) raises `CliExecError`.
    """
    if not _supported_platform():
        raise CliExecError(
            f"CLI runtime providers are not supported on {sys.platform!r} "
            f"in this stage (Linux/POSIX command interfaces only)."
        )

    argv = build_argv(command, prompt)
    run_env = {**os.environ, **(env or {})}

    # Isolated scratch dir per invocation unless the caller pinned one.
    tmp_ctx = (
        tempfile.TemporaryDirectory(prefix="botcircuits-run-")
        if cwd is None else None
    )
    work_dir = cwd if cwd is not None else tmp_ctx.name  # type: ignore[union-attr]

    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work_dir,
                env=run_env,
            )
        except FileNotFoundError as e:
            raise CliExecError(
                f"runtime command not found: {argv[0]!r}. Is it installed and "
                f"on PATH? ({e})"
            ) from e

        try:
            out_b, err_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.communicate()
            except Exception:
                pass
            return CliResult(
                stdout="", stderr=f"timed out after {timeout}s",
                returncode=-1, timed_out=True,
            )

        return CliResult(
            stdout=(out_b or b"").decode("utf-8", errors="replace"),
            stderr=(err_b or b"").decode("utf-8", errors="replace"),
            returncode=proc.returncode if proc.returncode is not None else -1,
        )
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()


__all__ = [
    "PROMPT_TOKEN",
    "CliResult",
    "CliExecError",
    "build_argv",
    "run_cli",
]
