"""Natural-language workflow authoring via the configured agent runtime.

The manager web's authoring chat sends a free-text instruction (e.g. "create
an order fulfillment workflow with a refund branch"); this module drives the
**configured agent runtime** (default ``claude-code``) as a subprocess to turn
that instruction into a written + built workflow file.

We reuse the runtime selection / argv template from ``runtime.detect`` so the
runtime is whatever the project is configured for, and ``cli_exec``-style argv
construction (list, never a shell string) so the instruction is just data.

Output is streamed as Server-Sent Events so the chat shows live progress: the
runtime's stdout/stderr lines as they arrive, then a terminal ``done`` event
carrying the resulting workflow source (if the named file now exists).
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

from botcircuits.runtime.cli_exec import build_argv
from botcircuits.runtime.detect import detect_runtime_name, runtime_config
from botcircuits.manager import workflows as wf_store


def _settings() -> dict:
    """Layered project settings (for runtime selection). Best-effort."""
    try:
        from botcircuits.cli.settings import load_layered_settings

        values, _used = load_layered_settings()
        return values if isinstance(values, dict) else {}
    except Exception:
        return {}


def _build_prompt(instruction: str, name: str, existing: dict | None) -> str:
    """The instruction handed to the runtime CLI.

    We explicitly point it at the workflow-authoring skill and the target file
    so the runtime writes the source and runs ``workflow build`` itself — the
    same contract the skill documents for an interactive session.
    """
    mode = "edit the existing" if existing else "create a new"
    return (
        "Use the botcircuits-workflow-authoring skill to "
        f"{mode} workflow named `{name}`.\n\n"
        f"User request:\n{instruction}\n\n"
        "Write the workflow source to "
        f".botcircuits/workflows/{name}.json and then run "
        f"`botcircuits workflow build --name {name}`. Keep the slug-safe name "
        f"`{name}`. When done, briefly confirm the steps and branches."
    )


def _sse(event: str, data: dict) -> str:
    """Format one Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def author_stream(instruction: str, name: str) -> AsyncIterator[str]:
    """Drive the configured runtime and yield SSE frames.

    Emits:
      - ``start``  {runtime, name}
      - ``log``    {line}            (per stdout/stderr line)
      - ``done``   {ok, name, workflow}  (workflow = the source doc, or null)
      - ``error``  {message}         (runtime could not be launched)
    """
    instruction = (instruction or "").strip()
    if not instruction:
        yield _sse("error", {"message": "empty instruction"})
        return
    if not wf_store.is_valid_name(name):
        yield _sse("error", {"message": f"invalid workflow name {name!r}"})
        return

    settings = _settings()
    runtime_name = detect_runtime_name(settings)
    config = runtime_config(runtime_name, settings)
    if not config.command:
        yield _sse(
            "error",
            {
                "message": (
                    f"runtime {runtime_name!r} has no command template; set a "
                    "CLI runtime (e.g. claude-code) to use chat authoring."
                )
            },
        )
        return

    existing = wf_store.get_workflow(name)
    prompt = _build_prompt(instruction, name, existing)
    argv = build_argv(config.command, prompt)

    yield _sse("start", {"runtime": runtime_name, "name": name})

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=config.cwd or None,
        )
    except (FileNotFoundError, OSError) as e:
        yield _sse(
            "error",
            {"message": f"could not launch runtime {argv[0]!r}: {e}"},
        )
        return

    assert proc.stdout is not None
    try:
        async for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if line:
                yield _sse("log", {"line": line})
        await asyncio.wait_for(proc.wait(), timeout=config.timeout)
    except asyncio.TimeoutError:
        proc.kill()
        yield _sse("error", {"message": f"runtime timed out after {config.timeout}s"})
        return

    ok = proc.returncode == 0
    doc = wf_store.get_workflow(name)
    built = bool(doc) and wf_store.is_built(name)
    yield _sse(
        "done",
        {"ok": ok and doc is not None, "name": name, "workflow": doc, "built": built},
    )


async def run_stream(name: str, reply: str | None = None) -> AsyncIterator[str]:
    """Run a built workflow through the deterministic engine and yield SSE.

    Mirrors the ``botcircuits workflow run`` CLI outcome contract, but in-process
    so the manager web's AI chat can run a workflow conversationally. Pause state
    is persisted by the engine to ``.botcircuits/workflows/.runs/<name>.json``,
    so a ``paused`` outcome can be resumed by a follow-up call carrying ``reply``.

    Emits:
      - ``start``   {name}
      - ``result``  {status, message?, question?}   status in success|failure|paused
      - ``error``   {message}                        could not start the run
    """
    if not wf_store.is_valid_name(name):
        yield _sse("error", {"message": f"invalid workflow name {name!r}"})
        return
    if not wf_store.get_workflow(name):
        yield _sse("error", {"message": f"workflow {name!r} not found"})
        return
    if not wf_store.is_built(name):
        yield _sse(
            "error",
            {
                "message": (
                    f"workflow {name!r} is not built yet — build it first, then run."
                )
            },
        )
        return

    yield _sse("start", {"name": name})

    from botcircuits.runtime.run_workflow import _run
    from botcircuits.agent.workflow.local import LocalWorkflowError

    try:
        result = await _run(name, initial_args={}, runtime_name=None, reply=reply)
    except LocalWorkflowError as e:
        yield _sse("result", {"status": "failure", "message": str(e)})
        return
    except Exception as e:  # defensive: surface as a failure, don't crash the stream
        yield _sse("result", {"status": "failure", "message": f"{type(e).__name__}: {e}"})
        return

    status = result.get("status")
    if status == "paused":
        yield _sse("result", {"status": "paused", "question": result.get("question") or ""})
    elif status == "done":
        yield _sse("result", {"status": "success", "message": result.get("summary") or ""})
    else:
        yield _sse("result", {"status": "failure", "message": result.get("error") or "workflow run failed"})


__all__ = ["author_stream", "run_stream"]
