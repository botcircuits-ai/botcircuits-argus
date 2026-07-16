"""Shared test fakes — one scripted `LLMProvider` instead of a copy per file.

`ScriptedProvider` plays back canned `LLMResponse`s in order; `stream()` is
always derived from `complete()`, so subclasses that override `complete()`
(dynamic routing, call recording, failure injection) get streaming for free.

`text_response` / `tool_call_response` build the two response shapes every
agent-loop test needs.
"""

from __future__ import annotations

from botcircuits.providers.base import LLMProvider
from botcircuits.types import LLMResponse, ToolCall


def text_response(text: str) -> LLMResponse:
    """A terminal assistant reply."""
    return LLMResponse(text=text, tool_calls=[], stop_reason="end_turn", raw=None)


def tool_call_response(name: str, args: dict, call_id: str = "t1") -> LLMResponse:
    """A single tool-use turn."""
    return LLMResponse(
        text="", stop_reason="tool_use", raw=None,
        tool_calls=[ToolCall(id=call_id, name=name, arguments=args)],
    )


class ScriptedProvider(LLMProvider):
    """Plays back one canned LLMResponse per `complete()` call, in order.

    Subclass and override `complete()` for dynamic behavior — `stream()`
    always delegates to `complete()`, yielding a `text_delta` (when the
    response has text) followed by the `final` response.
    """

    name = "scripted"
    model = "test"

    def __init__(self, responses: list[LLMResponse] | None = None):
        self.responses = list(responses or [])

    async def complete(self, system, messages, tools, hosted_mcp,
                       skills, max_tokens) -> LLMResponse:
        return self.responses.pop(0)

    async def stream(self, system, messages, tools, hosted_mcp,
                     skills, max_tokens):
        resp = await self.complete(system, messages, tools, hosted_mcp,
                                   skills, max_tokens)
        if resp.text:
            yield "text_delta", resp.text
        yield "final", resp

    async def aclose(self) -> None:
        pass
