"""Hermes agent runtime — drive a workflow via `hermes -z` (headless oneshot).

Hermes satisfies the exact same CLI contract as claude-code: one stateless
process per segment, the segment's actions handed over as a prompt, a strict
JSON object read back as the model's FINAL output. So this provider is a thin
subclass of `ClaudeCodeRuntime` — it inherits the whole segment/slot pipeline
(prompt assembly, the JSON output contract, Tier-0/Tier-2 slot resolution,
resume-after-pause handling) and changes only what is genuinely different
about hermes:

  * **Invocation.** `hermes -z {prompt} --yolo` (set in the detect registry,
    not here). `-z/--oneshot` is single-turn headless; `--yolo` is hermes'
    auto-approve. Unlike claude-code there is no `--output-format json`, so the
    JSON contract object arrives as the model's plain final text — which the
    shared `extract_json_object` already parses out of bare/fenced output.

  * **Usage.** Hermes oneshot routes its internal run to devnull and prints
    only the reply, so `usage_from_stdout` finds nothing. The real counters
    (tokens, api_call_count, cache) live in hermes' SQLite session store; we
    override `_attach_usage` to harvest them by matching the session this
    segment created (see `usage.hermes_usage`).

Everything else — `run_segment`, `resolve_slots`, the permission-pause path —
is reused verbatim from the base class.
"""

from __future__ import annotations

from botcircuits.runtime.base import RuntimeConfig
from botcircuits.runtime.providers.claude_code import ClaudeCodeRuntime
from botcircuits.agent.workflow.engine.runner import SegmentResult
from botcircuits.usage.hermes_usage import harvest_usage


class HermesRuntime(ClaudeCodeRuntime):
    """Drive a workflow via the Hermes CLI, one oneshot process per segment."""

    def __init__(self, config: RuntimeConfig):
        super().__init__(config)
        self.name = config.name or "hermes"
        # Session ids already attributed to a segment in this provider instance.
        # Concurrent segments of one run could share an identical prompt; the
        # claim set stops two of them harvesting the same session row.
        self._claimed_sessions: set[str] = set()

    def _attach_usage(self, result: SegmentResult, stdout: str,
                      *, prompt: str, launched_at: float) -> None:
        """Override: hermes hides usage from stdout, so read it from the
        session store instead. Best-effort — `harvest_usage` returns None (and
        the segment shows no usage) if the store is absent or no session
        matches, exactly as the base does for an unparsable stdout usage block.
        """
        result.usage = harvest_usage(
            prompt, launched_at, claimed=self._claimed_sessions,
        )


__all__ = ["HermesRuntime"]
