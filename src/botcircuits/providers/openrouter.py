"""OpenRouter provider — Chat Completions API via the OpenAI SDK.

OpenRouter exposes an OpenAI-compatible API but only implements Chat
Completions, not the Responses API (https://openrouter.ai/docs/client-sdks/overview),
so this can't subclass OpenAIProvider. No hosted MCP/code-interpreter support;
local tools only, dispatched through the standard function-calling shape.
"""

from __future__ import annotations

import json
from typing import Any

from botcircuits.types import LLMResponse, Message, ToolCall
from botcircuits.providers.base import DEFAULT_TEMPERATURE, LLMProvider

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(LLMProvider):
    name = "openrouter"

    def __init__(self, model: str = "openai/gpt-4.1", api_key: str | None = None,
                 base_url: str | None = None):
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url or DEFAULT_BASE_URL)
        self.model = model

    def supports_hosted_mcp(self) -> bool:
        return False

    async def aclose(self) -> None:
        await self.client.close()

    def _msgs_to_chat(self, system: str, messages: list[Message]) -> list[dict]:
        chat: list[dict] = []
        if system:
            chat.append({"role": "system", "content": system})
        for m in messages:
            if m.role == "assistant":
                text_parts = [b["text"] for b in m.blocks if b["type"] == "text"]
                tool_calls = [b for b in m.blocks if b["type"] == "tool_call"]
                entry: dict[str, Any] = {"role": "assistant"}
                entry["content"] = "\n".join(text_parts) if text_parts else None
                if tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc["arguments"]),
                            },
                        }
                        for tc in tool_calls
                    ]
                chat.append(entry)
            elif m.role == "user":
                text_parts = [b["text"] for b in m.blocks if b["type"] == "text"]
                if text_parts:
                    chat.append({"role": "user", "content": "\n".join(text_parts)})
                for b in m.blocks:
                    if b["type"] == "tool_result":
                        chat.append({
                            "role": "tool",
                            "tool_call_id": b["tool_call_id"],
                            "content": b["content"],
                        })
        return chat

    def _build_kwargs(self, system, messages, tools, hosted_mcp, skills, max_tokens):
        if hosted_mcp:
            print(f"[warn] OpenRouterProvider: hosted MCP ignored ({len(hosted_mcp)}). "
                  f"Use mode='local'.")
        if skills:
            print(f"[warn] OpenRouterProvider: skills ignored ({len(skills)}). "
                  f"No hosted code execution on OpenRouter.")

        api_tools = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._msgs_to_chat(system, messages),
            "max_tokens": max_tokens,
            "temperature": DEFAULT_TEMPERATURE,
        }
        if api_tools:
            kwargs["tools"] = api_tools
        return kwargs

    def _normalize(self, resp) -> LLMResponse:
        choice = resp.choices[0]
        msg = choice.message
        text = msg.content or ""
        tool_calls = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

        stop_reason = "tool_use" if tool_calls else (
            "max_tokens" if choice.finish_reason == "length" else "end_turn"
        )
        usage = getattr(resp, "usage", None)
        pin = int(getattr(usage, "prompt_tokens", 0) or 0)
        pout = int(getattr(usage, "completion_tokens", 0) or 0)
        details = getattr(usage, "prompt_tokens_details", None)
        cache_read = int(getattr(details, "cached_tokens", 0) or 0)
        self.record_usage(pin, pout, cache_read)
        return LLMResponse(text=text.strip(), tool_calls=tool_calls,
                           stop_reason=stop_reason, raw=resp,
                           input_tokens=pin, output_tokens=pout,
                           cache_read_tokens=cache_read)

    async def complete(self, system, messages, tools, hosted_mcp, skills, max_tokens):
        kwargs = self._build_kwargs(system, messages, tools, hosted_mcp, skills, max_tokens)
        resp = await self.client.chat.completions.create(**kwargs)
        return self._normalize(resp)

    async def stream(self, system, messages, tools, hosted_mcp, skills, max_tokens):
        kwargs = self._build_kwargs(system, messages, tools, hosted_mcp, skills, max_tokens)
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}

        text_parts: list[str] = []
        tool_call_chunks: dict[int, dict] = {}
        finish_reason = None
        usage = None

        stream = await self.client.chat.completions.create(**kwargs)
        async for chunk in stream:
            if getattr(chunk, "usage", None):
                usage = chunk.usage
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta
            if delta and delta.content:
                text_parts.append(delta.content)
                yield ("text_delta", delta.content)
            if delta and delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    entry = tool_call_chunks.setdefault(
                        tc_delta.index, {"id": None, "name": None, "arguments": ""})
                    if tc_delta.id:
                        entry["id"] = tc_delta.id
                    if tc_delta.function and tc_delta.function.name:
                        entry["name"] = tc_delta.function.name
                    if tc_delta.function and tc_delta.function.arguments:
                        entry["arguments"] += tc_delta.function.arguments

        tool_calls = []
        for entry in tool_call_chunks.values():
            try:
                args = json.loads(entry["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=entry["id"], name=entry["name"], arguments=args))

        stop_reason = "tool_use" if tool_calls else (
            "max_tokens" if finish_reason == "length" else "end_turn"
        )
        pin = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        pout = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        details = getattr(usage, "prompt_tokens_details", None) if usage else None
        cache_read = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
        self.record_usage(pin, pout, cache_read)
        yield ("final", LLMResponse(text="".join(text_parts).strip(),
                                     tool_calls=tool_calls, stop_reason=stop_reason,
                                     raw=None, input_tokens=pin, output_tokens=pout,
                                     cache_read_tokens=cache_read))
