"""Engine-driven workflow runner — the inversion-of-control loop.

Once a workflow starts, the ENGINE owns the loop. The LLM is a
subroutine the engine invokes per branch-delimited *segment* with a
constant-size, cache-stable prompt (see `agent.segments.SegmentRunner._run_segment`).
The state machine — not the conversation history — is the memory.

Contrast with `executor.run_flow` (the old LLM-driven path): there the
LLM drove and re-called the workflow tool to advance one step at a time,
replaying the whole history every round. Here the engine walks the
compiled `flow["segments"]`, calls the LLM once per segment, captures the
branch slots the call produced, evaluates the branch deterministically
(`evaluate_choices` — unchanged), and advances itself. The model can no
longer skip a step, reorder, or imitate stale history.

The loop yields control back to the conversational agent on exactly two
events:
  - workflow end          → `EngineResult(done=True, summary=...)`
  - user-interaction pause → `EngineResult(paused=True, question=...)`
    (a `question`-kind step, or a clarification step the engine inserts
    when a branch slot can't be filled confidently).

`run_workflow_engine` is provider-agnostic: it never calls the provider
directly. It calls back into the passed-in agent via `agent._run_segment`,
which owns the single provider round-trip + tool execution and reuses the
agent's existing tools / skills / MCP wiring.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol
from uuid import uuid4

from pathlib import Path

if TYPE_CHECKING:
    from botcircuits.usage.run_usage import ActionUsage, RunUsage

from botcircuits.agent.workflow.engine.handlers.choice import evaluate_choices
from botcircuits.agent.workflow.engine.item_resolver import resolve_item_facts
from botcircuits.agent.workflow.engine.result_render import (
    persist_result,
    render_result,
    result_summary_line,
)
from botcircuits.agent.workflow.engine.tier0_resolver import resolve_tier0
from botcircuits.agent.workflow.engine.utils import fill_text_with_slots
from botcircuits.agent.workflow.variable_normalizer import variables_for_step


def _BASE_DIR() -> Path:
    """The directory Tier-0 resolvers / result-render read files relative to:
    the run cwd (the agent runs with cwd = the workspace)."""
    return Path.cwd()

#: Upper bound on segments walked in one run — guards against a branch
#: cycle the deterministic graph could otherwise spin on forever.
_MAX_SEGMENTS = 500


@dataclass
class SegmentResult:
    """What `Agent._run_segment` returns for one segment call."""
    #: The assistant's final text for the segment (surfaced if the
    #: workflow ends right after).
    text: str = ""
    #: Branch slots the model reported via the synthetic `record_slots`
    #: tool (Tier 1), already filtered to the segment's branch variables.
    captured_slots: dict[str, Any] = field(default_factory=dict)
    #: True when the model asked the user a question via `human_feedback`
    #: during the segment — the engine yields so the user can reply.
    paused: bool = False
    #: The question to surface when `paused`.
    question: str = ""
    #: When the pause is specifically because a tool permission is missing
    #: (e.g. "WebSearch"), the tool name(s) the segment needs. The runner
    #: carries this up so a "yes, allow it" reply can grant it on resume.
    needs_tool: list[str] = field(default_factory=list)
    #: S3 — for a `listDecision` segment, the per-item fact-sets the model
    #: reported via `record_item_list`. The engine decides each deterministically.
    captured_items: list[dict] = field(default_factory=list)
    #: Real token usage this segment's LLM call(s) billed, when the runtime
    #: reports it (native providers always; CLI runtimes that emit a `usage`
    #: block on stdout). ``None`` when the runtime reports no usage. Carried so
    #: the run can publish a per-action-step token breakdown plus a total.
    usage: "ActionUsage | None" = None


@dataclass
class EngineResult:
    """What `run_workflow_engine` hands back to the workflow tool."""
    done: bool = False
    paused: bool = False
    summary: str = ""
    question: str = ""
    #: Segment head to resume from after a user-interaction pause.
    paused_step: str | None = None
    #: Tool name(s) a permission-style pause needs (propagated from the
    #: segment); empty for an ordinary user-input pause.
    needs_tool: list[str] = field(default_factory=list)
    #: Final slot values, for the summary line and the eval harness.
    slots: dict[str, Any] = field(default_factory=dict)
    #: Per-branch audit records (§6).
    decisions: list[dict] = field(default_factory=list)
    #: Real token usage for the run: per-action-step breakdown + session
    #: total. Populated from each segment's `SegmentResult.usage` (native
    #: providers and CLI runtimes that report it); empty when no runtime in
    #: the run reported any usage.
    usage: "RunUsage | None" = None


class SegmentRunner(Protocol):
    """The single capability the runner needs from the agent: run one
    segment (constant-size prompt + segment tools + record_slots) and
    return its result. Implemented by `Agent._run_segment`."""

    async def __call__(
        self,
        *,
        actions: list[str],
        branch_variables: list[dict],
        system_notes: list[str],
        slots: dict[str, Any],
        item_variables: list[dict] | None = None,
        data_variables: list[dict] | None = None,
        agent: str | None = None,
    ) -> SegmentResult: ...


def _segments_for(flow: dict) -> list[dict]:
    """The compiled segment list, or a one-step-per-segment fallback when
    a workflow predates segment computation (un-rebuilt `.build/`)."""
    segments = flow.get("segments")
    if isinstance(segments, list) and segments:
        return segments
    # Fallback: every pausing step is its own singleton segment. Branch
    # steps mark themselves so the runner still evaluates choices.
    steps = flow.get("steps") or {}
    out: list[dict] = []
    for step_id, step in steps.items():
        if step.get("type") not in ("agentAction", "question"):
            continue
        is_branch = bool(step.get("choices") or step.get("conditions"))
        out.append({
            "id": step_id,
            "steps": [step_id],
            "branchStep": step_id if is_branch else None,
        })
    return out


def _segment_index(segments: list[dict]) -> dict[str, dict]:
    return {s["id"]: s for s in segments}


def _action_texts(flow: dict, step_ids: list[str], slots: dict) -> list[str]:
    """Slot-interpolated action text for each pausing step in a segment."""
    steps = flow.get("steps") or {}
    ctx = {"slots": slots}
    out: list[str] = []
    for sid in step_ids:
        step = steps.get(sid) or {}
        action = (step.get("settings") or {}).get("action") or ""
        out.append(fill_text_with_slots(action, ctx) if action else "")
    return [a for a in out if a]


def _branch_variable_names(flow: dict) -> set[str]:
    """Every variable name referenced by any step's branch `choices`.

    These are the decision variables; everything else declared in
    `flow.variables` is a plain DATA variable — part of the workflow's
    key-value memory, carried across segments but not used to branch.
    """
    names: set[str] = set()
    for step in (flow.get("steps") or {}).values():
        for ch in (step.get("choices") or []):
            for expr in (ch.get("expressionList") or []):
                var = expr.get("variable")
                if isinstance(var, str):
                    names.add(var)
    return names


def _data_variables(flow: dict) -> list[dict]:
    """Declared variables that are NOT branch variables — the workflow's
    carried key-value memory. A segment reports any of these it produces into
    `slots`, so a later (stateless) segment can read them back."""
    branch = _branch_variable_names(flow)
    out: list[dict] = []
    for v in (flow.get("variables") or []):
        if isinstance(v, dict) and v.get("variableName") not in branch:
            out.append(v)
    return out


def input_variables(flow: dict) -> list[dict]:
    """Declared variables the USER must supply before the workflow can run —
    marked `input: true` in `flow.variables`. Everything else is either
    produced by the workflow or optional context. Unmarked workflows have no
    pre-start collection (legacy behavior)."""
    return [v for v in (flow.get("variables") or [])
            if isinstance(v, dict) and v.get("input")]


def _first_action_step_id(flow: dict) -> str | None:
    """The first step (walking `next` from `start`) that carries an action —
    the Tier-2 extractor uses its action text as context when resolving
    initial inputs from the conversation."""
    steps = flow.get("steps") or {}
    sid = flow.get("start")
    seen: set[str] = set()
    while isinstance(sid, str) and sid in steps and sid not in seen:
        seen.add(sid)
        step = steps[sid]
        if ((step.get("settings") or {}).get("action") or "").strip():
            return sid
        sid = step.get("next")
    return None


def _inputs_question(workflow_name: str, missing: list[dict]) -> str:
    """The deterministic collection question for unfilled input variables —
    built from the authored descriptions, no LLM involved."""
    lines = [f"To run {workflow_name}, please provide:"]
    for v in missing:
        name = v.get("variableName", "")
        desc = str(v.get("description") or "").strip()
        lines.append(f"- {name}" + (f" — {desc}" if desc else ""))
    return "\n".join(lines)


def _eval_message(workflow_name: str, slots: dict) -> dict:
    """Build the minimal `message` shape `evaluate_choices` reads from
    (it pulls `data.sessionContext.slots`)."""
    return {
        "inputText": "",
        "channel": "agent",
        "data": {"sessionContext": {"slots": slots}},
    }


#: Interpreters whose first script argument is the meaningful "tool" name —
#: `python3 bin/price.py` should surface as `price.py`, not `python3`.
_INTERPRETERS = frozenset((
    "python", "python3", "python2", "node", "ruby", "bash", "sh", "perl",
))


def _exec_tool_name(argv: list) -> str:
    """The tool name to report for an exec: the script if argv[0] is a known
    interpreter (the first arg ending in a script extension or path), else the
    program itself. Lets Tool Correctness match on the script a human reads in
    the workflow rather than the interpreter binary."""
    if not argv:
        return "exec"
    head = Path(str(argv[0])).name
    if head in _INTERPRETERS:
        for arg in argv[1:]:
            s = str(arg)
            if s.startswith("-"):
                continue  # interpreter flag, not the script
            return Path(s).name
    return head


async def _emit_execs(
    event_sink: Callable[[str, Any], Awaitable[None]] | None,
    execs: list[tuple[list, str, int, bool]],
) -> None:
    """Surface each deterministic engine exec as a tool_call/tool_result pair on
    the stream, matching what the LLM-driven tool path emits. The tool name is
    the executed program (e.g. `price.py`) so Tool Correctness can match it.
    No-op without a sink. Never raises — observability must not break a run."""
    if event_sink is None or not execs:
        return
    from botcircuits.types import ToolCall

    for argv, out, _rc, is_error in execs:
        argv = argv or []
        name = _exec_tool_name(argv)
        tc = ToolCall(id=f"engine-exec-{uuid4().hex[:8]}", name=name,
                      arguments={"argv": list(argv)})
        try:
            await event_sink("tool_call", tc)
            await event_sink("tool_result", (tc, out, is_error))
        except Exception:
            pass


async def _emit(
    event_sink: Callable[[str, Any], Awaitable[None]] | None,
    kind: str,
    payload: Any,
) -> None:
    """Send one observability event to the sink. Never raises — observability
    must not break a run."""
    if event_sink is None:
        return
    try:
        await event_sink(kind, payload)
    except Exception:  # pragma: no cover
        pass


def _record_decision(
    step: dict,
    matched_next: str | None,
    default_next: str | None,
    slots: dict,
    captured_keys: set[str],
) -> list[dict]:
    """One audit struct per condition the branch step evaluated (§6)."""
    records: list[dict] = []
    matched = matched_next is not None and matched_next != default_next
    for choice in step.get("choices") or []:
        for expr in choice.get("expressionList") or []:
            var = expr.get("variable")
            records.append({
                "variable": var,
                "operator": expr.get("operator"),
                "value": expr.get("value"),
                "slot_value": slots.get(var) if isinstance(var, str) else None,
                "slot_source": (
                    "llm_record_slots" if var in captured_keys else "deterministic"
                ),
                "matched_choice": (choice.get("next") == matched_next),
                "llm_extracted": var in captured_keys,
            })
    records.append({
        "matched_next": matched_next,
        "default_next": default_next,
        "branched": matched,
    })
    return records


async def run_workflow_engine(
    flow: dict,
    *,
    workflow_name: str,
    run_segment: SegmentRunner,
    start_step_id: str | None = None,
    slots: dict[str, Any] | None = None,
    resolve_unfilled: Callable[..., Awaitable[dict]] | None = None,
    event_sink: Callable[[str, Any], Awaitable[None]] | None = None,
) -> EngineResult:
    """Drive `flow` segment-by-segment until it ends or pauses for the user.

    `run_segment` is the agent callback that performs one segment's
    actions and returns the branch slots it captured. `slots` seeds the
    slot context (e.g. args the trigger call carried).

    `resolve_unfilled` is the optional Tier-0/Tier-2 slot backfill hook,
    called at a branch point with `(flow, step_id, variables, slots)` when
    one or more branch variables are still empty after Tier-1 capture. It
    returns a `{variableName: value}` dict of any it could satisfy
    (deterministic resolver first, cheap-model extraction last). When a
    branch variable is STILL empty after this, the engine routes to a
    clarification question instead of silently taking the default branch.

    `event_sink`, when given, is an async `(kind, payload)` callable (same shape
    the segment sink uses) that surfaces the engine's OWN deterministic tool
    runs — currently the per-item pricer execs in a listDecision step — as
    `tool_call`/`tool_result` events. Without it those execs run silently, so
    Tool Correctness sees an empty tool sequence and scores 0 even though the
    workflow really did invoke the tool.
    """
    segments = _segments_for(flow)
    if not segments:
        return EngineResult(done=True, summary=f"workflow {workflow_name}: no steps")

    by_id = _segment_index(segments)
    steps = flow.get("steps") or {}
    slots = dict(slots or {})
    decisions: list[dict] = []

    # Real token usage for this run: per-action-step + total. Each segment's
    # `run_segment` may attach `SegmentResult.usage` (native providers always;
    # CLI runtimes that report a `usage` block on stdout); we stamp it with the
    # segment head step id and fold it in here. `_account` is a no-op for
    # segments that did no LLM work or whose runtime reports nothing.
    from botcircuits.usage.run_usage import RunUsage

    run_usage = RunUsage()

    def _account(seg: "SegmentResult", step_id: str | None, agent: str | None = None) -> None:
        u = getattr(seg, "usage", None)
        if u is not None and not u.step:
            u.step = step_id or ""
        if u is not None and agent and not getattr(u, "agent", ""):
            u.agent = agent
        run_usage.add(u)

    # S4 — resolve every `flow.variables` entry that carries a deterministic
    # `resolver` up front, in code. This fills standalone values the result
    # template / later steps need (e.g. customer_id) without an LLM call, in
    # addition to the per-branch Tier-0 skip below. Best-effort: variables that
    # don't resolve are simply left for Tier-1.
    resolvable = [v for v in (flow.get("variables") or [])
                  if isinstance(v, dict) and isinstance(v.get("resolver"), dict)]
    for v in resolvable:
        one = resolve_tier0([v], slots, base_dir=_BASE_DIR())
        if one:
            slots.update(one)

    # Initial input collection — deterministic, BEFORE the first segment.
    # Variables marked `input: true` must be filled before the workflow can
    # start: first try to resolve them from the conversation already at hand
    # (the trigger args / `__last_user_message__`, via the same Tier-0/Tier-2
    # hook branches use); whatever is still missing pauses the run with ONE
    # authored-description question. Without this, the first segment's model
    # improvises its own `human_feedback` ask and the user's answer never
    # lands in the slots — the re-ask loop. `start_step_id` set means we're
    # resuming mid-flow, where inputs were already settled.
    inputs = input_variables(flow)
    if start_step_id is None and inputs:
        missing = _unfilled(inputs, slots)
        if missing and resolve_unfilled is not None:
            backfilled = await resolve_unfilled(
                flow=flow,
                step_id=_first_action_step_id(flow) or flow.get("start"),
                variables=missing,
                slots=slots,
            )
            for k, v in (backfilled or {}).items():
                if v not in (None, ""):
                    slots[k] = v
            missing = _unfilled(inputs, slots)
        if missing:
            return EngineResult(
                paused=True,
                question=_inputs_question(workflow_name, missing),
                paused_step=None,  # resume restarts collection, then the flow
                slots=slots,
            )

    # Pick the starting segment: the one whose head is the requested start
    # step, else the first segment (graph entry).
    current = by_id.get(start_step_id) if start_step_id else None
    if current is None:
        current = segments[0]

    last_text = ""
    walked = 0
    while current is not None:
        walked += 1
        if walked > _MAX_SEGMENTS:
            return EngineResult(
                done=True,
                summary=f"workflow {workflow_name}: stopped after "
                        f"{_MAX_SEGMENTS} segments (branch cycle?)",
                slots=slots,
                decisions=decisions,
                usage=run_usage,
            )

        branch_step_id = current.get("branchStep")
        branch_variables = (
            variables_for_step(flow, branch_step_id) if branch_step_id else []
        )
        # Carried key-value memory: declared non-branch variables a segment may
        # produce (e.g. `scraped_jobs`) so a later stateless segment reads them.
        data_variables = _data_variables(flow)
        actions = _action_texts(flow, current.get("steps") or [], slots)

        # Observability: announce entry into this segment (its head step, the
        # actions about to run, and the slot snapshot) so a tracer can record
        # the workflow's deterministic navigation. No-op without a sink.
        await _emit(event_sink, "step_enter", {
            "step": current.get("id"),
            "steps": list(current.get("steps") or []),
            "actions": list(actions),
            "slots": dict(slots),
        })

        # S4 — Tier-0 skip. When the segment is a SINGLE branch step explicitly
        # marked deterministic and EVERY one of its branch variables resolves in
        # code (resolver specs on all of them), the engine fills the slots
        # itself and skips the LLM call entirely — no provider round-trip, zero
        # tokens. The `deterministic` flag is the author's assertion that the
        # step has no side effects worth an LLM (it's a pure read-and-decide);
        # without it we always run the segment, so the skip can never drop a
        # step that actually does work.
        tier0 = None
        branch_step = steps.get(branch_step_id) or {} if branch_step_id else {}
        if (branch_step_id
                and branch_step.get("deterministic")
                and current.get("steps") == [branch_step_id]):
            tier0 = resolve_tier0(branch_variables, slots, base_dir=_BASE_DIR())
        if tier0 is not None:
            slots.update(tier0)
            captured_keys = set(tier0)
            default_next = branch_step.get("next")
            chosen = evaluate_choices(
                branch_step.get("choices") or [],
                _eval_message(workflow_name, slots),
                default_next,
            )
            decisions.extend(_record_decision(
                branch_step, chosen, default_next, slots, captured_keys,
            ))
            await _emit(event_sink, "branch", {
                "step": branch_step_id,
                "chosen_next": chosen,
                "default_next": default_next,
                "branched": chosen is not None and chosen != default_next,
                "slots": dict(slots),
                "tier0": True,
            })
            current = by_id.get(chosen) if chosen else None
            continue

        # S3 — listDecision. The model reports a LIST of per-item fact-sets in
        # one segment; the engine decides each element deterministically via the
        # same `evaluate_choices`, accumulating one record per element into the
        # `collectInto` slot. One LLM call → N deterministic decisions; the model
        # never picks an outcome word. Cost scales with branches (one segment),
        # not items.
        if branch_step_id and branch_step.get("type") == "listDecision":
            # S4-exec — if the step declares how to gather per-item facts
            # deterministically (itemSource + itemFacts), the ENGINE runs the
            # pricer per item itself and skips the LLM entirely. Otherwise the
            # model reports the fact list (S3 Tier-1).
            # Capture each pricer exec so we can surface it on the stream after
            # resolution (the resolver is sync; the sink is async).
            execs: list[tuple[list, str, int, bool]] = []
            engine_items = resolve_item_facts(
                branch_step, base_dir=_BASE_DIR(),
                on_exec=(lambda argv, out, rc, err:
                         execs.append((argv, out, rc, err)))
                if event_sink is not None else None,
            )
            if engine_items is not None:
                await _emit_execs(event_sink, execs)
                decided = _decide_list(workflow_name, branch_step, engine_items)
                collect_into = branch_step.get("collectInto")
                if isinstance(collect_into, str) and collect_into:
                    slots[collect_into] = decided
                decisions.extend(
                    {"step": branch_step_id, "item": d} for d in decided
                )
                nxt = branch_step.get("next")
                current = by_id.get(nxt) if nxt else None
                continue

            item_vars = branch_step.get("itemVariables") or []
            # Pass `agent` only when the segment is pinned to one, so simple
            # SegmentRunner callables (and tests) that don't accept the kwarg
            # keep working — same rationale as `data_variables` below.
            seg_kwargs: dict[str, Any] = {}
            segment_agent = current.get("agent")
            if segment_agent:
                seg_kwargs["agent"] = segment_agent
            seg = await run_segment(
                actions=actions,
                branch_variables=[],
                system_notes=[],
                slots=slots,
                item_variables=item_vars,
                **seg_kwargs,
            )
            _account(seg, current.get("id"), segment_agent)
            if seg.paused:
                return EngineResult(
                    paused=True, question=seg.question,
                    paused_step=current.get("id"), slots=slots,
                    needs_tool=list(seg.needs_tool),
                    decisions=decisions, usage=run_usage,
                )
            decided = _decide_list(
                workflow_name, branch_step, seg.captured_items,
            )
            collect_into = branch_step.get("collectInto")
            if isinstance(collect_into, str) and collect_into:
                slots[collect_into] = decided
            decisions.extend(
                {"step": branch_step_id, "item": d} for d in decided
            )
            nxt = branch_step.get("next")
            current = by_id.get(nxt) if nxt else None
            continue

        # Pass `data_variables`/`agent` only when present so simple
        # SegmentRunner callables (and tests) that don't accept the kwarg
        # keep working — mirrors how `item_variables` is passed only on the
        # listDecision path.
        seg_kwargs: dict[str, Any] = {}
        if data_variables:
            seg_kwargs["data_variables"] = data_variables
        segment_agent = current.get("agent")
        if segment_agent:
            seg_kwargs["agent"] = segment_agent
        seg = await run_segment(
            actions=actions,
            branch_variables=branch_variables,
            system_notes=[],
            slots=slots,
            **seg_kwargs,
        )
        _account(seg, current.get("id"), segment_agent)
        last_text = seg.text or last_text

        # User-interaction pause: yield control so the user can reply. The
        # next workflow-tool call resumes from this same segment.
        if seg.paused:
            return EngineResult(
                paused=True,
                question=seg.question,
                paused_step=current.get("id"),
                needs_tool=list(seg.needs_tool),
                slots=slots,
                decisions=decisions,
                usage=run_usage,
            )

        # Tier-1 capture: fold the reported branch slots into context.
        captured_keys = set(seg.captured_slots)
        if seg.captured_slots:
            slots.update(seg.captured_slots)

        # Non-branching segment: advance to the segment seeded by the last
        # step's static `next` (computed at build time as another segment
        # head), or end the workflow.
        if not branch_step_id:
            # The resume reply was the answer to THIS segment's question; it has
            # now been consumed (run_segment saw it). Clear it before advancing
            # so a later question step reached on the same in-process walk
            # (e.g. a retry loop back to ask_order_id) pauses for fresh input
            # instead of re-consuming the stale reply and spinning forever.
            slots.pop("__last_user_message__", None)
            nxt = _static_next_after(current, steps)
            current = by_id.get(nxt) if nxt else None
            continue

        # Branch segment. Backfill any still-empty branch variable via the
        # Tier-0/Tier-2 hook before evaluating, so a value the model didn't
        # report through record_slots (but the user clearly supplied) still
        # routes correctly.
        branch_step = steps.get(branch_step_id) or {}
        missing = _unfilled(branch_variables, slots)
        if missing and resolve_unfilled is not None:
            backfilled = await resolve_unfilled(
                flow=flow,
                step_id=branch_step_id,
                variables=missing,
                slots=slots,
            )
            if backfilled:
                slots.update(backfilled)
                captured_keys |= set(backfilled)

        # Required-but-unfillable after backfill: route to clarification
        # rather than silently defaulting. The runner yields a question; the
        # resume cursor stays on this segment so the user's reply re-runs it.
        # Unmarked (optional) empties fall through to the default branch.
        still_missing = _required_unfilled(branch_variables, slots)
        if still_missing:
            question = _clarification_question(branch_step, still_missing)
            return EngineResult(
                paused=True,
                question=question,
                paused_step=current.get("id"),
                slots=slots,
                decisions=decisions,
                usage=run_usage,
            )

        # Evaluate deterministically against current slots.
        default_next = branch_step.get("next")
        chosen = evaluate_choices(
            branch_step.get("choices") or [],
            _eval_message(workflow_name, slots),
            default_next,
        )
        decisions.extend(_record_decision(
            branch_step, chosen, default_next, slots, captured_keys,
        ))
        await _emit(event_sink, "branch", {
            "step": branch_step_id,
            "chosen_next": chosen,
            "default_next": default_next,
            "branched": chosen is not None and chosen != default_next,
            "slots": dict(slots),
        })
        # The resume reply has now been fully used by this branch segment (its
        # run_segment, the Tier-0/Tier-2 backfill, and the branch eval). Clear
        # it before following the chosen edge so a later question step in the
        # same walk (e.g. ask_retry → ask_order_id → … → ask_retry) pauses for
        # fresh input rather than re-consuming the stale reply and looping.
        slots.pop("__last_user_message__", None)
        current = by_id.get(chosen) if chosen else None

    # S2 — engine renders the final answer from its own state (a declared
    # `flow.result`), so the model never spends output tokens emitting it. Falls
    # back to the legacy outcome+slots line when no result is declared or it
    # can't be rendered.
    rendered = render_result(flow, slots, base_dir=_BASE_DIR())
    if rendered is not None:
        # Optionally persist the engine-rendered answer to a file so out-of-
        # process consumers (CLIs that truncate tool-result previews, eval
        # harnesses) can read the FULL result, not a display-clipped summary.
        persist_result(flow, rendered, base_dir=_BASE_DIR())
        summary = result_summary_line(workflow_name, rendered)
    else:
        summary = _summary_line(workflow_name, last_text, slots)
    return EngineResult(
        done=True, summary=summary, slots=slots, decisions=decisions,
        usage=run_usage,
    )


def _decide_list(
    workflow_name: str,
    step: dict,
    items: list[dict],
) -> list[dict]:
    """S3 — apply the listDecision step's `choices` to EACH reported item and
    return one decided record per item.

    For each item, `evaluate_choices` runs against that item's facts (as the
    slot context) and yields a `next` label naming the outcome; the default
    `next` covers the no-match case. The result record is the item's fields the
    workflow wants to keep (`emit` field list, or all of them) plus
    `{<decisionKey>: <label>}`. Deterministic: same facts → same decision.
    """
    choices = step.get("choices") or []
    # The no-match fallback. listDecision steps carry it as `defaultNext`
    # (e.g. "fulfill"); fall back to a plain `next` for older shapes. Reading
    # only `next` here made every default-branch item decide to `None` — the
    # common "fulfill" path emitted `decision: null`.
    default_next = step.get("defaultNext") or step.get("next")
    decision_key = step.get("decisionKey") or "decision"
    emit_fields = step.get("emit")  # optional whitelist of item fields to keep
    # Optional: {field: [labels]} — null out `field` when the decision is one of
    # `labels` (e.g. line_total must be null on reject). Deterministic.
    null_on = step.get("nullOn") or {}
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = evaluate_choices(
            choices, _eval_message(workflow_name, item), default_next,
        )
        kept = (
            {k: item.get(k) for k in emit_fields}
            if isinstance(emit_fields, list) else dict(item)
        )
        kept[decision_key] = label
        for field_name, labels in null_on.items():
            if isinstance(labels, list) and label in labels:
                kept[field_name] = None
        out.append(kept)
    return out


def _unfilled(variables: list[dict], slots: dict) -> list[dict]:
    """Branch variables whose slot value is still empty/absent."""
    out: list[dict] = []
    for v in variables:
        name = v.get("variableName")
        if not isinstance(name, str):
            continue
        if slots.get(name) in (None, ""):
            out.append(v)
    return out


def _required_unfilled(variables: list[dict], slots: dict) -> list[dict]:
    """Subset of `_unfilled` that should trigger a clarification rather than
    a silent default-branch fallthrough.

    A branch variable forces clarification only when it is explicitly marked
    `required: true` in the flow schema. An unmarked variable left empty is
    a legitimate "no value applies" — the deterministic default branch is the
    correct route (e.g. an optional early-termination id), and over-asking
    would regress the common path. The first-class clarification path (§4)
    is reserved for variables the workflow author declared mandatory.
    """
    return [
        v for v in _unfilled(variables, slots)
        if v.get("required") is True
    ]


def _clarification_question(branch_step: dict, missing: list[dict]) -> str:
    """A user-facing question asking for the branch variables that could
    not be filled — the first-class clarification path that replaces a
    silent default-branch fallthrough (§4)."""
    names = [v.get("description") or v.get("variableName")
             for v in missing if v.get("variableName")]
    action = (branch_step.get("settings") or {}).get("action") or ""
    asked = "; ".join(str(n) for n in names if n)
    if action:
        return (
            f"To continue, I need a bit more information: {asked}. "
            f"(Step: {action})"
        )
    return f"To continue, please provide: {asked}."


def _static_next_after(segment: dict, steps: dict) -> str | None:
    """The `next` of a non-branching segment's last step — the head of the
    segment that runs next."""
    ordered = segment.get("steps") or []
    if not ordered:
        return None
    last = steps.get(ordered[-1]) or {}
    nxt = last.get("next")
    return nxt if isinstance(nxt, str) and nxt else None


def _summary_line(workflow_name: str, last_text: str, slots: dict) -> str:
    """The single line injected into conversational history on completion
    (§5)."""
    slot_part = ""
    filled = {
        k: v for k, v in slots.items()
        if v not in (None, "") and not k.startswith("__")
    }
    if filled:
        slot_part = f", slots {filled}"
    outcome = (last_text or "completed").strip()
    if len(outcome) > 200:
        outcome = outcome[:200] + "…"
    return f"workflow {workflow_name} completed: {outcome}{slot_part}"
