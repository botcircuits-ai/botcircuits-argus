# Provider Abstraction

[← Implementation Guide index](../../IMPLEMENTATION.md)

---

## 6. Provider Abstraction

```python
class LLMProvider(ABC):
    async def complete(self, system, messages, tools, hosted_mcp,
                       skills, max_tokens) -> LLMResponse: ...
    async def stream(self, system, messages, tools, hosted_mcp,
                     skills, max_tokens):  # async generator
        yield ...
    def supports_hosted_mcp(self) -> bool: return False
    async def aclose(self) -> None: ...
```

A provider receives:
- `tools` — every callable available to the model (built-in local tools + user-registered + local-MCP-derived). The provider exposes these as the model's "function tools."
- `hosted_mcp` — MCP servers the **provider** will execute itself (relevant only when `supports_hosted_mcp()` is true).
- `skills` — hosted code-execution intents.

The provider is responsible for:
1. Translating `messages` into its wire format.
2. Wiring `hosted_mcp` and `skills` into vendor-specific parameters.
3. Calling its SDK.
4. Normalizing the response into `LLMResponse`.
5. (For streaming) yielding `("text_delta", str)` chunks plus a final `("final", LLMResponse)`.

### 6.1 Anthropic ([providers/anthropic.py](../../src/botcircuits/providers/anthropic.py))
- Uses `client.beta.messages` whenever any beta header is needed (MCP, Skills).
- Hosted MCP via `mcp_servers` parameter + `mcp-client-2025-11-20` beta.
- Skills via `container.skills` + `code_execution_20250825` tool + three beta headers.
- Streaming uses `client.messages.stream(...)`, which yields a `text` event for each delta and lets us call `get_final_message()` for the assembled result.

### 6.2 OpenAI ([providers/openai.py](../../src/botcircuits/providers/openai.py))
- Uses the **Responses API** (`client.responses.create`), not Chat Completions. Reason: hosted MCP and `code_interpreter` only exist on Responses.
- Hosted MCP entries go into the `tools` array as `{"type": "mcp", "server_label", "server_url", "require_approval"}`.
- Skills become `{"type": "code_interpreter", "container": {"type": "auto"}}`. `skill_id` is Anthropic-specific and ignored here.
- Streaming uses `stream=True` and listens for `response.output_text.delta` for live text and `response.completed` to grab the assembled response (which contains the final `function_call` items).

### 6.3 Gemini ([providers/gemini.py](../../src/botcircuits/providers/gemini.py))
- Uses `client.aio.models.generate_content` for native async.
- **No hosted MCP** as of 2026; configs with `mode="hosted"` are warned and skipped, **or** auto-promoted to `local` by the Agent.
- Skills map to `Tool(code_execution=ToolCodeExecution())`.
- Local function tools are wrapped as Python callables with synthesized signatures so genai's introspection picks up parameter names from `input_schema`.
- Streaming uses `client.aio.models.generate_content_stream(...)`; each chunk's `chunk.text` is the new text piece. Final `function_call` parts are read from accumulated chunks.
- Automatic function calling is **disabled** so all tool dispatch flows through the same outer agent loop, keeping history consistent across providers.

---
