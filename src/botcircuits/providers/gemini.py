"""Google Gemini provider — no hosted MCP yet; local MCP works via the
shared LocalMCPManager."""

from __future__ import annotations

import inspect
import uuid
from typing import Any

from botcircuits.types import LLMResponse, Message, ToolCall
from botcircuits.providers.base import DEFAULT_SEED, DEFAULT_TEMPERATURE, LLMProvider


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, model: str = "gemini-2.5-flash", api_key: str | None = None):
        from google import genai
        self.client = genai.Client(api_key=api_key) if api_key else genai.Client()
        self.model = model

    def supports_hosted_mcp(self) -> bool:
        return False  # Gemini doesn't expose remote MCP via the API yet.

    def _msgs_to_contents(self, messages: list[Message]) -> list[dict]:
        """Gemini uses 'contents' with role user|model and parts."""
        contents: list[dict] = []
        for m in messages:
            role = "model" if m.role == "assistant" else "user"
            parts: list[dict] = []
            for b in m.blocks:
                if b["type"] == "text":
                    parts.append({"text": b["text"]})
                elif b["type"] == "tool_call":
                    part: dict = {"function_call": {
                        "name": b["name"], "args": b["arguments"]
                    }}
                    # Echo the thinking-model signature back, or Gemini rejects
                    # the request with 400 "missing a thought_signature".
                    sig = b.get("thought_signature")
                    if sig is not None:
                        part["thought_signature"] = sig
                    parts.append(part)
                elif b["type"] == "tool_result":
                    # Gemini pairs function_response by name, not id.
                    parts.append({"function_response": {
                        "name": b.get("name", b["tool_call_id"]),
                        "response": {"content": b["content"]},
                    }})
            if parts:
                contents.append({"role": role, "parts": parts})
        return contents

    def _build_config(self, system, tools, skills, max_tokens):
        from google.genai import types as gt
        # Wrap each LocalTool as a Python callable with a synthesized
        # signature so genai can introspect parameter names.
        py_callables = []
        for t in tools:
            props = list(((t.input_schema or {}).get("properties") or {}).keys())

            def _make(name, desc, handler, props):
                def fn(**kwargs):
                    return handler(kwargs)
                fn.__name__ = name
                fn.__doc__ = desc or f"Tool {name}"
                params = [
                    inspect.Parameter(p, inspect.Parameter.KEYWORD_ONLY,
                                      annotation=str)
                    for p in props
                ]
                fn.__signature__ = inspect.Signature(parameters=params)
                return fn
            py_callables.append(_make(t.name, t.description, t.handler, props))

        tool_list: list[Any] = list(py_callables)
        if skills:
            # Gemini doesn't have named skills; any non-empty skills list
            # enables the hosted code_execution tool.
            tool_list.append(gt.Tool(code_execution=gt.ToolCodeExecution()))

        return gt.GenerateContentConfig(
            system_instruction=system or None,
            tools=tool_list or None,
            max_output_tokens=max_tokens,
            temperature=DEFAULT_TEMPERATURE,
            seed=DEFAULT_SEED,
            # Auto function calling OFF so all tool dispatch flows through
            # the outer agent loop and history stays consistent.
            automatic_function_calling=gt.AutomaticFunctionCallingConfig(disable=True),
        )

    def _normalize_chunks(self, chunks: list[Any], tools) -> LLMResponse:
        """Both complete (single response) and stream (list of chunks) reduce here."""
        text_parts, tool_calls = [], []
        local_names = {t.name for t in tools}
        for chunk in chunks:
            for cand in (chunk.candidates or []):
                for part in (cand.content.parts if cand.content else []) or []:
                    if getattr(part, "text", None):
                        text_parts.append(part.text)
                    fc = getattr(part, "function_call", None)
                    if fc and fc.name in local_names:
                        tool_calls.append(ToolCall(
                            id=str(uuid.uuid4()), name=fc.name,
                            arguments=dict(fc.args) if fc.args else {},
                            # Preserve the thinking-model signature so it can be
                            # replayed on the next turn (Gemini 400s without it).
                            thought_signature=getattr(
                                part, "thought_signature", None)))
        stop_reason = "tool_use" if tool_calls else "end_turn"
        # usage_metadata on the LAST chunk that carries it holds the call's
        # cumulative totals (complete() passes a single-element list). Thinking
        # tokens are billed output, so they count toward output_tokens.
        # `prompt_token_count` already INCLUDES the implicitly-cached portion;
        # `cached_content_token_count` breaks it out (billed at a discount).
        pin = pout = cache_read = 0
        for chunk in reversed(chunks):
            um = getattr(chunk, "usage_metadata", None)
            if um is not None and getattr(um, "prompt_token_count", None) is not None:
                pin = int(um.prompt_token_count or 0)
                pout = (int(getattr(um, "candidates_token_count", 0) or 0)
                        + int(getattr(um, "thoughts_token_count", 0) or 0))
                cache_read = int(
                    getattr(um, "cached_content_token_count", 0) or 0)
                break
        self.record_usage(pin, pout, cache_read)
        return LLMResponse(text="".join(text_parts).strip(),
                           tool_calls=tool_calls, stop_reason=stop_reason,
                           raw=chunks, input_tokens=pin, output_tokens=pout,
                           cache_read_tokens=cache_read)

    async def complete(self, system, messages, tools, hosted_mcp, skills, max_tokens):
        if hosted_mcp:
            print(f"[warn] GeminiProvider: hosted MCP ignored ({len(hosted_mcp)}). "
                  f"Use mode='local'.")
        cfg = self._build_config(system, tools, skills, max_tokens)
        resp = await self.client.aio.models.generate_content(
            model=self.model,
            contents=self._msgs_to_contents(messages),
            config=cfg,
        )
        return self._normalize_chunks([resp], tools)

    async def stream(self, system, messages, tools, hosted_mcp, skills, max_tokens):
        if hosted_mcp:
            print(f"[warn] GeminiProvider: hosted MCP ignored ({len(hosted_mcp)}). "
                  f"Use mode='local'.")
        cfg = self._build_config(system, tools, skills, max_tokens)
        chunks: list[Any] = []
        stream = await self.client.aio.models.generate_content_stream(
            model=self.model,
            contents=self._msgs_to_contents(messages),
            config=cfg,
        )
        async for chunk in stream:
            chunks.append(chunk)
            text_piece = getattr(chunk, "text", None)
            if text_piece:
                yield ("text_delta", text_piece)
        yield ("final", self._normalize_chunks(chunks, tools))
