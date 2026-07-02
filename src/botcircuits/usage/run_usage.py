"""Real token usage accumulated while RUNNING a workflow.

A workflow run executes one LLM call per action step (segment) plus the
occasional Tier-2 slot-resolution call. Each call bills tokens; this module
collects them into a per-step breakdown plus a session total so a run can
report exactly what it cost.

Two sources feed it, depending on runtime:

  - **Native providers** (anthropic/openai/gemini) already accumulate real
    usage on the `LLMProvider` via `record_usage`. The run snapshots that.
  - **CLI runtimes** (claude-code/codex/openclaw) bill inside their own
    process and report usage on their JSON stdout. `usage_from_stdout` digs
    the per-call counts out of those envelopes so CLI runs aren't blind.

`ActionUsage` is one step's tokens; `RunUsage` sums them and tracks the total.
Both serialize to plain dicts for the run's JSON output and the session trace.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

#: Keys CLI agents commonly nest a usage object under (claude-code puts it at
#: the envelope root as `usage`; some wrap the whole result first). Mirrors
#: result._ENVELOPE_TEXT_KEYS so both parsers peel the same envelopes.
_ENVELOPE_KEYS = ("result", "response", "output", "data")

#: Field-name aliases vendors use for the same count. We read the first key
#: present in each group.
_INPUT_KEYS = ("input_tokens", "prompt_tokens", "inputTokens")
_OUTPUT_KEYS = ("output_tokens", "completion_tokens", "outputTokens")
_CACHE_READ_KEYS = (
    "cache_read_input_tokens", "cache_read_tokens",
    "cached_tokens", "cacheReadInputTokens",
)
_CACHE_WRITE_KEYS = (
    "cache_creation_input_tokens", "cache_write_tokens",
    "cacheCreationInputTokens",
)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class ActionUsage:
    """Real token usage for one action step (segment) of a run.

    `input_tokens` is the TOTAL prompt size including any cached portion; the
    cache counters break out how much was served from / written to the prompt
    cache, so cost accounting can apply the vendor's cache discount.
    """

    step: str = ""
    runtime: str = ""
    #: The named agent (`agents.<name>` in the workflow doc) this call was
    #: pinned to, or "" for the run's default agent/model.
    agent: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["total_tokens"] = self.total_tokens
        return d


@dataclass
class RunUsage:
    """Per-step token usage plus the session total for one workflow run."""

    steps: list[ActionUsage] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0

    def add(self, usage: ActionUsage | None) -> None:
        """Fold one step's usage into the run totals and the per-step list.
        A `None` or all-zero usage is ignored so steps that did no LLM work
        (deterministic systemAction, Tier-0-only resolution) don't clutter the
        breakdown."""
        if usage is None:
            return
        if not (usage.input_tokens or usage.output_tokens or usage.calls):
            return
        self.steps.append(usage)
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_write_tokens += usage.cache_write_tokens
        self.calls += usage.calls

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_tokens": self.total_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "calls": self.calls,
            "steps": [s.to_dict() for s in self.steps],
        }


def _loads_lenient(text: str) -> Any:
    """JSON, then Python-literal (single-quoted dicts). Returns parsed or None.
    Mirrors result._loads_lenient so envelope handling stays consistent."""
    for parse in (json.loads, ast.literal_eval):
        try:
            return parse(text)
        except (ValueError, SyntaxError, TypeError):
            continue
    return None


def _first_int(obj: dict, keys: tuple[str, ...]) -> int:
    for k in keys:
        v = obj.get(k)
        if isinstance(v, bool):  # bools are ints in Python; never a count
            continue
        if isinstance(v, (int, float)):
            return max(0, int(v))
    return 0


def _usage_from_obj(obj: Any) -> dict[str, int] | None:
    """Pull the four token counts out of a parsed usage-bearing object.

    Accepts the usage block directly, OR a larger object carrying a `usage`
    sub-object (claude-code's envelope root), OR an envelope we peel first.
    Returns ``None`` when no recognizable counts are present.
    """
    if not isinstance(obj, dict):
        return None

    # A nested `usage` block wins (the common CLI shape).
    nested = obj.get("usage")
    if isinstance(nested, dict):
        got = _usage_from_obj(nested)
        if got is not None:
            return got

    counts = {
        "input_tokens": _first_int(obj, _INPUT_KEYS),
        "output_tokens": _first_int(obj, _OUTPUT_KEYS),
        "cache_read_tokens": _first_int(obj, _CACHE_READ_KEYS),
        "cache_write_tokens": _first_int(obj, _CACHE_WRITE_KEYS),
    }
    if any(counts.values()):
        return counts

    # Peel a known wrapper and retry once.
    for key in _ENVELOPE_KEYS:
        inner = obj.get(key)
        if isinstance(inner, dict):
            got = _usage_from_obj(inner)
            if got is not None:
                return got
        elif isinstance(inner, str) and inner.strip():
            parsed = _loads_lenient(inner.strip())
            got = _usage_from_obj(parsed)
            if got is not None:
                return got
    return None


def usage_from_stdout(
    raw: str, *, step: str = "", runtime: str = "",
) -> ActionUsage | None:
    """Parse a CLI agent's stdout for the usage of THIS invocation.

    CLI runtimes that emit ``--output-format json`` (claude-code) / ``--json``
    (codex/openclaw) put a `usage` block on stdout. We tolerantly locate it
    (whole-string parse → envelope peel → last-resort ``{...}`` span) and
    return an `ActionUsage`. Returns ``None`` when stdout carries no usage —
    the caller treats that as "this runtime doesn't report tokens" rather than
    an error, exactly how `complete()` already shrugs at unparsable stdout.
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    parsed = _loads_lenient(raw)
    counts = _usage_from_obj(parsed) if parsed is not None else None

    if counts is None:
        # Last resort: a `{...}` span anywhere in the output (prose around it).
        m = _JSON_OBJECT_RE.search(raw)
        if m:
            counts = _usage_from_obj(_loads_lenient(m.group(0)))

    if not counts:
        return None
    return ActionUsage(
        step=step,
        runtime=runtime,
        calls=1,
        **counts,
    )


__all__ = [
    "ActionUsage",
    "RunUsage",
    "usage_from_stdout",
]
