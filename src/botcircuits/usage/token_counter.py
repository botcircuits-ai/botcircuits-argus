"""Provider-aware token counting.

BotCircuits runs on many host agents — claude-code (Anthropic), codex
(OpenAI), hermes, openclaw, … — each with its own tokenizer. A workflow's
"size in tokens" therefore depends on whose model will read it, so this module
dispatches to a per-provider counter rather than baking in one tokenizer.

Design goals:

  - **No hard dependency / no network.** Every counter degrades to an offline
    heuristic. We try a provider's real tokenizer (Anthropic's local
    `count_tokens`, OpenAI's `tiktoken`) ONLY if it's already importable and
    works offline; any import / call failure falls back silently. Counting a
    workflow's footprint must never make an API call or raise.
  - **Registry, not branching.** `_COUNTERS` maps a provider/runtime family to
    a counter callable. Adding a tokenizer is one registry entry; unknown
    providers (hermes, openclaw) use the heuristic, which is good enough for a
    relative size estimate.

The provider key accepts both runtime names (`claude-code`, `codex`) and
vendor names (`anthropic`, `openai`, `gemini`), so callers can pass whichever
they have on hand.
"""

from __future__ import annotations

import json
from typing import Any, Callable

#: Average characters per token for the offline heuristic. ~4 is the long-
#: standing rule of thumb across GPT/Claude tokenizers for English-plus-JSON
#: text; we round up so the estimate never undersells the footprint.
_CHARS_PER_TOKEN = 4

#: Maps a normalized provider/runtime family → its counter. Vendor and runtime
#: aliases both resolve here via `_family_of`.
_COUNTERS: dict[str, Callable[[str], int]] = {}


def _heuristic_count(text: str) -> int:
    """Offline ~chars/4 estimate, rounded up, with a small floor so any
    non-empty text counts as at least one token. Tokenizer-agnostic and the
    universal fallback when no first-party counter is available."""
    if not text:
        return 0
    return max(1, -(-len(text) // _CHARS_PER_TOKEN))  # ceil division


def _anthropic_count(text: str) -> int:
    """Anthropic's local token counter, if the SDK exposes one offline.

    Newer `anthropic` SDKs ship a synchronous, network-free token counter
    (`anthropic.Anthropic().count_tokens`); older ones don't. Either way we
    only ever use it when it works WITHOUT a network call, so any
    AttributeError / TypeError / runtime error falls through to the heuristic.
    """
    try:
        import anthropic  # noqa: PLC0415 - optional, lazily imported

        client = anthropic.Anthropic(api_key="not-used-for-local-count")
        counter = getattr(client, "count_tokens", None)
        if callable(counter):
            n = counter(text)
            if isinstance(n, int) and n >= 0:
                return n
    except Exception:
        pass
    return _heuristic_count(text)


def _openai_count(text: str) -> int:
    """OpenAI's `tiktoken` count, if tiktoken is installed (it's offline once
    its encoding files are cached). Falls back to the heuristic otherwise."""
    try:
        import tiktoken  # noqa: PLC0415 - optional, lazily imported

        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except Exception:
        return _heuristic_count(text)


# Vendor families. Runtime names are aliased onto these in `_family_of`.
_COUNTERS["anthropic"] = _anthropic_count
_COUNTERS["openai"] = _openai_count
# gemini / hermes / openclaw have no readily-importable offline tokenizer here;
# they resolve to the heuristic via the default in `_resolve_counter`.


#: Runtime/provider name → vendor family. Anything not listed (hermes,
#: openclaw, an unknown CLI) resolves to the heuristic, which is fine for a
#: relative footprint.
_RUNTIME_TO_FAMILY = {
    "claude-code": "anthropic",
    "claude": "anthropic",
    "anthropic": "anthropic",
    "codex": "openai",
    "openai": "openai",
    "gpt": "openai",
    "gemini": "gemini",
    "google": "gemini",
}


def _family_of(provider: str | None) -> str:
    """Normalize a runtime/vendor name to a counter family key."""
    if not provider:
        return ""
    key = provider.strip().lower()
    if key in _RUNTIME_TO_FAMILY:
        return _RUNTIME_TO_FAMILY[key]
    # Substring match so `claude-code-foo` / `openai-compatible` still route.
    for needle, family in _RUNTIME_TO_FAMILY.items():
        if needle in key:
            return family
    return key


def _resolve_counter(provider: str | None) -> Callable[[str], int]:
    return _COUNTERS.get(_family_of(provider), _heuristic_count)


def count_tokens(text: str, provider: str | None = None) -> int:
    """Count tokens in `text` using the counter for `provider`.

    `provider` may be a runtime name (`claude-code`, `codex`, `openclaw`) or a
    vendor name (`anthropic`, `openai`). Unknown / None providers use the
    offline heuristic. Never raises and never makes a network call.
    """
    return _resolve_counter(provider)(text or "")


def count_json_tokens(obj: Any, provider: str | None = None) -> int:
    """Token count of `obj` serialized as compact JSON.

    Used for sizing workflow definitions (dicts) without the caller having to
    serialize first. Falls back to `str(obj)` for anything non-serializable.
    """
    try:
        text = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(obj)
    return count_tokens(text, provider)


def token_footprint(
    *,
    raw: Any = None,
    built: Any = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Static token footprint of a workflow's raw source and built artifact.

    Returns ``{"provider": <family>, "raw_tokens": n, "built_tokens": m,
    "total_tokens": n+m}``. Either input may be omitted (its count is 0).
    This is an authoring-time SIZE estimate — the context cost of carrying the
    definition — NOT tokens billed by any API call.
    """
    raw_tokens = count_json_tokens(raw, provider) if raw is not None else 0
    built_tokens = count_json_tokens(built, provider) if built is not None else 0
    return {
        "provider": _family_of(provider) or "heuristic",
        "raw_tokens": raw_tokens,
        "built_tokens": built_tokens,
        "total_tokens": raw_tokens + built_tokens,
    }


__all__ = [
    "count_tokens",
    "count_json_tokens",
    "token_footprint",
]
