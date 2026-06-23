"""Agent-driven evaluation runners.

Both modes drive a REAL `Agent` (the same code path production runs)
through one EvalCase's scripted user turns and capture the final
assistant reply for scoring. The two modes differ only in whether the
workflow tool is registered and exposed to the model:

  - `run_case_agent_with_workflow` — registers the workflow on the
    agent's registry, leaves `Agent.enable_workflows=True` so the
    workflow tool is visible. The agent decides when to call it; the
    engine handles branching and slot collection through the same
    LLM the agent is using.

  - `run_case_agent_no_workflow` — does NOT register the workflow.
    Sets `Agent.enable_workflows=False` (defensive: even if any
    workflow tools snuck into the registry, they'd be hidden). The
    dataset's `workflow_spec` is appended to the agent's system
    prompt as plain natural-language instructions; the agent has to
    follow the procedure from prose alone, using only its other
    tools (write_file, shell, etc.) to actually do work.

This is the apples-to-apples comparison for the original hypothesis:
same model, same other tools, same turn-by-turn user replies — only
difference is whether the workflow is executable or prompted.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from botcircuits.agent import Agent, default_registry, workflow_tool
from botcircuits.providers.base import LLMProvider
from botcircuits.agent.workflow import local as wf_local
from botcircuits.agent.workflow.evaluation.dataset import EvalCase


class _NonClosingProvider:
    """Thin wrapper that proxies an underlying provider but swallows
    `aclose()`. The harness shares one provider across many agent runs;
    `Agent.aclose()` unconditionally closes its provider, which would
    drop the shared HTTP client and break the next run.

    The wrapper attribute-passes everything the provider exposes so
    `Agent` and the various tools can read `.name`, `.model`,
    `.supports_hosted_mcp()`, etc. transparently.
    """

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        # Reached only for attributes we didn't override. Forwards to
        # the underlying provider so anything Agent reads (model,
        # name, complete, stream, supports_hosted_mcp) Just Works.
        return getattr(self._inner, name)

    async def aclose(self) -> None:
        return None


@dataclass
class AgentRunResult:
    case_id: str
    workflow: str
    mode: str                    # "workflow_on" | "workflow_off"
    final_text: str = ""
    transcript: list[dict] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    workflow_invocations: int = 0
    error: str | None = None
    elapsed_s: float = 0.0
    # Real token usage for THIS run, captured as a delta around the agent
    # drive. `usage_by_purpose` breaks the input/output tokens down by call
    # intent (trigger | segment | tier2_normalization | conversational) so
    # the §7 comparison can show where engine mode spends — and saves —
    # tokens vs. the prompt-driven baseline.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    llm_calls: int = 0
    usage_by_purpose: dict = field(default_factory=dict)


_BASELINE_SYSTEM_PREAMBLE = """\
You are a chat agent driving a procedure end-to-end. The procedure is
described below as a sequence of steps with branching conditions. You
must:

  - Follow the steps in order, asking the user one question at a time
    when a step needs input.
  - Evaluate branching conditions yourself against the values the user
    has provided so far. Do NOT skip steps or invent new ones.
  - For steps that compute a value (e.g. ratios, totals), do the
    arithmetic yourself before evaluating any condition that depends
    on it.
  - When you reach a terminal step, produce a clear final message to
    the user that contains the words requested by that step's action
    text (e.g. APPROVED, DENIED, MANUAL REVIEW, cancelled).

Do not ask the user for permission to begin; just start with the
procedure's first step.

The procedure:
"""


def _build_baseline_system(workflow_spec: str,
                           caller_system: str | None) -> str:
    """Compose the system prompt for the no-workflow baseline. The
    caller's existing system prompt (if any) comes first so things
    like 'always cite sources' still apply; the
    procedure block is appended underneath."""
    parts: list[str] = []
    if caller_system:
        parts.append(caller_system)
    parts.append(_BASELINE_SYSTEM_PREAMBLE)
    parts.append(workflow_spec)
    return "\n\n".join(parts)


def _user_messages(case: EvalCase) -> list[str]:
    """Concatenate `initial_user_text` + each turn's `user_text` into
    the list of messages the agent should receive, one per chat turn.

    Empty messages are dropped: they were a workflow-engine artifact
    (the engine needed an extra empty re-entry to advance past
    non-branching states) that doesn't apply when a real agent is
    driving — feeding an empty user turn into Agent.chat() would just
    be a no-op cost.
    """
    msgs: list[str] = []
    if case.initial_user_text:
        msgs.append(case.initial_user_text)
    for t in case.turns:
        if t.user_text:
            msgs.append(t.user_text)
    return msgs


async def _drive_agent(
    *,
    case: EvalCase,
    provider: LLMProvider,
    enable_workflows: bool,
    system: str,
    register_workflow_record: dict | None,
    mode_label: str,
) -> AgentRunResult:
    """Build an Agent, feed it the scripted user messages one per
    chat turn, capture the trail. Shared body for both modes.

    `register_workflow_record` is a fully-built workflow record (the
    dict produced by `fetch_workflows()`) that gets wrapped into a
    workflow tool and added to the registry before the agent starts.
    Pass None to skip — that's the no-workflow mode.
    """
    started = time.perf_counter()
    out = AgentRunResult(
        case_id=case.id, workflow=case.workflow, mode=mode_label,
    )
    usage_before = _usage_snapshot(provider)
    try:
        # default_registry brings the built-in tools (read_file,
        # write_file, shell, etc.). The agent in production uses
        # exactly this set; we keep parity here so the baseline mode
        # has the same surface to work with.
        registry = default_registry({}, provider=provider)

        if register_workflow_record is not None:
            wf_tool = workflow_tool(
                register_workflow_record, provider=provider,
            )
            registry.register(wf_tool)

        agent = Agent(
            # Wrap the provider so Agent.aclose() doesn't tear down
            # the shared HTTP client. Subsequent agent runs in the
            # same harness invocation reuse the original provider.
            provider=_NonClosingProvider(provider),
            tools=registry,
            enable_workflows=enable_workflows,
            # Bigger step budget than the default — long workflows
            # (50+ states) need many tool-call rounds. Tunable.
            max_steps=40,
        )

        async with agent:
            session_id: str | None = None
            final_text = ""
            for msg in _user_messages(case):
                final_text, session_id = await agent.chat(
                    msg, session_id=session_id, system=system,
                )
                out.transcript.append({"user": msg, "assistant": final_text})
            out.final_text = final_text

            # Roll up tool-call summary from the persisted message
            # history so the report can show what the agent actually
            # did (and so workflow-mode runs can be sanity-checked
            # against `workflow_invocations > 0`). ConversationStore
            # has no public `get` method; we go through the internal
            # dict because the alternative (storing the convo handle
            # from chat()'s return) would require a richer return
            # contract on the public API just for this telemetry.
            if session_id:
                convo = agent.store._sessions.get(session_id)
                if convo is not None:
                    for m in convo.messages:
                        for b in m.blocks:
                            if b.get("type") == "tool_call":
                                name = b.get("name") or ""
                                out.tool_calls.append(name)
                                if name == case.workflow:
                                    out.workflow_invocations += 1
    except Exception as e:
        out.error = f"{type(e).__name__}: {e}"

    _apply_usage_delta(out, provider, usage_before)
    out.elapsed_s = time.perf_counter() - started
    return out


def _usage_snapshot(provider: LLMProvider) -> dict:
    """Read the provider's cumulative usage so a per-run delta can be
    computed (the harness shares one provider across runs)."""
    by_purpose = getattr(provider, "usage_by_purpose", {}) or {}
    return {
        "input": getattr(provider, "usage_input_tokens", 0),
        "output": getattr(provider, "usage_output_tokens", 0),
        "cache_read": getattr(provider, "usage_cache_read_tokens", 0),
        "calls": getattr(provider, "usage_llm_calls", 0),
        "by_purpose": {
            k: dict(v) for k, v in by_purpose.items()
        },
    }


def _apply_usage_delta(
    out: AgentRunResult, provider: LLMProvider, before: dict,
) -> None:
    """Fold the post-run minus pre-run usage onto `out`, including the
    per-purpose breakdown (§7 token logging)."""
    out.input_tokens = getattr(provider, "usage_input_tokens", 0) - before["input"]
    out.output_tokens = (
        getattr(provider, "usage_output_tokens", 0) - before["output"]
    )
    out.cache_read_tokens = (
        getattr(provider, "usage_cache_read_tokens", 0) - before["cache_read"]
    )
    out.llm_calls = getattr(provider, "usage_llm_calls", 0) - before["calls"]

    after = getattr(provider, "usage_by_purpose", {}) or {}
    delta: dict = {}
    for purpose, bucket in after.items():
        prev = before["by_purpose"].get(purpose, {})
        d = {
            k: bucket.get(k, 0) - prev.get(k, 0)
            for k in ("input", "output", "cache_read", "cache_write", "calls")
        }
        if any(v for v in d.values()):
            delta[purpose] = d
    out.usage_by_purpose = delta


async def run_case_agent_with_workflow(
    case: EvalCase,
    provider: LLMProvider,
) -> AgentRunResult:
    """Mode 1: agent + workflow tool enabled.

    The named workflow must already be built on disk (the harness has
    already run the inline build by the time this is called).
    """
    record = wf_local._load_workflow_record(case.workflow)
    return await _drive_agent(
        case=case,
        provider=provider,
        enable_workflows=True,
        system=None,
        register_workflow_record=record,
        mode_label="workflow_on",
    )


async def run_case_agent_no_workflow(
    case: EvalCase,
    provider: LLMProvider,
    workflow_spec: str,
) -> AgentRunResult:
    """Mode 2: agent + no workflow + spec as system instructions.

    `workflow_spec` is the dataset's natural-language description.
    Empty spec is allowed (e.g. referenced-mode datasets that don't
    carry a spec) — the agent then just runs against the case's
    user turns with no procedure block.
    """
    system = _build_baseline_system(workflow_spec, None)
    return await _drive_agent(
        case=case,
        provider=provider,
        enable_workflows=False,
        system=system,
        register_workflow_record=None,
        mode_label="workflow_off",
    )
