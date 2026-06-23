"""Slash-command dispatch and CLI session state."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any, Optional

from botcircuits.agent.tools import register_builtin
from botcircuits.agent.workflow.cli_commands import (
    WORKFLOW_USAGE,
    compose_add_prompt,
    compose_edit_prompt,
    compose_forced_run_follow_up,
    compose_forced_run_kickoff,
    locate_workflow_for_edit,
    parse_workflow_command,
)
from botcircuits.types import Message
from botcircuits.cli.ansi import C, out
from botcircuits.cli.config import CLIConfig

if TYPE_CHECKING:
    from botcircuits.agent import Agent


# Slash commands that lazy-register a builtin tool and then forward the
# remainder of the line to the model as a chat message. The model sees
# the freshly registered tool on its next turn and uses it to satisfy
# the request — same code path as if the tool had been on the registry
# from the start, but without paying the cost of loading it on every run.
#
# Some triggers (e.g. /workflow) have bespoke argument parsing handled
# below; others fall through to the generic "load tool, forward prompt"
# path.
LAZY_TOOL_TRIGGERS: dict[str, str] = {
    "/workflow": "build_workflow",
}


class CLIState:
    """Mutable session state held across one CLI run. Seeded from a
    resolved CLIConfig; slash commands mutate these fields in place."""

    def __init__(self, cfg: CLIConfig) -> None:
        self.session_id: Optional[str] = cfg.session
        self.system: Optional[str] = cfg.system
        self.stream: bool = cfg.stream
        self.show_tool_results: bool = cfg.show_tool_results


def print_help() -> None:
    out(C.dim("commands:"))
    out(C.dim("  /reset                drop current session"))
    out(C.dim("  /session [id]         show or switch session id"))
    out(C.dim("  /system <text>        set system prompt (effective on /reset)"))
    out(C.dim("  /stream on|off        toggle streaming"))
    out(C.dim("  /tools                list available tools"))
    out(C.dim("  /skills               list filesystem skills"))
    out(C.dim("  /<skill-name>         run a filesystem skill directly"))
    out(C.dim('  /workflow add "<prompt>" [--name <wf>]'))
    out(C.dim('                        author a new workflow'))
    out(C.dim('  /workflow add --file <path.md> [--name <wf>]'))
    out(C.dim('                        author a new workflow from a prompt file'))
    out(C.dim('  /workflow edit "<prompt>" --name <wf>'))
    out(C.dim('                        edit existing workflow <wf> via build_workflow'))
    out(C.dim('  /workflow run --name <wf> [--initial-args \'{"k":"v"}\']'))
    out(C.dim('                        force-start workflow <wf>, bypassing tool choice'))
    out(C.dim("  /memory               show persistent memory (MEMORY.md, USER.md)"))
    out(C.dim("  /help                 this help"))
    out(C.dim("  /quit                 exit"))
    out(C.dim('  paste a """ line to start/end multi-line input'))


async def handle_slash(
    cmd: str, agent: "Agent", state: CLIState,
) -> tuple[bool, Optional[str]]:
    """Dispatch a slash command.

    Returns (handled, follow_up):
      - handled  — True if the command was recognized; caller skips its
                   own "unknown command" path.
      - follow_up — when non-None, the caller should treat this string as
                   a chat message to send right after handling. Used by
                   the lazy-tool triggers in LAZY_TOOL_TRIGGERS so e.g.
                   `/workflow check order status` registers
                   `build_workflow` and then sends "check order status"
                   to the model in one user action.
    """
    parts = cmd.strip().split(maxsplit=1)
    head = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    # Lazy-tool triggers run before the built-in slash command list so
    # /workflow doesn't have to compete with any other handler. The map
    # is the single source of truth.
    if head in LAZY_TOOL_TRIGGERS:
        tool_name = LAZY_TOOL_TRIGGERS[head]
        if head == "/workflow":
            return True, await _handle_workflow_trigger(rest, agent, state, tool_name)
        # Generic fallback for any future lazy trigger: load the tool,
        # forward the remainder as a chat message.
        prompt = rest.strip()
        if not prompt:
            out(C.dim(f"usage: {head} <prompt>"))
            return True, None
        added = register_builtin(
            agent.tools, tool_name, provider=agent.provider,
        )
        if added:
            out(C.dim(f"(loaded tool: {tool_name})"))
        return True, prompt

    if head == "/quit" or head == "/exit":
        raise SystemExit(0)

    if head == "/help":
        print_help()
        return True, None

    if head == "/reset":
        if state.session_id:
            agent.store.reset(state.session_id)
        state.session_id = None
        out(C.dim("(session reset)"))
        return True, None

    if head == "/session":
        if rest:
            state.session_id = rest.strip()
            out(C.dim(f"(session set to {state.session_id})"))
        else:
            out(C.dim(f"session_id = {state.session_id or '(none yet)'}"))
        return True, None

    if head == "/system":
        if not rest:
            out(C.dim(f"system = {state.system or '(default)'}"))
            return True, None
        state.system = rest
        out(C.dim("(system prompt set; takes effect on next /reset)"))
        return True, None

    if head == "/stream":
        if rest.lower() in ("on", "true", "1"):
            state.stream = True
        elif rest.lower() in ("off", "false", "0"):
            state.stream = False
        else:
            out(C.dim("usage: /stream on|off"))
            return True, None
        out(C.dim(f"(streaming = {state.stream})"))
        return True, None

    if head == "/tools":
        tools = agent.tools.all()
        if not tools:
            out(C.dim("(no tools registered)"))
        for t in tools:
            out(f"  {C.cyan(t.name)}  {C.dim(t.description)}")
        return True, None

    if head == "/memory":
        from ..agent.memory import (
            MEMORY_CAP_CHARS,
            USER_CAP_CHARS,
            list_entries,
            memory_dir,
        )
        out(C.dim(f"memory dir: {memory_dir()}"))
        for target, cap in (("user", USER_CAP_CHARS), ("memory", MEMORY_CAP_CHARS)):
            entries = list_entries(target)
            used = sum(len(e) for e in entries) + max(0, len(entries) - 1) * 3
            out(C.cyan(
                f"  {target.upper()}.md  ({len(entries)} entries, "
                f"{used}/{cap} chars)"
            ))
            if not entries:
                out(C.dim("    (empty)"))
                continue
            for i, e in enumerate(entries, 1):
                first_line = e.splitlines()[0] if e else ""
                preview = first_line[:160]
                truncated = len(first_line) > 160 or "\n" in e
                tail = C.dim(" …") if truncated else ""
                out(f"    {C.dim(str(i) + '.')} {preview}{tail}")
        return True, None

    if head == "/skills":
        skills = getattr(agent, "local_skills", [])
        if not skills:
            out(C.dim("(no filesystem skills loaded)"))
        for sk in skills:
            tag = C.dim(" [user-only]") if sk.disable_model_invocation else ""
            out(f"  {C.cyan('/' + sk.name)}{tag}  {C.dim(sk.description)}")
        return True, None

    # /<skill-name> — manual invocation, bypassing the model. Runs the
    # skill's rendered body (including !`cmd` substitutions) and prints
    # the result to the user. The handler is the same one the model
    # would have called, so user-invoke and model-invoke produce
    # identical output.
    skill_name = head.lstrip("/")
    skill = next((s for s in getattr(agent, "local_skills", [])
                  if s.name == skill_name), None)
    if skill is not None:
        from ..agent.skill import render_body
        body = await render_body(skill)
        out(body)
        return True, None

    out(C.red(f"unknown command: {head}  (try /help)"))
    return True, None


# ---------------------------------------------------------------------------
# /workflow trigger
# ---------------------------------------------------------------------------


async def _handle_workflow_trigger(
    rest: str, agent: "Agent", state: "CLIState", tool_name: str,
) -> Optional[str]:
    """Parse `/workflow add|edit|run ...` and return the chat message to send.

    Returns None when the command was malformed (we've already printed
    the usage hint) so the caller skips chat dispatch. For `add`/`edit`
    we lazy-register `build_workflow` and return a model-facing prompt
    that carries the right framing. For `run` we directly invoke the
    target workflow tool (bypassing model tool choice), seed the
    conversation with a synthetic tool_call/tool_result pair, and return
    a continuation prompt so the agent loop picks up where the workflow
    paused.

    The parsing + framing-prompt composition lives in
    `botcircuits.agent.workflow.cli_commands` so external embedders
    (e.g. `botcircuits_hermes`) reuse the same logic.
    """
    parsed = parse_workflow_command(rest)

    if parsed.kind == "error":
        out(C.red(f"[workflow] {parsed.error}"))
        if parsed.show_usage:
            out(C.dim(WORKFLOW_USAGE))
        return None

    if parsed.kind == "run":
        return await _handle_workflow_run(
            agent, state, parsed.target or "", parsed.initial_args,
        )

    if parsed.kind == "add":
        _load_tool(agent, tool_name)
        return compose_add_prompt(parsed.prompt, parsed.target)

    # parsed.kind == "edit"
    located = locate_workflow_for_edit(parsed.target or "")
    if not located.found:
        out(C.red(located.error or "[workflow edit] not found"))
        return None

    _load_tool(agent, tool_name)
    return compose_edit_prompt(
        parsed.target or "", parsed.prompt, located.path,  # type: ignore[arg-type]
    )


async def _handle_workflow_run(
    agent: "Agent",
    state: "CLIState",
    target: str,
    initial_args: dict,
) -> Optional[str]:
    """Force-start the named workflow tool, bypassing model tool choice.

    `target` + `initial_args` come from the shared parser in
    `botcircuits.agent.workflow.cli_commands`. This function focuses on
    the BotCircuits-specific work: refresh the in-process workflow
    registry, invoke the matching tool, and seed the conversation with
    a synthetic tool_call/tool_result pair so the next LLM turn sees
    the workflow step as if the model had called the tool itself.
    Returns a short continuation prompt the caller dispatches as a
    normal chat message — the workflow reminder in the system prompt
    then drives the loop.
    """
    # Re-discover workflow tools so a freshly authored workflow doesn't
    # require a CLI restart. Existing workflow tools get re-bound to the
    # latest on-disk record; collisions with built-ins are left alone.
    try:
        from ..agent.workflow import register_workflows
        await register_workflows(agent.tools, provider=agent.provider)
    except Exception as e:
        out(C.red(
            f"[workflow run] failed to refresh workflows: "
            f"{type(e).__name__}: {e}"
        ))
        return None

    tool = next((t for t in agent.tools.all() if t.name == target), None)
    if tool is None or getattr(tool, "_workflow_state", None) is None:
        available = [t.name for t in agent.tools.all()
                     if getattr(t, "_workflow_state", None) is not None]
        suffix = (
            f" Available workflows: {', '.join(sorted(available))}."
            if available else " No workflows are registered."
        )
        out(C.red(f"[workflow run] no workflow named {target!r}.{suffix}"))
        return None

    out(C.dim(
        f"(force-running workflow {target!r} "
        f"with initial args: {json.dumps(initial_args)})"
    ))

    convo = agent.store.get_or_create(state.session_id, system=state.system)
    state.session_id = convo.session_id

    ctx = {
        "last_assistant_message": "",
        "last_user_message": "",
        "session_id": convo.session_id,
    }
    try:
        text, is_error = await agent.tools.run(target, initial_args, ctx)
    except Exception as e:
        out(C.red(
            f"[workflow run] tool invocation failed: "
            f"{type(e).__name__}: {e}"
        ))
        return None

    color = C.red if is_error else C.green
    label = "error" if is_error else "result"
    out(color(f"  ◂ {label}      ") + (text or "(empty)"))

    # Seed the conversation with the forced call so the LLM's next turn
    # sees the workflow step as if it had been the one to invoke the
    # tool. We bracket it with a user "kickoff" message + synthetic
    # assistant tool_call + user tool_result so the provider gets a
    # well-formed turn order. Kickoff/follow-up wording is shared with
    # the Hermes adapter via `cli_commands`.
    call_id = f"force-{uuid.uuid4().hex[:12]}"
    convo.messages.append(Message(
        role="user",
        blocks=[{
            "type": "text",
            "text": compose_forced_run_kickoff(target, initial_args),
        }],
    ))
    convo.messages.append(Message(
        role="assistant",
        blocks=[{
            "type": "tool_call",
            "id": call_id,
            "name": target,
            "arguments": initial_args,
        }],
    ))
    convo.messages.append(Message(
        role="user",
        blocks=[{
            "type": "tool_result",
            "tool_call_id": call_id,
            "name": target,
            "content": text,
            "is_error": is_error,
        }],
    ))

    return compose_forced_run_follow_up(target)


def _load_tool(agent: "Agent", tool_name: str) -> None:
    """Lazy-register a builtin onto the agent's registry. No-op if
    already present; prints a one-line confirmation on first load.

    For `build_workflow` specifically, we also thread an `on_built`
    callback in via `register_builtin`'s `config` so that every time
    the model successfully writes a workflow file, the agent's tool
    registry picks up the new/edited workflow as a callable tool on
    the very next turn — no CLI restart required.
    """
    config: dict[str, Any] = {}
    if tool_name == "build_workflow":
        config["on_built"] = _make_workflow_refresh_callback(agent)
    added = register_builtin(
        agent.tools, tool_name, provider=agent.provider, config=config,
    )
    if added:
        out(C.dim(f"(loaded tool: {tool_name})"))


def _make_workflow_refresh_callback(agent: "Agent"):
    """Build the `on_built` callback for `build_workflow`.

    Captures `agent` in a closure so the callback can re-register
    workflow tools on the same registry the running agent reads from.
    """
    async def _refresh(info: dict) -> None:
        # Lazy import — workflow.* pulls the provider stack which we
        # don't want to drag into cli.commands on module import.
        from ..agent.workflow import register_workflows

        wf_name = info.get("workflow_name") or "<unknown>"
        was_new = bool(info.get("created"))
        already_active = agent.tools.has(wf_name)

        registered, _skipped = await register_workflows(
            agent.tools, provider=agent.provider,
        )
        # `register_workflows` returns names of workflows it newly
        # walked off disk. With overwrite-on-name semantics in
        # `ToolRegistry.register`, edits silently re-bind the existing
        # entry to the freshly-loaded record — no special-case needed
        # here. The user-facing message just distinguishes the two
        # cases for clarity.
        if was_new and wf_name in registered:
            out(C.dim(f"(registered workflow tool: {wf_name})"))
        elif already_active:
            out(C.dim(f"(refreshed workflow tool: {wf_name})"))
        elif wf_name in registered:
            out(C.dim(f"(registered workflow tool: {wf_name})"))

    return _refresh
