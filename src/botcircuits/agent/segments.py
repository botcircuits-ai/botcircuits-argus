"""Engine-driven workflow segment execution.

The workflow engine (`agent/workflow/engine/runner.py`) owns advancement
through a workflow; when a segment needs intelligence it calls back into
the agent via `run_segment`. This module is that callback's implementation:
one branch-delimited segment = a constant-size cached system prompt + the
segment payload, looped over provider calls until the model stops asking
for tools (or pauses for the user).

`SegmentRunner` is mixed into `Agent` (see `agent/loop.py`) so the methods
share the agent's provider, tool registry, and mode strategy without the
loop module carrying the segment machinery.
"""

from __future__ import annotations

from botcircuits.providers.base import LLMProvider
from botcircuits.types import Message, ToolCall
from botcircuits.agent.events import human_feedback_pause
from botcircuits.agent.tools import ToolRegistry
from botcircuits.agent.tools.builtins.human_feedback import HUMAN_FEEDBACK_TOOL
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
MAX_SEGMENT_TURNS = 25

# Provider short-names `make_provider` can build an in-process client for.
# A workflow agent binding whose `provider` isn't one of these (e.g. it only
# pins a CLI `runtime` like claude-code) falls back to the run's default
# provider under the native runtime — native never spawns an external CLI.
_IN_PROCESS_PROVIDERS = frozenset({"anthropic", "openai", "gemini", "openrouter"})


def fired_workflow_tool(reg: ToolRegistry, tool_calls: list[ToolCall]) -> bool:
    """True when any of this turn's tool calls invoked a workflow tool —
    used to retag that turn's conversational provider call as `trigger`
    in the per-purpose usage breakdown (§7)."""
    names = {tc.name for tc in tool_calls}
    for tool in reg.all():
        if getattr(tool, "_workflow_state", None) is not None and tool.name in names:
            return True
    return False


class SegmentRunner:
    """Segment-execution half of the `Agent` (mixin).

    Relies on the host class providing: `provider`, `tools`, `hosted_mcp`,
    `skills`, `max_tokens`, `mode`, `enable_workflows`, `_agents_config`,
    `_provider_cache`, `_interpret`, and `_result_message`.
    """

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
        provider: LLMProvider | None = None,
        event_sink=None,
        workflow_bg=None,
    ) -> SegmentResult:
        """Run ONE branch-delimited segment: a constant-size cached system
        prompt + the segment payload, looped over provider calls until the
        model stops asking for tools (or pauses for the user).

        `provider`, when given, overrides `self.provider` for JUST this
        segment's calls — the per-agent model resolved for a step pinned to
        a different agent than the run's default. `None` (the common case)
        uses `self.provider` as before.

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
        user_msg = build_segment_user_message(
            actions, branch_variables, system_notes,
            item_variables=item_variables,
            data_variables=data_variables,
            slots=slots,
        )
        messages: list[Message] = [
            Message(role="user", blocks=[{"type": "text", "text": user_msg}]),
        ]

        active_provider = provider or self.provider

        final_text = ""
        for _ in range(MAX_SEGMENT_TURNS):
            active_provider.usage_purpose = "segment"
            resp = await active_provider.complete(
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
                    paused_question = human_feedback_pause([tc], [(out, err)])
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
                          item_variables=None, data_variables=None, agent=None):
            # A segment pinned to a named agent gets that agent's in-process
            # provider/model when the binding resolves to one we can build;
            # otherwise `provider` is None and `_run_segment` uses the default.
            # Accepting `agent` here (the engine passes it only for pinned
            # segments) is what lets the native runtime honor a workflow's
            # per-agent `model` — without it the engine call would TypeError.
            return await self._run_segment(
                actions=actions,
                branch_variables=branch_variables,
                system_notes=system_notes,
                slots=slots,
                item_variables=item_variables,
                data_variables=data_variables,
                provider=self._resolve_segment_provider(agent),
                event_sink=event_sink,
                workflow_bg=workflow_bg,
            )
        return _runner

    def _resolve_segment_provider(self, agent: str | None) -> LLMProvider | None:
        """The in-process `LLMProvider` a segment pinned to `agent` should use,
        or None to fall back to `self.provider`.

        Per the native routing contract, we only override when the agent's
        binding names something we can actually build in-process: a `provider`
        key (e.g. `{"provider": "openai", "model": "gpt-4.1"}`). Bindings that
        only pin a CLI `runtime` (e.g. claude-code) — or whose model is an
        alias `make_provider` can't turn into a client — fall through to the
        default provider rather than crashing, since native never spawns the
        external CLI itself. Cached by (provider, model).
        """
        cfg = self._agents_config.get(agent) if agent else None
        if not isinstance(cfg, dict) or not cfg:
            return None
        kind = cfg.get("provider")
        # No explicit in-process provider (e.g. only a `runtime`/CLI model
        # alias) — keep the run's default provider.
        if not kind:
            return None
        # `make_provider` maps any unknown name to Anthropic rather than
        # raising, so whitelist the kinds we can actually build in-process;
        # an unrecognized binding falls back to the default provider instead
        # of silently switching vendors.
        if kind not in _IN_PROCESS_PROVIDERS:
            print(f"[native] agent '{agent}' provider '{kind}' is not an "
                  f"in-process provider; using default provider.")
            return None
        model = cfg.get("model")
        key = (kind, model)
        cached = self._provider_cache.get(key)
        if cached is None:
            from botcircuits.providers import make_provider
            try:
                cached = make_provider(kind, model)
            except Exception as e:  # unbuildable (missing key, bad model, …)
                print(f"[native] agent '{agent}' provider "
                      f"'{kind}' unavailable ({type(e).__name__}: {e}); "
                      f"using default provider.")
                return None
            self._provider_cache[key] = cached
        return cached

    @staticmethod
    async def tools_run_synthetic(tool, tc: ToolCall) -> tuple[str, bool]:
        """Run a synthetic (engine-only) tool not registered on the agent's
        registry — currently just `record_slots` / `record_item_list`."""
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
