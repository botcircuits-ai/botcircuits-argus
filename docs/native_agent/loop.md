# Loop (`agent/loop.py`)

The `Agent` class — the multi-round tool-use drive loop. It coordinates one
`LLMProvider`, one `ToolRegistry`, optional MCP servers, and optional skills,
and owns the `ConversationStore`.

```python
async with Agent(provider=make_provider("anthropic", None)) as agent:
    reply, sid = await agent.chat("hello")            # blocking turn
    async for ev in agent.chat_stream("...", sid):    # streaming turn
        ...
```

## Lifecycle

`start()` opens local MCP sessions and merges user tools + MCP tools +
filesystem skills into one registry (user tools win name collisions).
`aclose()` terminates background shells, closes MCP sessions and the provider.

## One turn

1. Append the user message; if a workflow is paused, resume it directly
   (no model decision — the message *is* the answer).
2. Call the provider with system + history + exposed tools.
3. Interpret the response (`_interpret`): text, tool calls, terminal?
4. Terminal → return the text. Otherwise run all tool calls concurrently,
   append the results as a tool-result message, and go to 2.
5. Stop early when `human_feedback` fired (pause: surface the question,
   the user's next message resumes) or after `max_steps` (default 500).

`chat_stream` is the same loop yielding `StreamEvent`s (`text_delta`,
`tool_call`, `tool_result`, `turn_end`, `done`, `error`), including live
events from inside engine-driven workflow segments.

## Modes

- `native` (default) — tools go to the provider's structured tool-use API.
- `react` — tools are described in the system prompt; the reply is parsed
  as Thought/Action/Action Input (see [react.md](react.md)).

The mode is isolated behind three small methods (`_exposed_tools`,
`_system_for_call`, `_interpret`, plus `_result_message`), so the rest of
the loop is mode-agnostic.

## Workflow gating

`enable_workflows=False` hides workflow tools from the model (they stay on
the registry for introspection) — used by the eval framework's no-workflow
baseline.
