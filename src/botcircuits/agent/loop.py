"""Agent — the multi-round tool-use drive loop.

Coordinates a single `LLMProvider`, a `ToolRegistry`, optional MCP
servers, and optional skills. Owns the `ConversationStore` so callers can
resume sessions across calls. Segment execution for engine-driven
workflows lives in `agent/segments.py` (mixed in as `SegmentRunner`);
context extraction in `agent/context.py`; event mapping in
`agent/events.py`.

Use as an async context manager:

    async with Agent(provider=...) as agent:
        reply, sid = await agent.chat("hello")
        async for ev in agent.chat_stream("..."):
            ...
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Literal
from uuid import uuid4

from botcircuits.providers.base import LLMProvider
from botcircuits.types import LLMResponse, Message, StreamEvent, ToolCall
from botcircuits.agent.context import last_assistant_text, last_user_text
from botcircuits.agent.events import human_feedback_pause, segment_stream_events
from botcircuits.agent.mcp import LocalMCPManager, MCPServer
from botcircuits.agent.react import (
    format_observation,
    parse_react_step,
    render_react_preamble,
)
from botcircuits.agent.segments import SegmentRunner, fired_workflow_tool
from botcircuits.agent.sessions import ConversationStore
from botcircuits.agent.skill import (
    DEFAULT_SKILL_ROOTS,
    SkillSpec,
    discover_skills,
    skill_to_tool,
)
from botcircuits.agent.subagents import delegate_tool, fan_out_tool
from botcircuits.agent.tools import ToolRegistry
from botcircuits.agent.verification import (
    changed_code,
    observed_pass,
    test_command,
    verification_nudge,
)
from botcircuits.agent.workflow import (
    CODING_PIPELINE_WORKFLOW,
    active_workflow_names,
    is_coding_request,
    match_workflow_trigger,
    strip_workflow_trigger,
    workflow_tool_names,
)

MAX_AGENT_STEPS = 500

# Synthetic id prefix for the workflow tool call the loop injects to resume
# a paused workflow on the user's next message. Lets us tell loop-injected
# calls apart from model-issued ones in history if needed.
_AUTO_RESUME_ID_PREFIX = "wf-autoresume-"


class Agent(SegmentRunner):
    def __init__(
        self,
        provider: LLMProvider,
        tools: ToolRegistry | None = None,
        mcp_servers: list[MCPServer] | None = None,
        skills: list[SkillSpec] | None = None,
        local_skills_paths: list[str | Path] | None = None,
        max_tokens: int = 4096,
        max_steps: int = MAX_AGENT_STEPS,
        store: ConversationStore | None = None,
        enable_workflows: bool = True,
        mode: Literal["native", "react"] = "native",
        agents_config: dict[str, dict] | None = None,
        enable_subagents: bool = True,
        verify_attempts: int = 3,
        require_run: bool = True,
        agents_dir: str | Path = ".",
        enable_coding_pipeline: bool = True,
    ):
        self.provider = provider
        self.user_tools = tools or ToolRegistry()
        # Agent name -> {"provider": "openai", "model": "..."} for workflow
        # steps pinned to a named agent. When a segment is pinned to one of
        # these AND the binding names an in-process provider we can build, the
        # in-process runner swaps to that provider/model for JUST that segment
        # (see `SegmentRunner._resolve_segment_provider`). Bindings that only
        # name a CLI runtime (e.g. claude-code) or an alias we can't build
        # fall back to `self.provider` — native never spawns a CLI here.
        # Cached by (provider, model) so agents sharing a binding reuse one
        # client.
        self._agents_config = agents_config or {}
        self._provider_cache: dict[tuple[str, str | None], LLMProvider] = {}
        self.skills = skills or []
        self.max_tokens = max_tokens
        self.max_steps = max_steps
        self.store = store or ConversationStore()
        # Tool-use strategy:
        #   "native" — hand tools to the provider's structured tool-use API
        #     and read resp.tool_calls back. The default; most robust.
        #   "react"  — describe tools in the system prompt and parse a
        #     Thought/Action/Action Input text block out of the model's
        #     reply (see agent/react.py). Works on any provider and yields
        #     a visible reasoning trace, at the cost of parse brittleness.
        self.mode = mode
        # When False, workflow tools registered on the registry are
        # hidden from the provider call entirely. The tools stay in the
        # registry (so other code can still inspect them) — only the
        # surface exposed to the LLM changes. Default True keeps the
        # existing behavior for every caller that doesn't ask
        # otherwise; the eval framework's "no workflow" baseline mode
        # is the one consumer that flips this off.
        self.enable_workflows = enable_workflows
        # When True, `start()` registers the `delegate` / `fan_out` tools so
        # the model can spawn isolated subagents. Workers spawned by the
        # subagents/orchestration modules set this False — no recursion.
        self.enable_subagents = enable_subagents
        # Verification (enforced-run gate): when a turn changes code and the
        # project declares a test command (AGENTS.md `## Testing` under
        # `agents_dir`), refuse "done" until a real passing shell_exec run of
        # that command is observed in this turn's transcript, feeding the
        # demand back up to `verify_attempts` times. `require_run=False`
        # opts out entirely.
        self.verify_attempts = verify_attempts
        self.require_run = require_run
        self.agents_dir = Path(agents_dir)
        # Deterministic coding-task routing: when True (the default) and the
        # static coding pipeline workflow (`CODING_PIPELINE_WORKFLOW`) is
        # registered, a message detected as a coding request
        # (`is_coding_request`) is handed straight to that pipeline instead
        # of the normal model-driven turn — the same before-any-provider-call
        # philosophy as the workflow trigger route. No-op when the pipeline
        # workflow isn't on disk, so it's safe to leave on everywhere.
        self.enable_coding_pipeline = enable_coding_pipeline

        # Roots scanned for filesystem skills on start(). Callers can
        # pass an explicit list (or []) to override the default project
        # locations. Paths are resolved against the current working
        # directory at discovery time, not __init__ time, so an Agent
        # created in one cwd and started in another still finds the
        # right skills.
        raw_roots = (local_skills_paths
                     if local_skills_paths is not None
                     else list(DEFAULT_SKILL_ROOTS))
        self.local_skills_paths: list[Path] = [Path(p) for p in raw_roots]
        self.local_skills: list = []  # populated on start()

        # Split MCP servers by mode; auto-promote hosted -> local on
        # providers that don't support hosted MCP, so a single config
        # works across providers.
        servers = mcp_servers or []
        if not provider.supports_hosted_mcp():
            for s in servers:
                if s.mode == "hosted":
                    print(f"[info] Provider '{provider.name}' lacks hosted MCP; "
                          f"running '{s.name}' locally.")
                    s.mode = "local"

        self.hosted_mcp = [s for s in servers if s.mode == "hosted"]
        self._local_mcp = LocalMCPManager([s for s in servers if s.mode == "local"])
        self._tools_built = False
        # Carry over the permission rules from the caller's registry (set
        # via `default_registry(..., permissions=...)`) — `start()` below
        # re-registers each LocalTool individually into this fresh
        # registry, which would otherwise silently drop them.
        self.tools = ToolRegistry(permissions=self.user_tools.permissions)

    # -- async lifecycle ----------------------------------------------------

    async def __aenter__(self) -> "Agent":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()

    async def start(self) -> None:
        """Open MCP sessions and merge user tools + MCP tools + filesystem
        skills into one registry."""
        if self._tools_built:
            return
        await self._local_mcp.start()
        for t in self.user_tools.all():
            self.tools.register(t)
        for t in self._local_mcp.tools():
            self.tools.register(t)
        # Filesystem skills become tools. They run last so user tools
        # and MCP tools win on name collisions — a skill named `shell`
        # shouldn't shadow the real shell tool. Skills marked
        # `disable-model-invocation: true` are still loaded (so the CLI
        # can dispatch them on /skill-name) but not registered as
        # callable tools.
        self.local_skills = discover_skills(self.local_skills_paths)
        for sk in self.local_skills:
            if sk.disable_model_invocation:
                continue
            if self.tools.has(sk.name):
                continue
            self.tools.register(skill_to_tool(sk))
        # Subagent spawning (delegate / fan_out), bound to this live agent so
        # subagents see the final merged registry. Registered last, and never
        # over a user tool of the same name.
        if self.enable_subagents:
            for factory in (delegate_tool, fan_out_tool):
                tool = factory(self)
                if not self.tools.has(tool.name):
                    self.tools.register(tool)
        self._tools_built = True

    async def aclose(self) -> None:
        # Terminate any background shell processes the model started.
        # Imported lazily so this module doesn't pull tools at import time
        # (tools depend on registry, which lives next door).
        from .tools.builtins import _bg
        await _bg.terminate_all()
        await self._local_mcp.stop()
        await self.provider.aclose()

    @asynccontextmanager
    async def _turn(self, convo):
        """One serialized turn on `convo`: hold the session lock, and persist
        the session however the turn ends (terminal reply, human_feedback
        pause, step cap, or an exception) — a no-op on the in-memory store."""
        async with convo.lock:
            try:
                yield
            finally:
                self.store.persist(convo.session_id)

    # -- mode strategy (native tool-use vs ReAct) ----------------------------

    def _exposed_tools(self) -> list:
        """Tool list handed to the provider's structured tool-use API.

        With workflows enabled (the default) this is just every tool
        on the registry. With workflows disabled, workflow tools —
        identified by the `_workflow_state` attribute set by
        `workflow.workflow_tool()` — are filtered out. They stay in
        the registry so the rest of the agent code can still see them
        (the eval framework, /tools listing, etc.); only the surface
        the model sees changes.

        In ReAct mode this returns [] — tools are described in the
        system prompt instead (see `_react_tools` / `_system_for_call`),
        so the provider gets no structured tool spec.
        """
        if self.mode == "react":
            return []
        return self._react_tools()

    def _react_tools(self) -> list:
        """The tools the model is allowed to use this call, after workflow
        gating. Shared by both modes: native passes these to the provider's
        tool API, react renders them into the prompt preamble."""
        all_tools = self.tools.all()
        if self.enable_workflows:
            return all_tools
        return [t for t in all_tools
                if getattr(t, "_workflow_state", None) is None]

    def _system_for_call(self, system: str | None) -> str | None:
        """System prompt with mode-specific blocks attached.

        ReAct mode appends the tool preamble that teaches the
        Thought/Action/Action Input format — without it the model has no
        way to know which tools exist, since they never reach the
        provider's tool API. Native mode returns `system` unchanged:
        workflow tools are presented like any other tool (name +
        description), and an already-paused workflow is resumed directly
        by the loop (see `_resume_active_workflow`), not via a prompt
        reminder telling the model to re-call it.
        """
        if self.mode == "react":
            preamble = render_react_preamble(self._react_tools())
            if preamble:
                system = (system or "") + preamble
        return system

    def _interpret(self, resp: LLMResponse) -> tuple[str, list[ToolCall], bool]:
        """Normalize a provider response into (assistant_text, tool_calls,
        is_terminal) so the rest of the loop is mode-agnostic.

        - native: tool calls come straight off `resp.tool_calls`; the turn
          is terminal when the model didn't ask for tools.
        - react: parse the reply text for a Thought/Action block. A parsed
          Action becomes a single-element tool_calls list; a Final Answer
          (or unparseable text) is terminal with the answer as the text.

        `assistant_text` is what we persist as the assistant turn's text
        block. In react mode that's the full reasoning trace (so the
        Thought/Action lines stay in history and the model sees its own
        prior format), except on a terminal turn where we store the clean
        Final Answer rather than the scaffolding.
        """
        if self.mode != "react":
            terminal = resp.stop_reason != "tool_use" or not resp.tool_calls
            return resp.text, resp.tool_calls, terminal

        step = parse_react_step(resp.text)
        if step.action is None:
            # Terminal: store the extracted Final Answer, not the raw
            # "Thought: ... Final Answer: ..." scaffolding.
            return step.final or resp.text, [], True
        # Non-terminal: keep the full trace (Thought + Action) in history.
        return resp.text, [step.action], False

    def _result_message(
        self,
        tool_calls: list[ToolCall],
        results: list[tuple[str, bool]],
    ) -> Message:
        """Pack tool outputs into the user-role message fed back to the model.

        Native mode uses structured `tool_result` blocks keyed by call id —
        the provider matches them to the original tool_use blocks. ReAct
        mode instead emits a plain-text `Observation:` block, because the
        model was prompted to expect that literal format in its transcript;
        feeding back structured blocks it never produced would break the
        format it's imitating. ReAct is one-action-per-turn, so there's a
        single observation.
        """
        if self.mode == "react":
            output, is_error = results[0]
            return Message(role="user", blocks=[{
                "type": "text",
                "text": format_observation(output, is_error),
            }])
        result_blocks = [
            {
                "type": "tool_result",
                "tool_call_id": tc.id,
                "name": tc.name,
                "content": output,
                "is_error": is_error,
            }
            for tc, (output, is_error) in zip(tool_calls, results)
        ]
        return Message(role="user", blocks=result_blocks)

    # -- workflow resume ----------------------------------------------------

    async def _resume_active_workflow(
        self,
        convo,
        *,
        event_sink=None,
    ) -> tuple[ToolCall, str, bool] | None:
        """If a workflow is paused waiting on the user, resume it directly —
        no provider call, no model decision involved.

        `active_workflow_names` is non-empty only between an engine
        user-interaction pause and its resolution (see `workflow_tool`'s
        `_run_engine`), so by construction the just-appended user message IS
        the answer to that pause. Resuming is therefore deterministic: call
        the SAME tool handler the model would have called, with the SAME
        `run_segment` callback, but trigger it from the loop itself instead
        of waiting for (and prompting for) a model tool-call decision.

        Returns `(tool_call, output, is_error)` for the synthetic call so
        callers can append it to history / stream it like any other tool
        round, or `None` when no workflow is paused.
        """
        if not self.enable_workflows:
            return None
        names = active_workflow_names(self.tools)
        if not names:
            return None
        return await self._call_workflow_tool(convo, names[0],
                                              event_sink=event_sink)

    async def _auto_workflow_call(
        self,
        convo,
        user_input: str,
        *,
        event_sink=None,
    ) -> tuple[ToolCall, str, bool] | None:
        """The loop's deterministic workflow entry points, checked BEFORE any
        provider call:

        1. resume — a paused workflow consumes the user message as its answer
           (see `_resume_active_workflow`);
        2. trigger — an explicit "run <workflow>" request invokes that
           workflow tool directly. Tool routing must not depend on the
           model: smaller models answer a run request with clarifying
           questions instead of calling the tool.
        3. coding — a message detected as a request to write/change code
           (`is_coding_request`) is routed to the static coding PIPELINE
           workflow, which derives requirements, plans, generates + runs a
           per-task coding workflow, and validates it in a gated loop. Same
           deterministic-before-the-model rationale as the trigger route.

        Returns the synthetic call's `(tool_call, output, is_error)` or None
        when none apply (the normal model-driven turn proceeds).
        """
        resumed = await self._resume_active_workflow(convo,
                                                     event_sink=event_sink)
        if resumed is not None:
            return resumed
        if not self.enable_workflows:
            return None
        name = match_workflow_trigger(user_input,
                                      workflow_tool_names(self.tools))
        if name is not None:
            # The trigger message is COMMAND, not input: hand the workflow
            # only what remains after the trigger phrase is stripped, so slot
            # extraction can't mistake "run <name>" for a variable value.
            return await self._call_workflow_tool(
                convo, name, event_sink=event_sink,
                last_user_message=strip_workflow_trigger(user_input, name),
            )
        # Coding route: only when enabled, the message looks like a coding
        # task, and the pipeline workflow is actually registered on disk.
        if (self.enable_coding_pipeline
                and CODING_PIPELINE_WORKFLOW in workflow_tool_names(self.tools)
                and is_coding_request(user_input)):
            # The full message IS the task description — pass it through
            # untouched (unlike the trigger route, there's no command phrase
            # to strip) so the pipeline's requirements step sees everything.
            return await self._call_workflow_tool(
                convo, CODING_PIPELINE_WORKFLOW, event_sink=event_sink,
                last_user_message=user_input,
            )
        return None

    async def _call_workflow_tool(
        self,
        convo,
        name: str,
        *,
        event_sink=None,
        last_user_message: str | None = None,
    ) -> tuple[ToolCall, str, bool]:
        """Invoke workflow tool `name` with a loop-injected (synthetic) call
        and append the round to history, exactly as if the model had asked
        for it. `last_user_message`, when given, overrides the transcript's
        last user text in the tool context (the trigger path passes the
        message with its command phrase stripped)."""
        tc = ToolCall(id=f"{_AUTO_RESUME_ID_PREFIX}{uuid4().hex[:8]}",
                      name=name, arguments={})
        tool_context = {
            "last_assistant_message": last_assistant_text(convo.messages),
            "last_user_message": (last_user_message
                                  if last_user_message is not None
                                  else last_user_text(convo.messages)),
            "session_id": convo.session_id,
            "run_segment": self._make_segment_runner(event_sink=event_sink),
            "event_sink": event_sink,
        }
        out, err = await self.tools.run(tc.name, tc.arguments, tool_context)

        convo.messages.append(Message(role="assistant", blocks=[{
            "type": "tool_call", "id": tc.id, "name": tc.name,
            "arguments": tc.arguments, "thought_signature": None,
        }]))
        convo.messages.append(self._result_message([tc], [(out, err)]))
        return tc, out, err

    # -- verification (enforced-run gate) -------------------------------------

    def _verify_nudge(self, messages: list[Message], turn_start: int,
                      state: dict) -> str | None:
        """If this turn changed code and the project's declared test command
        hasn't been OBSERVED passing in the transcript, return the demand to
        feed back (the loop continues); else None (accept the reply).

        The model runs the test itself with shell_exec; the harness only
        watches the receipts — a narrated "it works" is never enough. Capped
        at `verify_attempts`; exhausted attempts accept the last reply so the
        turn still ends (the failure is visible in the transcript)."""
        if not self.require_run:
            return None
        command = state.get("command")
        if command is None:
            command = state["command"] = test_command(self.agents_dir) or ""
        if not command or not changed_code(messages, turn_start):
            return None
        if observed_pass(messages, turn_start, command):
            return None
        if state.get("attempts", 0) >= self.verify_attempts:
            return None
        state["attempts"] = state.get("attempts", 0) + 1
        return verification_nudge(command)

    # -- non-streaming chat -------------------------------------------------

    async def chat(self, user_input: str, session_id: str | None = None,
                   system: str | None = None) -> tuple[str, str]:
        """Send a user message, run the loop until the model stops asking
        for tools, return (assistant_text, session_id)."""
        if not self._tools_built:
            await self.start()

        convo = self.store.get_or_create(session_id, system=system)
        async with self._turn(convo):
            turn_start = len(convo.messages)
            verify_state: dict = {}
            convo.messages.append(Message(
                role="user",
                blocks=[{"type": "text", "text": user_input}],
            ))

            # Deterministic workflow entry, before any model decision:
            # resume a paused workflow (the message IS its answer), or
            # trigger one the user explicitly asked to run by name. The
            # next provider call (below) sees the result and relays it.
            await self._auto_workflow_call(convo, user_input)

            for _ in range(self.max_steps):
                self.provider.usage_purpose = "conversational"
                resp = await self.provider.complete(
                    system=self._system_for_call(convo.system),
                    messages=convo.messages,
                    tools=self._exposed_tools(),
                    hosted_mcp=self.hosted_mcp, skills=self.skills,
                    max_tokens=self.max_tokens,
                )
                # Snapshot this call's usage so it can be retagged `trigger`
                # below if it turns out to have fired a workflow tool.
                conv_call = self.provider.last_call_usage()

                text, tool_calls, terminal = self._interpret(resp)

                assistant_blocks: list[dict] = []
                if text:
                    assistant_blocks.append({"type": "text", "text": text})
                for tc in tool_calls:
                    assistant_blocks.append({
                        "type": "tool_call",
                        "id": tc.id, "name": tc.name, "arguments": tc.arguments,
                        # Carried for providers that must replay it (Gemini).
                        "thought_signature": getattr(tc, "thought_signature", None),
                    })
                convo.messages.append(Message(role="assistant",
                                              blocks=assistant_blocks))

                if terminal:
                    # Verification gate: a code-changing turn must show a
                    # real passing test run before "done" is accepted.
                    nudge = self._verify_nudge(convo.messages, turn_start,
                                               verify_state)
                    if nudge is None:
                        return text, convo.session_id
                    convo.messages.append(Message(
                        role="user",
                        blocks=[{"type": "text", "text": nudge}],
                    ))
                    continue

                # Build tool-invocation context once per turn. The same
                # snapshot is handed to every tool call in this round.
                # `run_segment` is the engine-driven workflow callback: when
                # a workflow tool fires, the ENGINE owns its loop (calling
                # this per branch-delimited segment), instead of the model
                # re-calling the tool to advance one step at a time.
                tool_context = {
                    "last_assistant_message": last_assistant_text(convo.messages),
                    "last_user_message": last_user_text(convo.messages),
                    "session_id": convo.session_id,
                    "run_segment": self._make_segment_runner(),
                }
                # Run all tool calls concurrently (react mode yields exactly
                # one, native may yield several).
                results = await asyncio.gather(*[
                    self.tools.run(tc.name, tc.arguments, tool_context)
                    for tc in tool_calls
                ])
                # If this turn's call fired a workflow tool, retag its tokens
                # as `trigger` (the workflow engine's own segment calls are
                # already tagged `segment`).
                if fired_workflow_tool(self.tools, tool_calls):
                    self.provider.reclassify_call(conv_call, "trigger")
                convo.messages.append(self._result_message(tool_calls, results))

                # If the model asked the user a question via human_feedback,
                # pause the loop: surface the question as the reply and hand
                # control back to the user. Their next message resumes.
                # (An engine-driven workflow surfaces its own pauses through
                # the workflow tool's RESULT — the model relays it and the
                # turn ends naturally on the next pass.)
                paused = human_feedback_pause(tool_calls, results)
                if paused is not None:
                    return paused, convo.session_id

            return "[agent stopped: hit max_steps]", convo.session_id

    # -- streaming chat -----------------------------------------------------

    async def chat_stream(self, user_input: str, session_id: str | None = None,
                          system: str | None = None) -> AsyncIterator[StreamEvent]:
        """Async generator yielding StreamEvents through the full agent loop.

        Tool calls and tool results are surfaced as discrete events so a UI
        can show 'calling tool X...' between text deltas.
        """
        if not self._tools_built:
            await self.start()

        convo = self.store.get_or_create(session_id, system=system)
        sid = convo.session_id

        async with self._turn(convo):
            turn_start = len(convo.messages)
            verify_state: dict = {}
            convo.messages.append(Message(
                role="user",
                blocks=[{"type": "text", "text": user_input}],
            ))

            try:
                # Deterministic workflow entry, before any model decision:
                # resume a paused workflow (the message IS its answer), or
                # trigger one the user explicitly asked to run by name —
                # streaming its own segment events. The next provider call
                # below sees the result and relays/acts on it.
                resume_events: asyncio.Queue = asyncio.Queue()

                async def _resume_sink(kind: str, payload):
                    await resume_events.put((kind, payload))

                resume_task = asyncio.ensure_future(
                    self._auto_workflow_call(convo, user_input,
                                             event_sink=_resume_sink)
                )
                while not resume_task.done() or not resume_events.empty():
                    drainer = asyncio.ensure_future(resume_events.get())
                    done, _ = await asyncio.wait(
                        {resume_task, drainer}, return_when=asyncio.FIRST_COMPLETED,
                    )
                    if drainer in done:
                        kind, payload = drainer.result()
                        for ev in segment_stream_events(kind, payload, sid):
                            yield ev
                    else:
                        drainer.cancel()
                resumed = resume_task.result()
                if resumed is not None:
                    tc, out, err = resumed
                    yield StreamEvent(type="tool_call", tool_call=tc, session_id=sid)
                    yield StreamEvent(type="tool_result", tool_call_id=tc.id,
                                      text=out, is_error=err, session_id=sid)

                final_text = ""
                hit_step_limit = True
                for _ in range(self.max_steps):
                    final_resp: LLMResponse | None = None
                    self.provider.usage_purpose = "conversational"
                    async for kind, payload in self.provider.stream(
                        system=self._system_for_call(convo.system),
                        messages=convo.messages,
                        tools=self._exposed_tools(),
                        hosted_mcp=self.hosted_mcp,
                        skills=self.skills, max_tokens=self.max_tokens,
                    ):
                        if kind == "text_delta":
                            yield StreamEvent(type="text_delta", text=payload,
                                              session_id=sid)
                        elif kind == "final":
                            final_resp = payload
                    assert final_resp is not None, "provider didn't yield 'final'"
                    conv_call = self.provider.last_call_usage()

                    text, tool_calls, terminal = self._interpret(final_resp)

                    # Persist the assistant turn.
                    assistant_blocks: list[dict] = []
                    if text:
                        assistant_blocks.append({"type": "text", "text": text})
                    for tc in tool_calls:
                        assistant_blocks.append({
                            "type": "tool_call",
                            "id": tc.id, "name": tc.name,
                            "arguments": tc.arguments,
                            # Carried for providers that must replay it (Gemini).
                            "thought_signature": getattr(
                                tc, "thought_signature", None),
                        })
                    convo.messages.append(Message(role="assistant",
                                                  blocks=assistant_blocks))

                    # Surface tool-call decisions before running them. In
                    # react mode these are parsed from the text the UI
                    # already streamed, so the event is what lets a UI show
                    # 'calling X' rather than re-rendering the raw Action.
                    for tc in tool_calls:
                        yield StreamEvent(type="tool_call", tool_call=tc,
                                          session_id=sid)

                    yield StreamEvent(type="turn_end", session_id=sid)

                    if terminal:
                        # Verification gate: a code-changing turn must show
                        # a real passing test run before "done" is accepted.
                        nudge = self._verify_nudge(convo.messages, turn_start,
                                                   verify_state)
                        if nudge is not None:
                            convo.messages.append(Message(
                                role="user",
                                blocks=[{"type": "text", "text": nudge}],
                            ))
                            continue
                        final_text = text
                        hit_step_limit = False
                        break

                    # Build tool-invocation context once per turn. An
                    # engine-driven workflow tool runs the engine loop inside
                    # its handler; its segment events flow back through this
                    # queue so the UI stays live during the workflow.
                    segment_events: asyncio.Queue = asyncio.Queue()

                    async def _segment_sink(kind: str, payload):
                        await segment_events.put((kind, payload))

                    tool_context = {
                        "last_assistant_message":
                            last_assistant_text(convo.messages),
                        "last_user_message":
                            last_user_text(convo.messages),
                        "session_id": sid,
                        "run_segment":
                            self._make_segment_runner(event_sink=_segment_sink),
                        # Same sink the segment runner uses, so the engine's OWN
                        # deterministic execs (per-item pricer) surface as
                        # tool_call/tool_result events too — otherwise they run
                        # silently and Tool Correctness scores 0.
                        "event_sink": _segment_sink,
                    }

                    # Execute tools concurrently; surface each as it lands.
                    async def _run(tc: ToolCall):
                        out, err = await self.tools.run(
                            tc.name, tc.arguments, tool_context,
                        )
                        return tc, out, err

                    tasks = [asyncio.create_task(_run(tc))
                             for tc in tool_calls]
                    results: list[tuple[ToolCall, str, bool]] = []
                    pending = set(tasks)
                    drainer = asyncio.ensure_future(segment_events.get())
                    while pending or not segment_events.empty() or drainer is not None:
                        if drainer is None:
                            drainer = asyncio.ensure_future(segment_events.get())
                        done, _ = await asyncio.wait(
                            pending | {drainer},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if drainer in done:
                            kind, payload = drainer.result()
                            drainer = None
                            for ev in segment_stream_events(kind, payload, sid):
                                yield ev
                            continue
                        for t in (done & pending):
                            pending.discard(t)
                            tc, out, err = t.result()
                            results.append((tc, out, err))
                            yield StreamEvent(type="tool_result",
                                              tool_call_id=tc.id, text=out,
                                              is_error=err, session_id=sid)
                        if not pending:
                            # Tools done; drain any remaining buffered events
                            # then stop waiting on the queue.
                            if drainer is not None:
                                drainer.cancel()
                                drainer = None
                            while not segment_events.empty():
                                kind, payload = segment_events.get_nowait()
                                for ev in segment_stream_events(kind, payload, sid):
                                    yield ev

                    # Retag this turn's conversational call as `trigger` if
                    # it fired a workflow tool (§7 token accounting).
                    if fired_workflow_tool(self.tools, tool_calls):
                        self.provider.reclassify_call(conv_call, "trigger")

                    # Re-pair results to calls in original order, then hand
                    # to _result_message (structured blocks for native, a
                    # single Observation: text block for react).
                    by_id = {tc.id: (out, err) for tc, out, err in results}
                    ordered = [by_id[tc.id] for tc in tool_calls]
                    convo.messages.append(
                        self._result_message(tool_calls, ordered))

                    # human_feedback pauses the loop: surface its question
                    # as the final reply and hand control back to the user
                    # (their next message resumes the run).
                    paused = human_feedback_pause(tool_calls, ordered)
                    if paused is not None:
                        final_text = paused
                        hit_step_limit = False
                        break

                if hit_step_limit:
                    final_text = "[agent stopped: hit max_steps]"

                yield StreamEvent(type="done", text=final_text, session_id=sid)

            except Exception as e:
                yield StreamEvent(type="error",
                                  text=f"{type(e).__name__}: {e}",
                                  session_id=sid)
