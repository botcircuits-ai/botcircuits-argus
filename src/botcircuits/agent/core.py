"""Agent — the multi-round tool-use loop.

Coordinates a single `LLMProvider`, a `ToolRegistry`, optional MCP
servers, and optional skills. Owns the `ConversationStore` so callers
can resume sessions across calls.

Use as an async context manager:

    async with Agent(provider=...) as agent:
        reply, sid = await agent.chat("hello")
        async for ev in agent.chat_stream("..."):
            ...
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import AsyncIterator, Literal
from uuid import uuid4

from botcircuits.providers.base import LLMProvider
from botcircuits.types import LLMResponse, Message, StreamEvent, ToolCall
from botcircuits.agent.mcp import LocalMCPManager, MCPServer
from botcircuits.agent.react import (
    format_observation,
    parse_react_step,
    render_react_preamble,
)
from botcircuits.agent.skill import (
    DEFAULT_SKILL_ROOTS,
    SkillSpec,
    discover_skills,
    skill_to_tool,
)
from botcircuits.agent.store import ConversationStore
from botcircuits.agent.tools import ToolRegistry
from botcircuits.agent.tools.builtins.human_feedback import HUMAN_FEEDBACK_TOOL
from botcircuits.agent.workflow import active_workflow_names
from botcircuits.agent.workflow.engine.runner import SegmentResult
from botcircuits.agent.workflow.engine.segment_exec import (
    ENGINE_SYSTEM_PROMPT,
    RECORD_ITEM_LIST_TOOL,
    RECORD_SLOTS_TOOL,
    build_record_item_list_tool,
    build_record_slots_tool,
    build_segment_user_message,
)

#: Inner-loop bound on provider round-trips within a single segment. A
#: segment's actions may need several tool round-trips (e.g. read a file,
#: then write one); this caps runaway loops without an LLM driving them.
_MAX_SEGMENT_TURNS = 25

MAX_AGENT_STEPS = 500

# Synthetic id prefix for the workflow tool call the loop injects to resume
# a paused workflow on the user's next message. Lets us tell loop-injected
# calls apart from model-issued ones in history if needed.
_AUTO_RESUME_ID_PREFIX = "wf-autoresume-"

# Truncation cap on the last-assistant-message we hand to tools via context.
# Variable normalization (the workflow tool's main consumer of this field)
# only needs the most recent prose-y reply, not the model's entire monologue.
_CONTEXT_LAST_ASSISTANT_CHARS = 2000


def _last_assistant_text(messages: list[Message]) -> str:
    """Pull the most recent assistant `text` block out of `messages` and
    truncate it. Returns "" when no assistant text exists yet (e.g., the
    workflow tool is called on the very first turn before the model has
    said anything beyond a tool call).
    """
    for m in reversed(messages):
        if m.role != "assistant":
            continue
        for b in m.blocks:
            if b.get("type") == "text" and b.get("text"):
                text = b["text"]
                if len(text) > _CONTEXT_LAST_ASSISTANT_CHARS:
                    return text[:_CONTEXT_LAST_ASSISTANT_CHARS] + "…"
                return text
    return ""


def _last_user_text(messages: list[Message]) -> str:
    """Pull the most recent user `text` block out of `messages` and
    truncate it. Tool-result blocks (which also live on user-role messages)
    are skipped — we want the human's actual utterance, not tool output.
    Returns "" when no user text exists yet.
    """
    for m in reversed(messages):
        if m.role != "user":
            continue
        for b in m.blocks:
            if b.get("type") == "text" and b.get("text"):
                text = b["text"]
                if len(text) > _CONTEXT_LAST_ASSISTANT_CHARS:
                    return text[:_CONTEXT_LAST_ASSISTANT_CHARS] + "…"
                return text
    return ""


def _fired_workflow_tool(reg: ToolRegistry, tool_calls: list[ToolCall]) -> bool:
    """True when any of this turn's tool calls invoked a workflow tool —
    used to retag that turn's conversational provider call as `trigger`
    in the per-purpose usage breakdown (§7)."""
    names = {tc.name for tc in tool_calls}
    for tool in reg.all():
        if getattr(tool, "_workflow_state", None) is not None and tool.name in names:
            return True
    return False


def _segment_stream_events(kind: str, payload, sid: str):
    """Map an engine-segment sink event to StreamEvents for the UI.

    The segment sink (passed to `Agent._run_segment`) emits `("text", str)`,
    `("tool_call", ToolCall)`, and `("tool_result", (ToolCall, out, err))`;
    we translate those to the same StreamEvent shapes the main loop yields,
    so a workflow's internal segment calls look live to the UI.
    """
    if kind == "text":
        yield StreamEvent(type="text_delta", text=payload, session_id=sid)
    elif kind == "tool_call":
        yield StreamEvent(type="tool_call", tool_call=payload, session_id=sid)
    elif kind == "tool_result":
        tc, out, err = payload
        yield StreamEvent(type="tool_result", tool_call_id=tc.id,
                          text=out, is_error=err, session_id=sid)


def _human_feedback_pause(
    tool_calls: list[ToolCall],
    results: list[tuple[str, bool]],
) -> str | None:
    """If a `human_feedback` call ran this round, return the question to
    surface to the user (so the loop can pause); else None.

    `human_feedback`'s handler returns `{"paused": true, "question": ...}`,
    JSON-encoded into the result text. We match by tool name and pull the
    question back out of that payload, falling back to the model's own
    `question` argument, then the raw result text.
    """
    for tc, (output, _is_error) in zip(tool_calls, results):
        if tc.name != HUMAN_FEEDBACK_TOOL:
            continue
        question = ""
        try:
            payload = json.loads(output)
            if isinstance(payload, dict):
                question = payload.get("question") or ""
        except (ValueError, TypeError):
            question = ""
        if not question and isinstance(tc.arguments, dict):
            question = tc.arguments.get("question") or ""
        return question or output
    return None


class Agent:
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
    ):
        self.provider = provider
        self.user_tools = tools or ToolRegistry()
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
        self._tools_built = True

    async def aclose(self) -> None:
        # Terminate any background shell processes the model started.
        # Imported lazily so this module doesn't pull tools at import time
        # (tools depend on registry, which lives next door).
        from .tools.builtins import _bg
        await _bg.terminate_all()
        await self._local_mcp.stop()
        await self.provider.aclose()

    # -- workflow gating ---------------------------------------------------

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

    # -- engine-driven workflow segment execution --------------------------

    def _engine_tools(self, record_slots, record_item_list=None) -> list:
        """Tools exposed to a segment call: the agent's real tools (built-ins,
        MCP, skills) MINUS workflow tools — the engine owns advancement now,
        so the model must not re-enter a workflow tool — PLUS the synthetic
        capture tool(s): `record_slots` when the segment branches, or
        `record_item_list` for a listDecision segment (S3)."""
        base = [t for t in self.tools.all()
                if getattr(t, "_workflow_state", None) is None]
        if record_slots is not None:
            base.append(record_slots)
        if record_item_list is not None:
            base.append(record_item_list)
        return base

    async def _run_segment(
        self,
        *,
        actions: list[str],
        branch_variables: list[dict],
        system_notes: list[str],
        slots: dict,
        item_variables: list[dict] | None = None,
        data_variables: list[dict] | None = None,
        event_sink=None,
        workflow_bg=None,
    ) -> SegmentResult:
        """Run ONE branch-delimited segment: a constant-size cached system
        prompt + the segment payload, looped over provider calls until the
        model stops asking for tools (or pauses for the user).

        Returns the captured branch slots (via the synthetic `record_slots`
        tool, Tier 1), the final assistant text, and a pause flag when the
        model called `human_feedback`. The engine runner (`run_workflow_engine`)
        consumes this to evaluate the branch and advance.

        `event_sink`, when provided, is an async callable the streaming path
        passes so segment `text_delta`/`tool_call`/`tool_result` events still
        reach the UI. When None, this runs non-streaming.
        """
        captured: dict = {}
        # `record_slots` advertises branch variables AND carried data variables,
        # so the native path can capture a "scrape" step's data payload into
        # slots for a later "save" step — the same key-value memory the CLI
        # runtime carries via its JSON contract.
        record_slots_vars = list(branch_variables) + list(data_variables or [])
        record_slots = (
            build_record_slots_tool(record_slots_vars, captured)
            if record_slots_vars else None
        )
        # S3 — for a listDecision segment, expose the list-capture tool instead
        # of (or alongside) record_slots. The model reports a list of per-item
        # fact-sets; the engine decides each deterministically.
        item_sink: dict = {}
        record_item_list = (
            build_record_item_list_tool(item_variables, item_sink)
            if item_variables else None
        )
        engine_tools = self._engine_tools(record_slots, record_item_list)
        # A throwaway registry so `record_slots` is runnable without
        # polluting the agent's real registry. Real tools still execute via
        # self.tools; record_slots is intercepted below.
        user_msg = build_segment_user_message(
            actions, branch_variables, system_notes,
            item_variables=item_variables,
            data_variables=data_variables,
            slots=slots,
        )
        messages: list[Message] = [
            Message(role="user", blocks=[{"type": "text", "text": user_msg}]),
        ]

        final_text = ""
        for _ in range(_MAX_SEGMENT_TURNS):
            self.provider.usage_purpose = "segment"
            resp = await self.provider.complete(
                system=ENGINE_SYSTEM_PROMPT,
                messages=messages,
                tools=engine_tools if self.mode != "react" else [],
                hosted_mcp=self.hosted_mcp, skills=self.skills,
                max_tokens=self.max_tokens,
            )
            text, tool_calls, terminal = self._interpret(resp)
            if text:
                final_text = text

            assistant_blocks: list[dict] = []
            if text:
                assistant_blocks.append({"type": "text", "text": text})
            for tc in tool_calls:
                assistant_blocks.append({
                    "type": "tool_call", "id": tc.id, "name": tc.name,
                    "arguments": tc.arguments,
                    "thought_signature": getattr(tc, "thought_signature", None),
                })
            messages.append(Message(role="assistant", blocks=assistant_blocks))

            if event_sink is not None and text:
                await event_sink("text", text)
            for tc in tool_calls:
                if event_sink is not None:
                    await event_sink("tool_call", tc)

            if terminal:
                break

            # Execute tool calls. `record_slots` is intercepted (its handler
            # writes into `captured`); `human_feedback` pauses the segment;
            # everything else runs on the agent's real registry.
            results: list[tuple[str, bool]] = []
            paused_question: str | None = None
            recorded_slots = False
            recorded_items = False
            for tc in tool_calls:
                if tc.name == RECORD_SLOTS_TOOL and record_slots is not None:
                    out, err = await self.tools_run_synthetic(record_slots, tc)
                    recorded_slots = True
                elif tc.name == RECORD_ITEM_LIST_TOOL and record_item_list is not None:
                    out, err = await self.tools_run_synthetic(record_item_list, tc)
                    recorded_items = True
                elif tc.name == HUMAN_FEEDBACK_TOOL:
                    hf_ctx = {"_workflow_bg": workflow_bg} if workflow_bg is not None else None
                    out, err = await self.tools.run(tc.name, tc.arguments, hf_ctx)
                    paused_question = _human_feedback_pause([tc], [(out, err)])
                else:
                    tool_ctx: dict = {"session_id": None}
                    if workflow_bg is not None:
                        tool_ctx["_workflow_bg"] = workflow_bg
                    out, err = await self.tools.run(tc.name, tc.arguments, tool_ctx)
                results.append((out, err))
                if event_sink is not None:
                    await event_sink("tool_result", (tc, out, err))

            messages.append(self._result_message(tool_calls, results))

            if paused_question is not None:
                return SegmentResult(
                    text=final_text, captured_slots=dict(captured),
                    captured_items=list(item_sink.get("items", [])),
                    paused=True, question=paused_question,
                )

            # `record_slots` / `record_item_list` is the segment's terminal
            # signal: once the model reports the branch values (or the per-item
            # fact list), the engine has what it needs to decide, so we stop
            # spending provider round-trips on this segment. (Non-branching
            # segments have neither tool and terminate naturally when the model
            # stops calling tools.)
            if recorded_slots or recorded_items:
                break

        return SegmentResult(
            text=final_text, captured_slots=dict(captured),
            captured_items=list(item_sink.get("items", [])),
        )

    def _make_segment_runner(self, event_sink=None, workflow_bg=None):
        """A `run_segment` callable for the tool context, bound to this
        agent. Engine-disabled agents return None so the workflow tool falls
        back to its legacy per-step path.

        `event_sink`, when given (streaming path), forwards segment events
        so the UI stays live during an engine-driven workflow.

        `workflow_bg`, when given, is a `WorkflowTask` whose channel the
        `human_feedback` tool uses to block the background coroutine and
        await the user's reply on the main thread instead of returning a
        `paused` signal up the stack.
        """
        if not self.enable_workflows:
            return None

        async def _runner(*, actions, branch_variables, system_notes, slots,
                          item_variables=None, data_variables=None):
            return await self._run_segment(
                actions=actions,
                branch_variables=branch_variables,
                system_notes=system_notes,
                slots=slots,
                item_variables=item_variables,
                data_variables=data_variables,
                event_sink=event_sink,
                workflow_bg=workflow_bg,
            )
        return _runner

    @staticmethod
    async def tools_run_synthetic(tool, tc: ToolCall) -> tuple[str, bool]:
        """Run a synthetic (engine-only) tool not registered on the agent's
        registry — currently just `record_slots`."""
        import inspect as _inspect
        import json as _json
        try:
            res = tool.handler(tc.arguments or {})
            if _inspect.isawaitable(res):
                res = await res
            text = res if isinstance(res, str) else _json.dumps(res, default=str)
            return text, False
        except Exception as e:  # pragma: no cover - defensive
            return f"{type(e).__name__}: {e}", True

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
        from botcircuits.agent.workflow import active_workflow_names

        if not self.enable_workflows:
            return None
        names = active_workflow_names(self.tools)
        if not names:
            return None
        name = names[0]

        tc = ToolCall(id=f"{_AUTO_RESUME_ID_PREFIX}{uuid4().hex[:8]}",
                      name=name, arguments={})
        tool_context = {
            "last_assistant_message": _last_assistant_text(convo.messages),
            "last_user_message": _last_user_text(convo.messages),
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

    # -- non-streaming chat -------------------------------------------------

    async def chat(self, user_input: str, session_id: str | None = None,
                   system: str | None = None) -> tuple[str, str]:
        """Send a user message, run the loop until the model stops asking
        for tools, return (assistant_text, session_id)."""
        if not self._tools_built:
            await self.start()

        convo = self.store.get_or_create(session_id, system=system)
        async with convo.lock:
            convo.messages.append(Message(
                role="user",
                blocks=[{"type": "text", "text": user_input}],
            ))

            # A paused workflow's resume doesn't need a model decision — the
            # user's message we just appended IS the answer. Resume it
            # directly so the next provider call (below) sees the result and
            # relays/acts on it, instead of asking the model to notice the
            # pause and re-call the tool itself.
            await self._resume_active_workflow(convo)

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
                    return text, convo.session_id

                # Build tool-invocation context once per turn. The same
                # snapshot is handed to every tool call in this round.
                # `run_segment` is the engine-driven workflow callback: when
                # a workflow tool fires, the ENGINE owns its loop (calling
                # this per branch-delimited segment), instead of the model
                # re-calling the tool to advance one step at a time.
                tool_context = {
                    "last_assistant_message": _last_assistant_text(convo.messages),
                    "last_user_message": _last_user_text(convo.messages),
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
                if _fired_workflow_tool(self.tools, tool_calls):
                    self.provider.reclassify_call(conv_call, "trigger")
                convo.messages.append(self._result_message(tool_calls, results))

                # If the model asked the user a question via human_feedback,
                # pause the loop: surface the question as the reply and hand
                # control back to the user. Their next message resumes.
                # (An engine-driven workflow surfaces its own pauses through
                # the workflow tool's RESULT — the model relays it and the
                # turn ends naturally on the next pass.)
                paused = _human_feedback_pause(tool_calls, results)
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

        async with convo.lock:
            convo.messages.append(Message(
                role="user",
                blocks=[{"type": "text", "text": user_input}],
            ))

            try:
                # A paused workflow's resume doesn't need a model decision —
                # the user's message we just appended IS the answer. Resume
                # it directly (streaming its own segment events) so the next
                # provider call below sees the result and relays/acts on it,
                # instead of asking the model to notice the pause and re-call
                # the tool itself.
                resume_events: asyncio.Queue = asyncio.Queue()

                async def _resume_sink(kind: str, payload):
                    await resume_events.put((kind, payload))

                resume_task = asyncio.ensure_future(
                    self._resume_active_workflow(convo, event_sink=_resume_sink)
                )
                while not resume_task.done() or not resume_events.empty():
                    drainer = asyncio.ensure_future(resume_events.get())
                    done, _ = await asyncio.wait(
                        {resume_task, drainer}, return_when=asyncio.FIRST_COMPLETED,
                    )
                    if drainer in done:
                        kind, payload = drainer.result()
                        for ev in _segment_stream_events(kind, payload, sid):
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
                            _last_assistant_text(convo.messages),
                        "last_user_message":
                            _last_user_text(convo.messages),
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
                            for ev in _segment_stream_events(kind, payload, sid):
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
                                for ev in _segment_stream_events(kind, payload, sid):
                                    yield ev

                    # Retag this turn's conversational call as `trigger` if
                    # it fired a workflow tool (§7 token accounting).
                    if _fired_workflow_tool(self.tools, tool_calls):
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
                    paused = _human_feedback_pause(tool_calls, ordered)
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
