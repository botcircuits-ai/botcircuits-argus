"""Shared protocol types used across providers, the agent loop, the CLI
renderer, and the gateway SSE serializer.

These dataclasses carry no behavior — keep them that way. Anything with
logic belongs in the module that owns the responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Union


# ---------------------------------------------------------------------------
# Core message / tool-call / provider-response shapes
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """The model's request to invoke a tool."""
    id: str
    name: str
    arguments: dict
    # Opaque provider-specific data that must be echoed back when this tool
    # call is replayed in conversation history. Gemini "thinking" models
    # (gemini-2.5+/3.x) attach a `thought_signature` to each function-call part
    # and REJECT the next request (400 "missing a thought_signature") if it
    # isn't sent back. Other providers leave this None.
    thought_signature: bytes | None = None


@dataclass
class LLMResponse:
    """One provider response, normalized across vendors."""
    text: str
    tool_calls: list[ToolCall]
    stop_reason: Literal["end_turn", "tool_use", "max_tokens", "other"]
    raw: Any  # provider-native response, useful for debugging or token counting
    # Real token usage from the provider's API response (0 when the vendor
    # didn't report usage, e.g. an aborted stream). Normalized here so callers
    # never have to poke vendor-specific shapes out of `raw`.
    #
    # `input_tokens` is the TOTAL prompt size including any cached portion —
    # vendors disagree (Anthropic's `usage.input_tokens` EXCLUDES cached
    # tokens; Gemini's `prompt_token_count` and OpenAI's `input_tokens`
    # include them), so each provider normalizes to the total here.
    # `cache_read_tokens` is the portion served from the prompt cache
    # (billed at a steep discount); `cache_write_tokens` is the portion
    # written to it this call (Anthropic bills a 25% premium; Gemini/OpenAI
    # implicit caching has no write charge and reports 0).
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class Message:
    """One conversation turn. Content is a list of typed blocks so we can
    carry text, tool calls, and tool results uniformly."""
    role: Literal["user", "assistant", "system"]
    blocks: list[dict]
    # Block shapes:
    #   {"type": "text", "text": "..."}
    #   {"type": "tool_call", "id": "...", "name": "...", "arguments": {...}}
    #   {"type": "tool_result", "tool_call_id": "...", "name": "...",
    #    "content": "...", "is_error": False}


# ---------------------------------------------------------------------------
# Streaming event shapes
# ---------------------------------------------------------------------------


@dataclass
class StreamEvent:
    """One event in a streamed agent turn.

    Types:
      'text_delta'   : .text is an incremental chunk of assistant text
      'tool_call'    : a complete tool call was decided; .tool_call set
      'tool_result'  : a tool finished; .tool_call_id, .text, .is_error set
      'turn_end'     : one provider round done (the loop may continue)
      'done'         : full agent turn done; .text is the final reply
      'error'        : something failed; .text holds the message
    """
    type: Literal["text_delta", "tool_call", "tool_result",
                  "turn_end", "done", "error"]
    text: str | None = None
    tool_call: ToolCall | None = None
    tool_call_id: str | None = None
    is_error: bool = False
    session_id: str | None = None


# What providers' .stream() yields. A minimal contract that every vendor's
# streaming SDK can satisfy: text deltas plus exactly one final response.
ProviderStreamEvent = Union[
    tuple[Literal["text_delta"], str],
    tuple[Literal["final"], LLMResponse],
]
