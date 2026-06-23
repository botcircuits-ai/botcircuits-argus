"""Token-usage accounting for BotCircuits.

Two distinct kinds of "token usage" live here:

  - **Run usage** (`RunUsage`) — REAL tokens an LLM billed while *running* a
    workflow: per-action-step and the session total. Native providers already
    accumulate this via `LLMProvider.record_usage`; CLI runtimes
    (claude-code/codex/…) surface it on their JSON stdout, which
    `usage_from_stdout` parses. The engine folds each segment's usage into a
    `RunUsage` so the run output carries a per-step breakdown plus a total.

  - **Authoring footprint** (`token_footprint`) — a STATIC, provider-aware
    estimate of how many tokens a workflow *definition* occupies (its raw JSON
    source and its built artifact). No API call: it tokenizes the text with a
    counter chosen by provider so the same workflow reports a Claude-accurate
    count under claude-code, a GPT-accurate count under codex, and a sane
    heuristic for runtimes with no first-party tokenizer (hermes, openclaw).

The counter dispatch (`token_counter`) is the single seam both halves share,
so adding a provider's tokenizer is registry-only.
"""

from __future__ import annotations

from botcircuits.usage.run_usage import (
    ActionUsage,
    RunUsage,
    usage_from_stdout,
)
from botcircuits.usage.token_counter import (
    count_tokens,
    count_json_tokens,
    token_footprint,
)

__all__ = [
    "ActionUsage",
    "RunUsage",
    "usage_from_stdout",
    "count_tokens",
    "count_json_tokens",
    "token_footprint",
]
