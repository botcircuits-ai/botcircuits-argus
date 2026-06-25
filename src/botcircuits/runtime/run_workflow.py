"""CLI entry point for the workflow-running SKILL.

Drives a built workflow through the deterministic engine, using the selected
agent runtime (claude-code, native, …) for action steps and non-deterministic
slot resolution. Prints a single JSON object describing the outcome so the
host agent can react programmatically.

Usage:
    python -m botcircuits.runtime.run_workflow --name <wf> \\
        [--initial-args '{"k": "v"}'] [--runtime claude-code] [--reply "..."]

Pause/resume across PROCESSES: the engine pauses on a `question` step and
yields a resume cursor + accumulated slots. A single process can't hold that
in memory between a question and the user's answer, so we persist it to
`.botcircuits/workflows/.runs/<name>.json`. Passing `--reply` on the next
invocation loads that state, seeds the reply as the freshest user context,
and continues from the same segment. On completion the state file is removed.

Output (stdout, one JSON object):
    {"status": "done",   "summary": "...", "slots": {...}}
    {"status": "paused", "question": "...", "name": "<wf>"}
    {"status": "error",  "error": "..."}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

from botcircuits.runtime.detect import (
    NATIVE,
    detect_runtime_name as _detect,
    select_runtime,
)
from botcircuits.agent.workflow.engine.runner import run_workflow_engine
from botcircuits.agent.workflow.local import (
    LocalWorkflowError,
    _load_workflow_record,
    _resolve_workflows_dir,
)
from botcircuits.agent.workflow.tracing import SessionTrace, new_session_id
from botcircuits.runtime.trace_hooks import traced_provider


_RUNS_DIR_NAME = ".runs"

#: Replies that count as "yes, grant the tool" when a segment paused asking
#: for a tool permission. Matched case-insensitively as a leading token, so
#: "yes use websearch" / "sure, go ahead" / "ok" all grant.
_AFFIRMATIVE_RE = re.compile(
    r"^\s*(yes|yep|yeah|yup|sure|ok|okay|go ahead|please do|do it|"
    r"allow|grant|approved?|use it|fine|y)\b",
    re.IGNORECASE,
)


def _reply_grants_tools(reply: str | None, needs_tool: list[str]) -> list[str]:
    """If the user affirmatively answered a permission pause, return the tools
    to grant; otherwise an empty list.

    A permission pause carries the tool(s) it was blocked on in `needs_tool`.
    An affirmative reply ("yes use websearch", "ok", "allow") grants exactly
    those — we do NOT parse tool names out of free text, so the user can't
    accidentally grant something the segment never asked for.
    """
    if not reply or not needs_tool:
        return []
    if _AFFIRMATIVE_RE.match(reply):
        return list(needs_tool)
    return []


def _claude_settings_path(cwd: str | None = None) -> Path:
    """`<cwd>/.claude/settings.json` — the permission policy the spawned
    headless `claude` reads. This is Claude Code's settings file (NOT
    BotCircuits' own `.botcircuits/settings.json`)."""
    base = Path(cwd) if cwd else Path.cwd()
    return base / ".claude" / "settings.json"


def _persist_granted_tools(tools: list[str], cwd: str | None = None) -> list[str]:
    """Add `tools` to `.claude/settings.json` → `permissions.allow` so the
    grant is permanent: future runs' spawned `claude` inherit it from disk and
    never pause for these tools again.

    Idempotent — only tools not already allowed are added. Returns the tools
    actually newly written (empty if all were already present or on any I/O /
    parse error, which is logged and swallowed so a settings hiccup never
    breaks the run).
    """
    if not tools:
        return []
    path = _claude_settings_path(cwd)
    try:
        data: dict = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8")) or {}
            except (OSError, json.JSONDecodeError) as e:
                # Don't clobber a file we can't parse — surface and skip.
                print(f"[runtime] could not read {path} to persist grant: {e}",
                      file=sys.stderr)
                return []
        if not isinstance(data, dict):
            print(f"[runtime] {path} is not a JSON object; skipping grant "
                  "persistence", file=sys.stderr)
            return []

        perms = data.get("permissions")
        if not isinstance(perms, dict):
            perms = {}
            data["permissions"] = perms
        allow = perms.get("allow")
        if not isinstance(allow, list):
            allow = []
            perms["allow"] = allow

        existing = {a for a in allow if isinstance(a, str)}
        added = [t for t in tools if t not in existing]
        if not added:
            return []
        allow.extend(added)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return added
    except OSError as e:
        print(f"[runtime] could not write {path} to persist grant: {e}",
              file=sys.stderr)
        return []


def _runs_dir() -> Path:
    return _resolve_workflows_dir() / _RUNS_DIR_NAME


def _state_path(name: str) -> Path:
    return _runs_dir() / f"{name}.json"


def _load_state(name: str) -> dict:
    p = _state_path(name)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(name: str, state: dict) -> None:
    p = _state_path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state), encoding="utf-8")


def _clear_state(name: str) -> None:
    try:
        _state_path(name).unlink()
    except OSError:
        pass


# --- Tracing helpers -------------------------------------------------------

def _open_trace(
    *,
    name: str,
    runtime: str,
    initial_slots: dict[str, Any],
    saved_session_id: str | None,
    flow: dict | None = None,
) -> SessionTrace | None:
    """Open the session trace for this run. On resume (a saved session_id),
    reopen the same file so events append into one timeline; otherwise start a
    fresh session (snapshotting the workflow graph for the trace view).
    Best-effort — a tracing failure never blocks the run."""
    try:
        if saved_session_id:
            existing = SessionTrace.load(saved_session_id)
            if existing is not None:
                return existing
        return SessionTrace.start(
            workflow_name=name,
            runtime=runtime,
            initial_slots=initial_slots,
            session_id=saved_session_id or new_session_id(),
            flow=flow,
        )
    except Exception:  # pragma: no cover - tracing must not break a run
        return None


def _trace_sink(trace: SessionTrace | None):
    """Build the engine event_sink that records step-enter / branch events.

    The engine emits ``("step_enter", {...})`` and ``("branch", {...})`` for its
    own deterministic navigation; the traced provider handles action + slot
    events. Returns ``None`` when not tracing so the engine skips the work."""
    if trace is None:
        return None

    async def sink(kind: str, payload: Any) -> None:
        try:
            if kind == "step_enter" and isinstance(payload, dict):
                # A segment bundles one or more actual steps; `payload["step"]`
                # is the segment HEAD, which for a transparent start/systemAction
                # head (e.g. `start`) is not the step whose action runs. Label
                # the event with the last real step in the segment (the one the
                # action belongs to) and carry the full `steps` list so the UI
                # can mark every bundled step visited — without it, steps that
                # are never a segment head (e.g. a question or its follow-on)
                # never appear "entered", breaking path connectivity.
                seg_steps = [s for s in (payload.get("steps") or []) if s]
                head = payload.get("step")
                primary = seg_steps[-1] if seg_steps else head
                trace.event(
                    "step_enter",
                    step=primary,
                    slots=payload.get("slots"),
                    data={
                        "actions": payload.get("actions") or [],
                        "segment": head,
                        "steps": seg_steps or ([head] if head else []),
                    },
                )
            elif kind == "branch" and isinstance(payload, dict):
                trace.event(
                    "branch",
                    step=payload.get("step"),
                    slots=payload.get("slots"),
                    data={
                        "chosen_next": payload.get("chosen_next"),
                        "default_next": payload.get("default_next"),
                        "branched": payload.get("branched"),
                    },
                )
        except Exception:  # pragma: no cover
            pass

    return sink


def _record_memory_graph(
    trace: SessionTrace, flow: dict, slots: dict[str, Any] | None,
) -> None:
    """Project the final state into the session memory graph: one node per
    filled slot, plus step nodes from the trace, edges from each step to the
    slots it produced. Best-effort."""
    try:
        produced: dict[str, str | None] = {}
        # `action_after` events carry no step id (the action ran inside the
        # provider, not at a step boundary), so attribute their captured slots
        # to the most recent `step_enter` — the step in progress at that point.
        current_step: str | None = None
        for ev in trace._doc.get("trace", []):  # noqa: SLF001 - same package
            if ev.get("type") == "step_enter" and ev.get("step"):
                current_step = ev.get("step")
            elif ev.get("type") == "action_after":
                out = (ev.get("data") or {}).get("output") or {}
                for k in (out.get("captured_slots") or {}):
                    produced[k] = ev.get("step") or current_step
        for k, v in (slots or {}).items():
            if isinstance(k, str) and k.startswith("__"):
                continue
            trace.add_memory_node(f"slot:{k}", kind="slot", label=k, value=v)
            src_step = produced.get(k)
            if src_step:
                trace.add_memory_node(f"step:{src_step}", kind="step", label=src_step)
                trace.add_memory_edge(f"step:{src_step}", f"slot:{k}", kind="produces")
    except Exception:  # pragma: no cover
        pass


async def _run(
    name: str,
    *,
    initial_args: dict,
    runtime_name: str | None,
    reply: str | None,
    runtime_command: list[str] | None = None,
) -> dict:
    record = _load_workflow_record(name)
    flow = record.get("flow")
    if not isinstance(flow, dict):
        raise LocalWorkflowError(f"workflow {name!r} is missing flow")

    # Select the runtime. The runner is for EXTERNAL/CLI hosts; `native` here
    # would need a live Agent we don't build in this entry point, so reject it
    # with a clear message (use the in-process CLI / agent loop for native).
    resolved_name = runtime_name or _detect()
    if resolved_name == NATIVE:
        raise LocalWorkflowError(
            "the native runtime has no standalone runner; run the workflow "
            "through the BotCircuits agent (botcircuits) instead, or pass "
            "--runtime claude-code."
        )
    # A caller may pin the exact spawn argv for the chosen CLI runtime (e.g.
    # the eval pinning hermes' `--provider`/`-m`, which hermes takes only as
    # flags — it ignores model env by design). Feed it through the runtime
    # layer's existing `runtimes.<name>.command` override path.
    settings = None
    if runtime_command:
        settings = {"runtimes": {resolved_name: {"command": runtime_command}}}
    provider = select_runtime(settings=settings, name=resolved_name)

    # Resume from a persisted pause if this is a --reply continuation.
    saved = _load_state(name) if reply is not None else {}
    resume_step = saved.get("engine_paused_step")
    slots: dict[str, Any] = dict(saved.get("engine_slots") or {})
    if resume_step is None:
        slots.update({k: v for k, v in initial_args.items() if v not in (None, "")})
    if reply:
        slots["__last_user_message__"] = reply

    # Grant-on-reply: a segment that paused for a missing tool permission
    # recorded the tool(s) in run-state. If THIS reply affirmatively answers
    # that pause, grant those tools for the rest of the run by handing them to
    # the CLI provider (it appends `--allowedTools …` to each spawn). Granted
    # tools accumulate across pauses and persist via run-state so they survive
    # the next process hop too.
    granted: list[str] = list(saved.get("granted_tools") or [])
    newly = _reply_grants_tools(reply, list(saved.get("needs_tool") or []))
    for t in newly:
        if t not in granted:
            granted.append(t)
    if granted and hasattr(provider, "config"):
        provider.config.allowed_tools = granted
    # Persist a FIRST-TIME grant to `.claude/settings.json` so it's permanent:
    # the spawned `claude` reads that file from the project dir, so once a tool
    # is allowed there, future runs never pause for it again. Best-effort —
    # written to the same cwd the CLI provider spawns in.
    if newly:
        provider_cwd = getattr(getattr(provider, "config", None), "cwd", None)
        persisted = _persist_granted_tools(newly, cwd=provider_cwd)
        if persisted:
            print(
                f"[runtime] granted {', '.join(persisted)} permanently in "
                f"{_claude_settings_path(provider_cwd)}",
                file=sys.stderr,
            )

    # --- Tracing -----------------------------------------------------------
    # One session_id spans the whole run, including pause/resume: a resumed
    # leg reopens the same session file (id stored in the run-state). The
    # session_start event records the initial slots; the engine + the traced
    # provider append step/action/slot/branch events; we close it at the end.
    trace = _open_trace(
        name=name,
        runtime=resolved_name,
        initial_slots=slots,
        saved_session_id=saved.get("session_id"),
        flow=flow,
    )
    sink = _trace_sink(trace)
    run_provider = traced_provider(provider, trace)

    try:
        result = await run_workflow_engine(
            flow,
            workflow_name=name,
            run_segment=lambda **kw: run_provider.run_segment(**kw),
            start_step_id=resume_step,
            slots=slots,
            resolve_unfilled=lambda **kw: run_provider.resolve_slots(**kw),
            event_sink=sink,
        )
    finally:
        await provider.aclose()

    usage_dict = result.usage.to_dict() if result.usage else None

    if result.paused:
        _save_state(name, {
            "engine_paused_step": result.paused_step or resume_step,
            "engine_slots": result.slots,
            "session_id": trace.session_id if trace else None,
            # Carry the tool(s) this pause is blocked on so the next --reply
            # can grant them, plus the running set already granted.
            "needs_tool": list(result.needs_tool),
            "granted_tools": granted,
        })
        if trace:
            trace.event(
                "paused", slots=result.slots,
                data={"question": result.question, "usage": usage_dict},
            )
        out: dict[str, Any] = {
            "status": "paused", "question": result.question, "name": name,
        }
        if usage_dict:
            out["usage"] = usage_dict
        return out

    _clear_state(name)
    clean_slots = {
        k: v for k, v in (result.slots or {}).items()
        if not k.startswith("__")
    }
    if trace:
        _record_memory_graph(trace, flow, result.slots)
        trace.end(status="done", summary=result.summary, slots=result.slots)
        try:
            trace.event("usage", data=usage_dict or {})
        except Exception:  # pragma: no cover - tracing must not break a run
            pass
    out = {"status": "done", "summary": result.summary, "slots": clean_slots}
    if usage_dict:
        out["usage"] = usage_dict
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="botcircuits.runtime.run_workflow")
    parser.add_argument("--name", required=True, help="built workflow name")
    parser.add_argument("--initial-args", default="",
                        help="JSON object of initial slot values")
    parser.add_argument("--runtime", default=None,
                        help="force a runtime (claude-code, hermes, codex, …); "
                             "default = auto-detect")
    parser.add_argument("--reply", default=None,
                        help="user's answer to a prior pause; resumes the run")
    parser.add_argument(
        "--runtime-command", default=None,
        help="override the CLI runtime's spawn argv as a JSON array; the "
             "token \"{prompt}\" is replaced with each segment prompt. e.g. "
             "'[\"hermes\",\"-z\",\"{prompt}\",\"--yolo\",\"--provider\","
             "\"anthropic\",\"-m\",\"claude-opus\"]'",
    )
    args = parser.parse_args(argv)

    runtime_command: list[str] | None = None
    if args.runtime_command and args.runtime_command.strip():
        try:
            parsed_cmd = json.loads(args.runtime_command)
        except json.JSONDecodeError as e:
            print(json.dumps({"status": "error",
                              "error": f"--runtime-command not valid JSON: {e}"}))
            return 2
        if not (isinstance(parsed_cmd, list)
                and all(isinstance(t, str) for t in parsed_cmd)):
            print(json.dumps({"status": "error",
                              "error": "--runtime-command must be a JSON array "
                                       "of strings"}))
            return 2
        runtime_command = parsed_cmd

    initial_args: dict = {}
    if args.initial_args.strip():
        try:
            parsed = json.loads(args.initial_args)
        except json.JSONDecodeError as e:
            print(json.dumps({"status": "error",
                              "error": f"--initial-args not valid JSON: {e}"}))
            return 2
        if not isinstance(parsed, dict):
            print(json.dumps({"status": "error",
                              "error": "--initial-args must be a JSON object"}))
            return 2
        initial_args = parsed

    try:
        out = asyncio.run(_run(
            args.name,
            initial_args=initial_args,
            runtime_name=args.runtime,
            reply=args.reply,
            runtime_command=runtime_command,
        ))
    except LocalWorkflowError as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        return 1
    except Exception as e:  # pragma: no cover - defensive top-level guard
        print(json.dumps({"status": "error",
                          "error": f"{type(e).__name__}: {e}"}))
        return 1

    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
