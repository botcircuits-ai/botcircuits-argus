"""LLMProvider abstract base class.

Every concrete provider must implement `complete()` (single non-streaming
call) and `stream()` (async generator that yields text deltas and exactly
one `('final', LLMResponse)` at the end).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from botcircuits.agent.mcp import MCPServer
from botcircuits.agent.skill import SkillSpec
from botcircuits.agent.tools import LocalTool
from botcircuits.types import LLMResponse, Message, ProviderStreamEvent

#: Greedy decoding for reproducibility. botcircuits' core claim is run-to-run
#: predictability, so we pin temperature to 0 on every provider.
DEFAULT_TEMPERATURE: float = 0.0

#: Fixed sampling seed for providers that accept one (currently only Gemini
DEFAULT_SEED: int = 0


class LLMProvider(ABC):
    name: str = "base"
    model: str = ""

    # Session-cumulative real usage, accumulated by `record_usage()` on every
    # normalized response. Provider-level (not Agent-level) on purpose: the
    # agent loop is not the only caller — workflow Layer-B normalization and
    # the condition indexer call `complete()` directly, and their tokens must
    # count too. Class attributes are safe int defaults; `self.x += n` creates
    # per-instance attributes on first write.
    #
    # `usage_input_tokens` is the TOTAL prompt size (cached portion included);
    # the cache counters break out how much of it was served from / written
    # to the prompt cache, so cost accounting can apply the vendor's cache
    # discount instead of billing everything at the full input rate.
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0
    usage_cache_read_tokens: int = 0
    usage_cache_write_tokens: int = 0
    usage_llm_calls: int = 0

    # Purpose tag for the NEXT recorded call, so per-call usage can be
    # attributed by intent: `trigger` (the conversational call that fires a
    # workflow tool), `segment` (an engine-driven segment execution),
    # `tier2_normalization` (the cheap-model slot-extraction fallback), or
    # `conversational` (anything else / the default). Callers set this
    # immediately before a `complete`/`stream` round-trip; `record_usage`
    # folds the call's tokens into `usage_by_purpose[tag]` and resets it.
    usage_purpose: str = "conversational"

    def record_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """Accumulate one API call's real token usage onto session totals.
        Concrete providers call this from their normalize step."""
        i = max(0, int(input_tokens or 0))
        o = max(0, int(output_tokens or 0))
        cr = max(0, int(cache_read_tokens or 0))
        cw = max(0, int(cache_write_tokens or 0))
        self.usage_input_tokens += i
        self.usage_output_tokens += o
        self.usage_cache_read_tokens += cr
        self.usage_cache_write_tokens += cw
        self.usage_llm_calls += 1

        # Per-purpose breakdown (lazily created so existing callers that
        # never set a purpose still work and land under "conversational").
        purpose = self.usage_purpose or "conversational"
        by_purpose = self.__dict__.setdefault("usage_by_purpose", {})
        bucket = by_purpose.setdefault(
            purpose,
            {"input": 0, "output": 0, "cache_read": 0,
             "cache_write": 0, "calls": 0},
        )
        delta = {"input": i, "output": o, "cache_read": cr,
                 "cache_write": cw, "calls": 1}
        for k, v in delta.items():
            bucket[k] += v
        # Remember the LAST call's bucket + amounts so the agent loop can
        # reclassify it post-hoc — a `conversational` call that turns out to
        # have fired a workflow tool is retagged `trigger` (§7), an intent
        # only knowable after the model has responded.
        self.__dict__["_last_call_usage"] = (purpose, delta)

    def last_call_usage(self) -> tuple[str, dict] | None:
        """The `(purpose, delta)` of the most recent recorded call, or None.
        The agent loop snapshots this right after a conversational call so it
        can retag that exact call later even if segment calls land in
        between (each `record_usage` overwrites the live `_last_call_usage`)."""
        return self.__dict__.get("_last_call_usage")

    def reclassify_call(
        self, snapshot: tuple[str, dict] | None, to_purpose: str,
    ) -> None:
        """Move a previously-snapshotted call's per-purpose tokens to
        `to_purpose`. Pass the value `last_call_usage()` returned right after
        the call. Totals are unchanged — only the breakdown shifts. Used to
        retag a `conversational` call as `trigger` once it's known to have
        fired a workflow tool (§7)."""
        if not snapshot:
            return
        from_purpose, delta = snapshot
        if from_purpose == to_purpose:
            return
        by_purpose = self.__dict__.setdefault("usage_by_purpose", {})
        src = by_purpose.get(from_purpose)
        if src:
            for k, v in delta.items():
                src[k] = max(0, src.get(k, 0) - v)
            if not any(src.values()):
                by_purpose.pop(from_purpose, None)
        dst = by_purpose.setdefault(
            to_purpose,
            {"input": 0, "output": 0, "cache_read": 0,
             "cache_write": 0, "calls": 0},
        )
        for k, v in delta.items():
            dst[k] += v

    @abstractmethod
    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[LocalTool],
        hosted_mcp: list[MCPServer],
        skills: list[SkillSpec],
        max_tokens: int,
    ) -> LLMResponse:
        ...

    @abstractmethod
    async def stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[LocalTool],
        hosted_mcp: list[MCPServer],
        skills: list[SkillSpec],
        max_tokens: int,
    ) -> AsyncIterator[ProviderStreamEvent]:
        """Yields provider-stream events. Must yield exactly one
        ('final', LLMResponse) at the end."""
        ...
        yield  # pragma: no cover  (marks this as an async generator)

    def supports_hosted_mcp(self) -> bool:
        return False

    async def aclose(self) -> None:
        """Override if the provider holds an async client to clean up."""
        return None
