"""Workflow subpackage — load on-disk workflows and expose them as tools.

Workflows are loaded from a local directory (`$BOTCIRCUITS_WORKFLOWS_DIR`
or `.botcircuits/workflows`) and driven by the embedded flow engine in
`engine/`.

Public surface:

    from botcircuits.agent.workflow import fetch_workflows, run_workflow
"""

from __future__ import annotations

import re

from botcircuits.agent.tools import LocalTool, ToolRegistry
from botcircuits.providers.base import LLMProvider
from botcircuits.agent.workflow import local
from botcircuits.agent.workflow.cli_commands import (
    compose_workflow_empty_action,
    compose_workflow_step_directive,
    render_system_notes,
)
from botcircuits.agent.workflow.engine.runner import run_workflow_engine
from botcircuits.agent.workflow.local import LocalWorkflowError, _load_workflow_record


async def fetch_workflows() -> list[dict]:
    """Return the workflow records discovered on disk.

    Each record has at least: `id`, `name`, `description`, plus the full
    flow definition under `flow`.
    """
    return await local.fetch_workflows()


async def run_workflow(
    workflow_name: str,
    args: dict,
    *,
    session_id: str | None = None,
    provider: LLMProvider | None = None,
    last_assistant_message: str = "",
    last_user_message: str = "",
    normalize_enabled: bool = True,
) -> dict:
    """Execute a workflow by name and return its result.

    `provider` + `last_assistant_message` + `last_user_message` are
    forwarded to the LLM-side variable normalizer (Layer B). They are
    optional; when omitted, normalization falls back to deterministic
    type coercion only.
    """
    return await local.run_workflow(
        workflow_name, args,
        session_id=session_id,
        provider=provider,
        last_assistant_message=last_assistant_message,
        last_user_message=last_user_message,
        normalize_enabled=normalize_enabled,
    )


_EMPTY_SCHEMA: dict = {"type": "object", "properties": {}}

# flow.variables dataType → JSON-schema type for the tool's input_schema.
_JSON_TYPE_BY_DATATYPE = {"number": "number", "boolean": "boolean"}


def _branch_input_schema(branch_variables: list[dict]) -> dict:
    """Build the tool input_schema exposing a pending branch's variables.

    The model sees these as ordinary optional tool arguments — passing
    them on the re-call is how the slots ride the main loop's tool call
    instead of being re-derived from the transcript inside the tool.
    """
    properties: dict[str, dict] = {}
    for v in branch_variables:
        name = v.get("variableName")
        if not isinstance(name, str) or not name:
            continue
        dtype = (v.get("dataType") or "string").lower()
        prop: dict = {"type": _JSON_TYPE_BY_DATATYPE.get(dtype, "string")}
        desc = v.get("description")
        if isinstance(desc, str) and desc:
            prop["description"] = desc
        properties[name] = prop
    if not properties:
        return dict(_EMPTY_SCHEMA)
    return {"type": "object", "properties": properties}


def _make_resolve_unfilled(*, provider, normalize_enabled):
    """Build the Tier-0/Tier-2 backfill hook the engine runner calls when a
    branch variable is still empty after Tier-1 (`record_slots`) capture.

    Tier 0 — the deterministic resolver (`slot_resolver.resolve_slots`):
    raw args, authored choice values, typed extraction from the last user
    message, saved slots. Zero tokens.
    Tier 2 — cheap-model semantic extraction (`variable_normalizer.normalize`),
    only for what Tier 0 leaves unresolved, only when a provider is given.
    """
    from botcircuits.agent.workflow.slot_resolver import resolve_slots
    from botcircuits.agent.workflow.variable_normalizer import normalize

    async def _resolve(*, flow, step_id, variables, slots):
        out: dict = {}
        # Tier 0 — deterministic. `last_user_message` is the freshest
        # context the resolver reads; the engine keeps it on the slots
        # context under a reserved key when available.
        last_user = slots.get("__last_user_message__", "") if isinstance(slots, dict) else ""
        resolved, unresolved = resolve_slots(
            flow=flow,
            step_id=step_id,
            variables=variables,
            raw_args={},
            saved_slots=slots,
            last_user_message=last_user,
        )
        if resolved:
            out.update(resolved)
        # Tier 2 — cheap-model fallback for the remainder.
        if provider is not None and normalize_enabled and unresolved:
            from botcircuits.agent.workflow.local import _action_text_for_step
            # Tag the cheap-model fallback call so eval token accounting can
            # break it out from segment / conversational usage (§7).
            try:
                provider.usage_purpose = "tier2_normalization"
            except Exception:
                pass
            extracted = await normalize(
                provider=provider,
                variables=unresolved,
                raw_args={**slots, **out},
                action_text=_action_text_for_step(flow, step_id),
                last_assistant_message="",
                last_user_message=last_user,
            )
            if extracted:
                out.update(extracted)
        return out

    return _resolve


async def _run_engine(
    wf_name: str,
    args: dict,
    state: dict,
    run_segment,
    *,
    provider: LLMProvider | None,
    normalize_enabled: bool,
    last_user_message: str = "",
    event_sink=None,
    runtime=None,
) -> str:
    """Engine-driven execution: the runner owns the loop, calling
    `run_segment` (the agent's `_run_segment`) once per branch-delimited
    segment. Returns one summary line on completion, or the pending
    question when the workflow pauses for the user.

    `runtime`, when given, is an `AgentRuntimeProvider` (e.g. a CLI host like
    claude-code) supplying BOTH the segment runner and the slot-resolution
    hook — the "use an existing agent as the loop provider" path. When None
    (the in-process default), `run_segment` is the native agent callback and
    slot resolution is the local Tier-0/Tier-2 closure. Either way the engine
    itself is identical: it can't tell the providers apart.

    Pause/resume: on a user-interaction pause the runner yields; we stash
    the resume cursor + accumulated slots on `state` so the next call (the
    user's reply) continues from the same segment. On completion or when
    no workflow is active, `state` is reset so a fresh request restarts.
    """
    record = _load_workflow_record(wf_name)
    flow = record.get("flow")
    if not isinstance(flow, dict):
        raise LocalWorkflowError(f"workflow {wf_name!r} is missing flow")

    # Resume from a prior pause if there is one; otherwise start fresh and
    # seed slots from the trigger call's args.
    resume_step = state.get("engine_paused_step")
    slots = dict(state.get("engine_slots") or {})
    if resume_step is None:
        slots.update({k: v for k, v in (args or {}).items() if v not in (None, "")})
    # Reserved key the Tier-0 resolver reads as the freshest user context
    # (deterministic choice-value / typed extraction). Stripped from the
    # final slots before the summary so it never leaks into output.
    if last_user_message:
        slots["__last_user_message__"] = last_user_message

    # A runtime provider supplies its own segment runner + resolve hook; in
    # the native default we keep the passed-in callback and build the local
    # Tier-0/Tier-2 closure.
    if runtime is not None:
        run_segment = lambda **kw: runtime.run_segment(event_sink=event_sink, **kw)
        resolve_unfilled = lambda **kw: runtime.resolve_slots(**kw)
    else:
        resolve_unfilled = _make_resolve_unfilled(
            provider=provider, normalize_enabled=normalize_enabled,
        )
    result = await run_workflow_engine(
        flow,
        workflow_name=wf_name,
        run_segment=run_segment,
        start_step_id=resume_step,
        slots=slots,
        resolve_unfilled=resolve_unfilled,
        event_sink=event_sink,
    )

    if result.paused:
        # Park: remember where to resume and what we've collected.
        state["engine_paused_step"] = result.paused_step or resume_step
        state["engine_slots"] = result.slots
        state["session_id"] = wf_name  # mark active for active_workflow_names
        state["finished_quietly"] = False
        return result.question or "(workflow is waiting for your input)"

    # Completed — reset so the next request restarts cleanly.
    state["engine_paused_step"] = None
    state["engine_slots"] = {}
    state["session_id"] = None
    state["finished_quietly"] = True
    return result.summary


def workflow_tool(
    record: dict,
    *,
    provider: LLMProvider | None = None,
    normalize_enabled: bool = True,
    runtime=None,
) -> LocalTool:
    """Wrap a workflow record into a `LocalTool` the agent can call.

    A workflow is multi-turn: one call returns one `AGENT_ACTION` for the
    LLM to act on, and the engine keeps the saved session in memory keyed
    by `session_id` until the workflow ends. We keep that session_id in
    the tool's closure so the next invocation re-enters the same
    workflow conversation instead of starting a new one.

    When the engine pauses on a *branching* step, the result carries the
    filtered variable schema the branch references (`branch_variables`).
    The handler then (a) widens the tool's `input_schema` to expose those
    variables and (b) appends a directive line asking the model to
    re-call the tool with the observed values — so the slots arrive as
    plain tool-call args (the slot resolver's highest-priority source)
    instead of being re-derived from a transcript snapshot. The agent
    loop's empty-args auto-recall remains as the fallback when the model
    doesn't re-call.

    `provider` enables Layer B (LLM-driven variable normalization) on
    workflow re-entry. The tool's handler accepts an optional `context`
    dict (filled by the agent loop) carrying `last_assistant_message`
    and `last_user_message`, which Layer B uses as part of its
    hallucination-guard source.

    `runtime`, when given, is an `AgentRuntimeProvider` (e.g. claude-code via
    CLI). It makes the engine drive the workflow through that external agent
    instead of the in-process loop — this is the path the workflow-running
    SKILL uses on a host that is NOT the native BotCircuits agent. When a
    runtime is set the engine path is taken unconditionally (it doesn't need
    a `run_segment` callback from the agent loop, since the runtime supplies
    its own).
    """
    wf_name = record["name"]
    # Always carry an explicit, deterministic invocation rule: smaller
    # models otherwise treat "run <name> workflow" as an open-ended coding
    # request and ask clarifying questions instead of calling the tool.
    wf_desc = (
        (record.get("description") or f"Run workflow {wf_name}.")
        + f" Deterministic workflow — call this tool IMMEDIATELY (no "
          f"arguments needed) whenever the user asks to run/start/execute "
          f"'{wf_name}'; never ask clarifying questions first. The engine "
          f"owns the steps and will itself ask the user when it needs input."
    )

    state: dict[str, object] = {
        "session_id": None,
        "branch_variables": [],
        "finished_quietly": False,
        # Engine-driven mode: the runner pauses on a user-interaction step
        # and stashes its resume cursor here so the next call continues.
        "engine_paused_step": None,
        "engine_slots": {},
    }

    async def _handler(args: dict, context: dict | None = None) -> str:
        ctx = context or {}

        # Engine-driven path. Taken when EITHER a runtime provider is bound
        # (external agent like claude-code drives the loop) OR the in-process
        # agent loop supplied a `run_segment` callback (native). In both cases
        # the ENGINE owns the loop — one call drives the whole workflow (or up
        # to the next user-interaction pause) and returns a single summary
        # line, instead of yielding one step at a time.
        run_segment = ctx.get("run_segment")
        if runtime is not None or run_segment is not None:
            return await _run_engine(
                wf_name, args, state, run_segment,
                provider=provider, normalize_enabled=normalize_enabled,
                last_user_message=ctx.get("last_user_message", ""),
                event_sink=ctx.get("event_sink"),
                runtime=runtime,
            )

        result = await run_workflow(
            wf_name, args,
            session_id=state["session_id"],
            provider=provider,
            last_assistant_message=ctx.get("last_assistant_message", ""),
            last_user_message=ctx.get("last_user_message", ""),
            normalize_enabled=normalize_enabled,
        )
        action = result.get("action")
        done = bool(result.get("done"))
        kind = result.get("kind")
        branch_variables = result.get("branch_variables") or []
        system_notes = result.get("system_notes") or []

        # Reset the closure's session_id as soon as the workflow finishes
        # so the next user request starts a fresh run. We do this even
        # when `action` is set because the engine returns `done=True`
        # together with the final state's action payload.
        if done:
            state["session_id"] = None
            branch_variables = []
        else:
            state["session_id"] = result.get("session_id")

        # Track the pending branch's variables and mirror them onto the
        # tool's input_schema so the next provider call advertises them.
        # `tool` is the LocalTool constructed below; the closure cell
        # resolves at call time, after construction.
        state["branch_variables"] = branch_variables
        tool.input_schema = _branch_input_schema(branch_variables)

        # A "quiet finish": the workflow ended and the trailing steps were
        # all engine-side bookkeeping (systemAction) — nothing left for the
        # model to perform. The agent loop reads this flag (via
        # `workflow_finished_quietly`) to end the turn WITHOUT another
        # provider call when its own auto-recall produced this result.
        state["finished_quietly"] = bool(done and not action)

        notes_block = render_system_notes(system_notes)

        # No action and not done shouldn't happen with a well-formed STM,
        # but guard against it so the LLM gets a clear signal instead of
        # an empty string. (With trailing systemActions this is a NORMAL
        # terminal shape — the notes carry what the engine recorded.)
        if not action:
            finished = compose_workflow_empty_action(wf_name)
            return f"{notes_block}\n\n{finished}" if notes_block else finished

        # Frame the action as a directive, not a status update — the LLM
        # has to perform it (tool call, human_feedback question, message,
        # skill, etc.) before the workflow can advance. A `question`-kind
        # step forces a `human_feedback` call (which pauses the loop);
        # a branching step asks the model to re-call this tool with the
        # branch variables once the step is done; anything else
        # auto-advances via the agent loop's recall. Wording is shared
        # with the out-of-process tool wrapper (Hermes) via cli_commands.
        directive = compose_workflow_step_directive(
            wf_name, done=done, kind=kind,
            branch_variables=branch_variables,
        )
        text = directive.as_plain_text(action)
        return f"{notes_block}\n\n{text}" if notes_block else text

    # Seed the FIRST call's schema from the workflow's declared top-level
    # `flow.variables` (not just branch variables revealed after a pause).
    # Without this, a model literally cannot pass `topic`/`research_depth`-
    # style inputs on the initial call — the schema would have no
    # properties — forcing every run through a human_feedback pause even
    # when the caller already has the values. Passing extra (output-only)
    # variables as args is harmless: they just merge into slots the
    # workflow overwrites anyway.
    declared_variables = record.get("flow", {}).get("variables") or []
    initial_schema = _branch_input_schema(declared_variables)

    tool = LocalTool(
        name=wf_name,
        description=wf_desc,
        input_schema=initial_schema,
        handler=_handler,
    )
    # Expose the session state so the agent loop can detect that this
    # workflow is mid-execution and remind the model to re-enter it.
    tool._workflow_state = state  # type: ignore[attr-defined]
    return tool


def workflow_branch_variables(reg: ToolRegistry, name: str) -> list[dict]:
    """The pending branch's variable schema for the named workflow tool.

    Non-empty only while the workflow is paused on a branching step.
    The agent loop reads this to tell the model, via the `[Active
    workflow]` reminder, to re-call the tool with those values once the
    current step is done.
    """
    for tool in reg.all():
        if tool.name != name:
            continue
        state = getattr(tool, "_workflow_state", None)
        if isinstance(state, dict):
            variables = state.get("branch_variables")
            if isinstance(variables, list):
                return variables
    return []


def workflow_finished_quietly(reg: ToolRegistry, name: str) -> bool:
    """True when the named workflow tool's LAST call ended the workflow with
    nothing left for the model to perform (the trailing steps were all
    engine-side systemActions). The agent loop checks this after its own
    auto-recall: if every recalled workflow finished quietly, the model's
    previous text already was the final answer, so the loop ends the turn
    instead of spending another provider call on a restatement."""
    for tool in reg.all():
        if tool.name != name:
            continue
        state = getattr(tool, "_workflow_state", None)
        if isinstance(state, dict):
            return bool(state.get("finished_quietly"))
    return False


def active_workflow_names(reg: ToolRegistry) -> list[str]:
    """Names of workflow tools on `reg` that have a live session_id.

    A workflow tool is "active" between its first call (which returns a
    pending step) and the call that finishes the workflow. The agent
    loop reads this to inject a per-turn reminder pushing the model to
    re-enter the workflow.
    """
    active: list[str] = []
    for tool in reg.all():
        state = getattr(tool, "_workflow_state", None)
        if isinstance(state, dict) and state.get("session_id"):
            active.append(tool.name)
    return active


async def collect_agents_config() -> dict[str, dict]:
    """Merge every discovered workflow's top-level `agents` map into one
    `{agent_name: {provider?, model?, runtime?}}` dict.

    Used by the CLI to seed a native `Agent`'s `agents_config` before the
    Agent is built, so a workflow step pinned to a named agent can resolve
    that agent's in-process model (see `Agent._resolve_segment_provider`).
    Later workflows win on a name clash.
    """
    merged: dict[str, dict] = {}
    for record in await fetch_workflows():
        agents_map = record.get("agents")
        if isinstance(agents_map, dict):
            for a_name, a_cfg in agents_map.items():
                if isinstance(a_cfg, dict):
                    merged[a_name] = a_cfg
    return merged


def workflow_tool_names(reg: ToolRegistry) -> list[str]:
    """Names of ALL workflow tools on `reg` (active or not) — the set the
    loop's deterministic trigger matches user messages against."""
    return [t.name for t in reg.all()
            if getattr(t, "_workflow_state", None) is not None]


#: Verbs that make a message a run request …
_TRIGGER_VERBS = ("run", "start", "execute", "launch", "kick off", "trigger")
#: … unless it opens interrogatively (asking ABOUT a workflow, not for it).
_QUESTION_OPENERS = ("how", "what", "why", "when", "where", "who", "which",
                     "explain", "describe", "can ", "could", "should", "is ",
                     "does", "do ")


def match_workflow_trigger(text: str, names: list[str]) -> str | None:
    """Deterministic routing for "run <workflow>"-style requests.

    Returns the workflow name when `text` contains a trigger verb AND names
    a registered workflow as a standalone token; None otherwise. Questions
    ("how do I run ai_trends?") don't trigger. Longest name wins so
    `order_fulfillment_eu` isn't shadowed by `order_fulfillment`.

    This exists because tool routing must not depend on the model: smaller
    models answer "run ai_trends workflow" with clarifying questions
    instead of calling the tool. The agent loop checks this BEFORE the
    provider call and invokes the workflow tool itself — same philosophy
    as the auto-resume of a paused workflow.
    """
    lowered = text.strip().lower()
    if lowered.startswith(_QUESTION_OPENERS) or lowered.endswith("?"):
        return None
    if not any(verb in lowered for verb in _TRIGGER_VERBS):
        return None
    for name in sorted(names, key=len, reverse=True):
        if re.search(rf"(?<![\w-]){re.escape(name.lower())}(?![\w-])", lowered):
            return name
    return None


def workflows_system_prompt(names: list[str]) -> str:
    """A system-prompt block advertising the registered workflow tools with
    a deterministic invocation rule.

    The per-tool description already carries the rule, but a small model
    scanning many tools can still miss it; naming the workflows in the
    system prompt makes "run <name> workflow" → "call the tool named
    <name>" an explicit instruction instead of an inference. Returns ""
    when no workflows are registered, so callers can append it blindly.
    """
    if not names:
        return ""
    listed = ", ".join(sorted(names))
    return (
        "\n\n## Workflows\n"
        f"These deterministic workflows are available as tools, named exactly "
        f"after the workflow: {listed}.\n"
        "When the user asks to run/start/execute one of them (e.g. \"run "
        "<name> workflow\"), call that tool immediately with no arguments — "
        "do NOT ask clarifying questions first. The workflow engine drives "
        "the steps deterministically and pauses to ask the user itself "
        "whenever it needs input. If the tool result is a question, relay it "
        "to the user verbatim; when the workflow completes, relay its summary."
    )


async def register_workflows(
    reg: ToolRegistry,
    *,
    provider: LLMProvider | None = None,
    normalize_enabled: bool = True,
    runtime=None,
    agent=None,
) -> tuple[list[str], list[str]]:
    """Discover workflows on disk and register each as a LocalTool on `reg`.

    Built-in tools take precedence: a workflow whose name collides with
    an already-registered tool is skipped (not registered) so user-defined
    workflows can never override built-ins.

    Pass `provider` (typically the same one the agent's `LLMProvider` is
    built from) to enable Layer B variable normalization on workflow
    re-entry. Set `normalize_enabled=False` to register the tools but
    skip B even when a provider is available.

    Pass `agent` (the live `Agent`) to thread each workflow's top-level
    `agents` map into the agent's `_agents_config`, so the in-process
    (native) runtime can honor a step's per-agent `model` binding (see
    `Agent._resolve_segment_provider`). Maps from all discovered workflows
    are merged; later workflows win on a name clash. Harmless for the CLI
    runtimes, which resolve per-agent models through their own path.

    Returns `(registered, skipped)` — both lists of workflow names.
    Names already bound to a non-workflow tool (a builtin or MCP tool)
    are reported as `skipped`. Names already bound to an EARLIER
    workflow tool are re-registered: this is what lets the CLI re-run
    `register_workflows` after the user edits a workflow on disk and
    have the agent pick up the new description / state map on the
    very next turn.
    """
    records = await fetch_workflows()
    registered: list[str] = []
    skipped: list[str] = []
    merged_agents: dict[str, dict] = {}
    for record in records:
        agents_map = record.get("agents")
        if isinstance(agents_map, dict):
            for a_name, a_cfg in agents_map.items():
                if isinstance(a_cfg, dict):
                    merged_agents[a_name] = a_cfg
        tool = workflow_tool(
            record,
            provider=provider,
            normalize_enabled=normalize_enabled,
            runtime=runtime,
        )
        if reg.has(tool.name):
            existing = next(
                (t for t in reg.all() if t.name == tool.name), None,
            )
            # Only skip when colliding with a non-workflow tool (i.e. a
            # builtin or MCP tool). Workflow tools are tagged with
            # `_workflow_state` by `workflow_tool()`; overwriting one
            # with the freshly-loaded record is the whole point of
            # re-running this function.
            if existing is None or getattr(
                existing, "_workflow_state", None,
            ) is None:
                skipped.append(tool.name)
                continue
        reg.register(tool)
        registered.append(tool.name)

    # Thread the discovered per-agent bindings onto the live Agent so the
    # native runner can resolve a step's `agent` to an in-process
    # provider/model. Merge (don't replace) so a re-run after editing one
    # workflow keeps bindings from the others.
    if agent is not None and merged_agents:
        existing = getattr(agent, "_agents_config", None)
        if isinstance(existing, dict):
            existing.update(merged_agents)
        else:
            agent._agents_config = dict(merged_agents)

    return registered, skipped


__all__ = [
    "LocalWorkflowError",
    "fetch_workflows",
    "run_workflow",
    "workflow_tool",
    "active_workflow_names",
    "workflow_branch_variables",
    "register_workflows",
    "collect_agents_config",
    "_make_resolve_unfilled",
]
