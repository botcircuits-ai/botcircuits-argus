"""OpenAI provider — Responses API (hosted MCP + code_interpreter).

Stateless when previous_response_id isn't passed; our ConversationStore
manages history.
"""

from __future__ import annotations

import json
import re
from typing import Any

from botcircuits.types import LLMResponse, Message, ToolCall
from botcircuits.providers.base import DEFAULT_TEMPERATURE, LLMProvider


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, model: str = "gpt-4.1", api_key: str | None = None,
                 base_url: str | None = None):
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.model = model

    def supports_hosted_mcp(self) -> bool:
        return True

    async def aclose(self) -> None:
        await self.client.close()

    def _msgs_to_input(self, system: str, messages: list[Message]) -> list[dict]:
        """Responses API takes a flat 'input' list with typed items."""
        items: list[dict] = []
        if system:
            items.append({"role": "system", "content": system})
        for m in messages:
            if m.role == "assistant":
                text_parts = [b["text"] for b in m.blocks if b["type"] == "text"]
                if text_parts:
                    items.append({"role": "assistant",
                                  "content": "\n".join(text_parts)})
                for b in m.blocks:
                    if b["type"] == "tool_call":
                        items.append({
                            "type": "function_call",
                            "call_id": b["id"],
                            "name": b["name"],
                            "arguments": json.dumps(b["arguments"]),
                        })
            elif m.role == "user":
                text_parts = [b["text"] for b in m.blocks if b["type"] == "text"]
                if text_parts:
                    items.append({"role": "user",
                                  "content": "\n".join(text_parts)})
                for b in m.blocks:
                    if b["type"] == "tool_result":
                        items.append({
                            "type": "function_call_output",
                            "call_id": b["tool_call_id"],
                            "output": b["content"],
                        })
        return items

    def _build_kwargs(self, system, messages, tools, hosted_mcp, skills, max_tokens):
        api_tools: list[dict] = []
        for t in tools:
            api_tools.append({
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            })
        for s in hosted_mcp:
            entry: dict = {
                "type": "mcp",
                "server_label": s.name,
                "server_url": s.url,
                "require_approval": s.require_approval,
            }
            if s.authorization_token:
                entry["headers"] = {"Authorization": f"Bearer {s.authorization_token}"}
            if s.allowed_tools:
                entry["allowed_tools"] = s.allowed_tools
            api_tools.append(entry)
        if skills:
            # OpenAI doesn't have named skill bundles; any non-empty skills
            # list just enables hosted code execution.
            api_tools.append({
                "type": "code_interpreter",
                "container": {"type": "auto"},
            })

        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": self._msgs_to_input(system, messages),
            "max_output_tokens": max_tokens,
        }
        # Reasoning-style models (gpt-5*, o1*, o3*) reject a non-default
        # temperature outright ("Unsupported parameter: 'temperature'").
        if not re.match(r"^(gpt-5|o1|o3)", self.model):
            kwargs["temperature"] = DEFAULT_TEMPERATURE
        if api_tools:
            kwargs["tools"] = api_tools
        return kwargs

    def _normalize(self, resp, tools) -> LLMResponse:
        text_parts, tool_calls = [], []
        local_names = {t.name for t in tools}
        for item in resp.output:
            t = getattr(item, "type", None)
            if t == "message":
                for c in item.content:
                    if getattr(c, "type", None) in ("output_text", "text"):
                        text_parts.append(c.text)
            elif t == "function_call" and item.name in local_names:
                # Hosted MCP and code_interpreter calls also appear in
                # resp.output but with different types; we only surface
                # local function calls to the agent loop.
                try:
                    args = json.loads(item.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCall(id=item.call_id, name=item.name,
                                            arguments=args))
        stop_reason = "tool_use" if tool_calls else "end_turn"
        usage = getattr(resp, "usage", None)
        # Responses API `input_tokens` is the total; the automatically-cached
        # portion (prompts >1024 tokens) is broken out in the details.
        pin = int(getattr(usage, "input_tokens", 0) or 0)
        pout = int(getattr(usage, "output_tokens", 0) or 0)
        details = getattr(usage, "input_tokens_details", None)
        cache_read = int(getattr(details, "cached_tokens", 0) or 0)
        self.record_usage(pin, pout, cache_read)
        return LLMResponse(text="\n".join(text_parts).strip(),
                           tool_calls=tool_calls, stop_reason=stop_reason, raw=resp,
                           input_tokens=pin, output_tokens=pout,
                           cache_read_tokens=cache_read)

    async def complete(self, system, messages, tools, hosted_mcp, skills, max_tokens):
        kwargs = self._build_kwargs(system, messages, tools, hosted_mcp,
                                     skills, max_tokens)
        resp = await self.client.responses.create(**kwargs)
        return self._normalize(resp, tools)

    async def stream(self, system, messages, tools, hosted_mcp, skills, max_tokens):
        kwargs = self._build_kwargs(system, messages, tools, hosted_mcp,
                                     skills, max_tokens)
        kwargs["stream"] = True
        events = await self.client.responses.create(**kwargs)
        final_response = None
        async for event in events:
            etype = getattr(event, "type", "")
            if etype == "response.output_text.delta":
                yield ("text_delta", event.delta)
            elif etype == "response.completed":
                final_response = event.response
            # We intentionally don't surface argument deltas — partial JSON
            # is lossy. Final tool calls live on event.response.output.
        if final_response is None:
            yield ("final", LLMResponse(text="", tool_calls=[],
                                         stop_reason="other", raw=None))
            return
        yield ("final", self._normalize(final_response, tools))
