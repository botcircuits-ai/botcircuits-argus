"""CLI agent runtime — shell out to a host agent (claude-code et al.).

Headless, ONE process per segment, in an isolated working directory. The
host CLI's own tools / MCP / model do the real work; we hand it the segment's
actions as a prompt and read a strict-JSON object back on stdout. No SDK
binding — the only contract is "print this JSON shape as your final output",
which any CLI agent that can follow instructions satisfies.

Reuse, not reinvention:
  - The segment prompt reuses the engine's cache-stable `ENGINE_SYSTEM_PROMPT`
    and `build_segment_user_message`; we only swap the trailing instruction
    from "call the record_slots tool" to "print this JSON".
  - Slot resolution keeps Tier-0 deterministic in-process (`slot_resolver`,
    zero tokens, OS-independent); only Tier-2 semantic extraction crosses the
    CLI boundary, reusing `variable_normalizer`'s prompt body and
    hallucination guard.

This class is the reference CLI impl; codex/openclaw select it via config
(same JSON contract) until they need a bespoke output adapter.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from botcircuits.runtime.base import AgentRuntimeProvider, EventSink, RuntimeConfig
from botcircuits.runtime.cli_exec import CliExecError, run_cli
from botcircuits.runtime.result import (
    normalized_slots_from_stdout,
    segment_result_from_stdout,
)
from botcircuits.usage.run_usage import usage_from_stdout
from botcircuits.agent.workflow.engine.runner import SegmentResult
from botcircuits.agent.workflow.engine.segment_exec import (
    ENGINE_SYSTEM_PROMPT,
    build_segment_user_message,
)


#: Appended to the engine system prompt so the CLI agent reports through
#: stdout JSON instead of the (native-only) record_slots tool.
_CLI_OUTPUT_CONTRACT = (
    "\n\nOUTPUT CONTRACT. You have no record_slots tool here. After "
    "performing this segment's action(s), print EXACTLY ONE JSON object as "
    "your FINAL output and nothing after it:\n"
    '  {"slots": {<branchVar: value>, ...}, '
    '"items": [{<itemFact: value>, ...}], '
    '"paused": false, "question": "", "needs_tool": [], '
    '"text": "<short result>"}\n'
    "Rules:\n"
    "  - `slots`: the branch variables you were asked to report; omit any "
    "you do not genuinely have, never invent one.\n"
    "  - `items`: ONLY for a list-decision segment — one object of FACTS per "
    "list element (never a decision/outcome word). Omit otherwise.\n"
    "  - If an action needs information only the user can provide, set "
    '`"paused": true` and put the question in `"question"`, then stop.\n'
    "  - If you cannot proceed ONLY because a tool's permission is not "
    'granted (e.g. WebSearch/WebFetch), set `"paused": true`, list the '
    'exact tool name(s) in `"needs_tool"` (e.g. ["WebSearch"]), and put a '
    'short request in `"question"`. Only list a tool a permission error '
    "actually blocked — never one you already have.\n"
    "  - `text`: a short human-readable result line (optional).\n"
    "  - Output JSON ONLY for that final object — no markdown fence is "
    "required, but if you use one it must wrap the whole object."
)


def _resolve_tier2_prompt(
    variables: list[dict],
    slots: dict,
    action_text: str,
    last_user_message: str,
) -> str:
    """Build the Tier-2 extraction prompt, reusing the variable_normalizer
    body so the schema/rules/guard wording stays single-sourced."""
    from botcircuits.agent.workflow.variable_normalizer import _build_prompt

    body = _build_prompt(
        variables=variables,
        raw_args=slots,
        action_text=action_text,
        last_assistant_message="",
        last_user_message=last_user_message,
    )
    return (
        "You produce strict JSON only — no commentary, no markdown fences, no "
        "extra keys.\n\n" + body
    )


#: The CLI flag each host uses to pin a specific model on ONE invocation.
#: `claude` (claude-code) takes `--model`; `codex exec` takes `-m`/`--model` —
#: verify against the installed CLI's own `--help` before relying on this in
#: production, since these are maintained out-of-repo and can change.
#: Unlisted runtime names (e.g. openclaw, until confirmed) fall back to
#: `--model`, the most common convention among these CLIs.
_MODEL_FLAG: dict[str, str] = {
    "claude-code": "--model",
    "codex": "-m",
}


class ClaudeCodeRuntime(AgentRuntimeProvider):
    """Drive a workflow via a headless CLI agent, one process per segment."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        agents_config: dict[str, dict] | None = None,
    ):
        self.config = config
        self.name = config.name or "claude-code"
        # Agent name -> {"model": "..."} for agents routed to THIS runtime
        # instance. A segment pinned to one of these gets `--model <x>`
        # appended to just that invocation's argv.
        self.agents_config = agents_config or {}

    def _command(self, *, model: str | None = None) -> list[str]:
        """The spawn argv template, with any run-granted tools appended as
        ``--allowedTools <tool> …`` so a "yes, allow it" reply takes effect on
        the very next segment without the user touching settings.json.

        `model`, when given, appends this runtime's model flag (see
        `_MODEL_FLAG`) so JUST this invocation targets a different model —
        the per-agent override, resolved per call rather than baked into
        `self.config` so one instance serves every agent on this runtime."""
        cmd = list(self.config.command)
        tools = [t for t in (self.config.allowed_tools or []) if t]
        if tools:
            cmd += ["--allowedTools", *tools]
        if model:
            flag = _MODEL_FLAG.get(self.name, "--model")
            cmd += [flag, model]
        return cmd

    # -- segment execution --------------------------------------------------

    def _build_segment_prompt(
        self,
        *,
        actions: list[str],
        branch_variables: list[dict],
        system_notes: list[str],
        slots: dict[str, Any],
        item_variables: list[dict] | None,
        data_variables: list[dict] | None,
    ) -> str:
        """Assemble the full segment prompt (engine system prompt + output
        contract + segment message + carried-memory / resume blocks).

        Factored out so subclasses driving a different CLI (e.g. Hermes) reuse
        the exact same prompt without re-implementing the run loop.
        """
        user_msg = build_segment_user_message(
            actions, branch_variables, system_notes,
            item_variables=item_variables,
            data_variables=data_variables,
        )
        # The host CLI is stateless between segments, so it has no conversation
        # history. When the engine resumes a paused segment after the user
        # answered, that answer rides on the reserved `__last_user_message__`
        # slot — surface it in the prompt so the CLI agent can act on it.
        last_user = ""
        if isinstance(slots, dict):
            last_user = str(slots.get("__last_user_message__") or "")

        # Carried key-value memory: a prior segment may have produced data
        # variables (e.g. `tasks_data`) now sitting in `slots`. Because each
        # segment is a FRESH process with no history, the agent can't see them
        # unless we hand the VALUES over here — without this, a "print/save the
        # fetched data" step has nothing to act on and stalls. Surface only the
        # data variables in scope that actually have a value.
        memory_block = ""
        if isinstance(slots, dict) and data_variables:
            available = {}
            for v in data_variables:
                name = v.get("variableName") if isinstance(v, dict) else None
                if isinstance(name, str) and slots.get(name) not in (None, ""):
                    available[name] = slots[name]
            if available:
                memory_block = (
                    "\n\nAVAILABLE DATA (produced by earlier steps — use these "
                    "values directly; do NOT re-fetch or claim they are "
                    "missing):\n"
                    + json.dumps(available, ensure_ascii=False, default=str)
                )
        # On resume, the action text still reads as an instruction to ASK (e.g.
        # "Ask: check another order?"), but the user has ALREADY answered — their
        # reply is here. Without explicit guidance the agent re-asks and pauses
        # forever (the stuck retry-loop bug). Tell it to treat this reply as the
        # answer to THIS segment's question: fill the requested branch variables
        # from it and DO NOT pause again unless the reply genuinely fails to
        # answer (in which case re-ask, briefly acknowledging what they said).
        context_block = (
            "\n\nRESUMING AFTER A PAUSE. The user has already replied to this "
            "segment's question; their reply is below. Treat it as the answer: "
            "extract the requested branch variable(s) from it and report them — "
            'do NOT re-ask the same question or set "paused": true unless the '
            "reply genuinely does not answer it.\n"
            f"User reply: {last_user}"
        ) if last_user else ""
        return (
            ENGINE_SYSTEM_PROMPT
            + _CLI_OUTPUT_CONTRACT
            + "\n\n=== SEGMENT ===\n"
            + user_msg
            + memory_block
            + context_block
        )

    def _attach_usage(self, result: SegmentResult, stdout: str,
                      *, prompt: str, launched_at: float) -> None:
        """Attach this segment's real token usage to `result`.

        claude-code (and codex/openclaw) report a `usage` block on their JSON
        stdout, so we parse it out. Subclasses whose CLI hides usage from stdout
        (Hermes) override this to read it from elsewhere. `prompt`/`launched_at`
        are supplied for those out-of-band lookups; this base impl ignores them.
        """
        result.usage = usage_from_stdout(stdout, runtime=self.name)

    async def run_segment(
        self,
        *,
        actions: list[str],
        branch_variables: list[dict],
        system_notes: list[str],
        slots: dict[str, Any],
        item_variables: list[dict] | None = None,
        data_variables: list[dict] | None = None,
        agent: str | None = None,
        event_sink: EventSink | None = None,
    ) -> SegmentResult:
        """Run one segment by invoking the host CLI once.

        `agent`, when it names one of `self.agents_config`, pins JUST this
        invocation to that agent's model via the runtime's model flag (see
        `_MODEL_FLAG`). `event_sink` is ignored — headless one-shot mode has
        no incremental events to stream; the host shows its own progress.
        """
        prompt = self._build_segment_prompt(
            actions=actions,
            branch_variables=branch_variables,
            system_notes=system_notes,
            slots=slots,
            item_variables=item_variables,
            data_variables=data_variables,
        )
        launched_at = time.time()
        model = self.agents_config.get(agent, {}).get("model") if agent else None

        try:
            res = await run_cli(
                self._command(model=model), prompt, timeout=self.config.timeout,
                cwd=self.config.cwd,
            )
        except CliExecError as e:
            # Can't run the host CLI at all — surface as a paused question so
            # the run yields cleanly instead of crashing the engine.
            print(f"[runtime:{self.name}] {e}", file=sys.stderr)
            return SegmentResult(
                paused=True,
                question=(
                    f"The '{self.name}' runtime could not be started: {e}"
                ),
            )

        if not res.ok:
            print(
                f"[runtime:{self.name}] segment CLI exited rc={res.returncode}"
                f"{' (timeout)' if res.timed_out else ''}: "
                f"{res.stderr.strip()[:300]}",
                file=sys.stderr,
            )

        result = segment_result_from_stdout(res.stdout)
        # Attach the real token usage this segment billed. The engine folds it
        # into the run's per-action-step token breakdown; None when the runtime
        # reports nothing — the run simply shows no usage for it. Where usage
        # lives is runtime-specific (stdout for claude-code, the session store
        # for hermes), so it goes through `_attach_usage`.
        self._attach_usage(
            result, res.stdout, prompt=prompt, launched_at=launched_at,
        )
        return result

    # -- slot resolution ----------------------------------------------------

    async def resolve_slots(
        self,
        *,
        flow: dict,
        step_id: str,
        variables: list[dict],
        slots: dict[str, Any],
    ) -> dict[str, Any]:
        """Backfill empty branch variables: Tier-0 deterministic in-process,
        then Tier-2 semantic extraction via the host CLI for the remainder."""
        from botcircuits.agent.workflow.slot_resolver import resolve_slots as tier0
        from botcircuits.agent.workflow.local import _action_text_for_step
        from botcircuits.agent.workflow.variable_normalizer import (
            _value_present_in_context,
        )

        out: dict[str, Any] = {}
        last_user = (
            slots.get("__last_user_message__", "")
            if isinstance(slots, dict) else ""
        )

        # Tier 0 — deterministic, zero tokens.
        resolved, unresolved = tier0(
            flow=flow,
            step_id=step_id,
            variables=variables,
            raw_args={},
            saved_slots=slots,
            last_user_message=last_user,
        )
        if resolved:
            out.update(resolved)
        if not unresolved:
            return out

        # Tier 2 — CLI semantic extraction for whatever Tier-0 left empty.
        action_text = _action_text_for_step(flow, step_id)
        prompt = _resolve_tier2_prompt(
            unresolved, {**slots, **out}, action_text, last_user,
        )
        try:
            res = await run_cli(
                self._command(), prompt, timeout=self.config.timeout,
                cwd=self.config.cwd,
            )
        except CliExecError as e:
            print(f"[runtime:{self.name}] tier2 resolve skipped: {e}",
                  file=sys.stderr)
            return out

        extracted = normalized_slots_from_stdout(res.stdout)
        if not extracted:
            return out

        # Same guards the native normalizer applies: restrict to requested
        # variable names and drop values not present in the source context.
        allowed = {
            v.get("variableName") for v in unresolved
            if isinstance(v, dict) and isinstance(v.get("variableName"), str)
        }
        context_blob = "\n".join([
            json.dumps({**slots, **out}, default=str),
            action_text or "",
            last_user or "",
        ])
        for name, value in extracted.items():
            if name not in allowed:
                continue
            if not _value_present_in_context(value, context_blob):
                print(
                    f"[runtime:{self.name}] dropping hallucinated "
                    f"{name}={value!r} (not in source context)",
                    file=sys.stderr,
                )
                continue
            out[name] = value
        return out


__all__ = ["ClaudeCodeRuntime"]
