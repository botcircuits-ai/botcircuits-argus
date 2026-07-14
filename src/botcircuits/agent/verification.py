"""Verification — the harness will not accept "done" without receipts.

Until now the agent answered and the harness trusted the answer. This
module closes that gap in two ways (modeled on gemma ch-12):

1. **Enforced-run gate** (wired into the loop, `agent/loop.py`): when a
   turn changed code AND the project declares a test command (a fenced
   block under `## Testing` in AGENTS.md), the harness refuses to accept
   the reply until it has OBSERVED, in this turn's tool transcript, a
   real passing `shell_exec` run of that command (exit 0, paired by
   tool_call_id). A narrated "it works" is never enough — the model runs
   the test itself with the shell tool it already has; the harness only
   watches the receipts. On a missing/failed run it feeds the evidence
   back and loops, capped at `verify_attempts`.

2. **Standalone oracle** (`run_python`): run candidate code plus an
   independent assertion in a fresh, scrubbed process. Used by callers
   (evals, orchestration checks) that hold both a candidate and a check.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from botcircuits.types import Message

#: A write/edit of one of these arms the test gate (a code change to
#: verify, not a prose file — by extension, the way a pre-commit hook
#: decides what to run on).
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb",
    ".c", ".cpp", ".cc", ".h", ".hpp", ".cs", ".php", ".swift", ".kt",
    ".scala", ".sh",
}

#: Tools whose calls count as a code change (the gate trigger) …
_WRITE_TOOLS = ("write_file", "edit_file")
#: … and the tool whose passing run counts as the receipt.
_SHELL_TOOL = "shell_exec"


# ---------------------------------------------------------------------------
# Project test command — the declared oracle
# ---------------------------------------------------------------------------

# The project's declared test command lives in a fenced block under a
# `## Testing` heading — a light convention the harness parses (no
# prose-guessing, no LLM call).
_TESTING_RE = re.compile(
    r"^##\s+Test(?:ing|s)?\b.*?\n```[a-zA-Z0-9]*\n\s*([^\n`]+)",
    re.MULTILINE | re.DOTALL,
)


def test_command(directory: str | Path = ".") -> str | None:
    """The project's declared test command: the first line of the first
    fenced code block under a `## Testing` heading in AGENTS.md. `None`
    if there is none — in which case the harness has no hard gate to
    enforce."""
    path = Path(directory) / "AGENTS.md"
    if not path.is_file():
        return None
    m = _TESTING_RE.search(path.read_text(encoding="utf-8"))
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Transcript checks — did this turn change code / show a passing run?
# ---------------------------------------------------------------------------


def changed_code(messages: list[Message], turn_start: int) -> bool:
    """Did this turn write or edit a source file? Scans the turn's
    assistant `tool_call` blocks for write_file/edit_file with a code
    extension path."""
    for m in messages[turn_start:]:
        if m.role != "assistant":
            continue
        for b in m.blocks:
            if b.get("type") != "tool_call" or b.get("name") not in _WRITE_TOOLS:
                continue
            path = str((b.get("arguments") or {}).get("path", ""))
            if any(path.endswith(ext) for ext in CODE_EXTENSIONS):
                return True
    return False


def observed_pass(messages: list[Message], turn_start: int, command: str) -> bool:
    """True iff this turn's transcript holds a `shell_exec` call running
    `command` that exited 0 — paired by tool_call_id so a failed run is
    not counted as a pass."""
    ran_ids: set[str] = set()
    for m in messages[turn_start:]:
        if m.role != "assistant":
            continue
        for b in m.blocks:
            if b.get("type") != "tool_call" or b.get("name") != _SHELL_TOOL:
                continue
            argv = (b.get("arguments") or {}).get("argv")
            if isinstance(argv, list) and command in " ".join(map(str, argv)):
                ran_ids.add(b.get("id", ""))
    if not ran_ids:
        return False
    for m in messages[turn_start:]:
        for b in m.blocks:
            if (b.get("type") != "tool_result"
                    or b.get("tool_call_id") not in ran_ids
                    or b.get("is_error")):
                continue
            try:
                payload = json.loads(b.get("content") or "")
            except (ValueError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("exit_code") == 0:
                return True
    return False


def verification_nudge(command: str) -> str:
    """The evidence-demand fed back when the gate is not satisfied."""
    return (
        "You changed code but I don't see a passing run of the project's "
        f"tests. Run `{command}` with the shell_exec tool now — it must "
        "exit 0 before you report done. Show the real output."
    )


# ---------------------------------------------------------------------------
# Standalone oracle — run candidate code against an independent check
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:python)?\s*(.*?)```", re.DOTALL)


@dataclass
class VerificationResult:
    passed: bool
    output: str


def extract_code(text: str) -> str:
    """Pull a python code block from model output, or return the text as-is."""
    match = _FENCE.search(text)
    return (match.group(1) if match else text).strip()


def run_python(code: str, check: str, timeout: float = 10.0) -> VerificationResult:
    """Run candidate `code` then an assertion `check` in a fresh process.

    Model-written code runs with a *scrubbed* environment and a scoped
    temp workdir, so we never hand untrusted code our credentials.

    Success is signalled by a **per-run random nonce** printed only after
    `check` completes. A fixed sentinel would be forgeable: code could
    print it and exit 0 *before* the assertion ran. The nonce is unknown
    to the candidate, so an early exit or a printed guess doesn't count.
    (Teaching-grade, not adversarial: candidate code that reads its own
    source file could still recover the nonce.)
    """
    nonce = f"VERIFIED-{uuid.uuid4().hex}"
    script = f"{code}\n\n{check}\nprint({nonce!r})\n"
    workdir = Path(tempfile.mkdtemp(prefix="verify-"))
    candidate = workdir / "candidate.py"
    candidate.write_text(script)
    scrubbed_env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                    "HOME": str(workdir), "LC_ALL": "C"}
    try:
        proc = subprocess.run(
            [sys.executable, str(candidate)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(workdir),
            env=scrubbed_env,
        )
    except subprocess.TimeoutExpired:
        return VerificationResult(False, "error: timed out")
    output = (proc.stdout + proc.stderr).strip()
    passed = proc.returncode == 0 and nonce in proc.stdout
    return VerificationResult(passed=passed, output=output)
