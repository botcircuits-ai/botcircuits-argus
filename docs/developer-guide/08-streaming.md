# Streaming Pipeline

[← Implementation Guide index](../../IMPLEMENTATION.md)

---

## 10. Streaming Pipeline

Two layers of streaming:

### 10.1 Provider-level streaming
`provider.stream(...)` is an async generator yielding two-tuples:

```python
("text_delta", "Hello")        # incremental text chunk
("text_delta", " world")
("final", LLMResponse(...))    # assembled response, exactly once
```

This intentionally surfaces only what every provider can guarantee. Tool-call argument deltas are interesting but lossy (they're partial JSON); we read final tool calls from the assembled response instead. This tradeoff buys reliability across providers.

### 10.2 Agent-level streaming
`Agent.chat_stream(...)` runs the multi-round loop and yields normalized `StreamEvent`s:

```python
StreamEvent(type="text_delta", text="...")
StreamEvent(type="tool_call", tool_call=ToolCall(...))
StreamEvent(type="tool_result", tool_call_id="...", text="...", is_error=False)
StreamEvent(type="turn_end")          # one provider round done; loop may continue
StreamEvent(type="done", text="...")  # entire user turn done
StreamEvent(type="error", text="...")
```

This is the API the FastAPI gateway and CLI consume. A consumer never has to know which provider it's talking to; the events are stable.

The `tool_result` events use `asyncio.as_completed` so a slow MCP query doesn't block surfacing fast ones. Result blocks are still appended to history in original order.

---
