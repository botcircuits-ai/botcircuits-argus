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

```
 user message
      │
      ▼
 append to history ──(workflow paused? resume it directly)
      │
      ▼
 ┌─► provider.complete / stream          (system + history + tools)
 │        │
 │        ▼
 │   interpret reply ── mode-agnostic: native tool_calls | ReAct parse
 │        │
 │        ├── tool calls ──► run all concurrently ──► results appended ──┐
 │        │                  (human_feedback? pause: reply = question)   │
 │        │                                                              │
 │        └── terminal text                                              │
 │                │                                                      │
 │                ▼                                                      │
 │        verification gate ── code changed, no observed test pass?      │
 │                │      │                                               │
 │           pass │      └── nudge "run the tests" ──────────────────────┤
 │                ▼                                                      │
 │          reply → user                                                 │
 └───────────────────────────────────────────────────────────────────◄──┘
                                             (≤ max_steps rounds, default 500)
```

1. Append the user message, then the deterministic workflow entry runs
   BEFORE any model decision: a paused workflow is resumed directly (the
   message *is* the answer), and an explicit "run <workflow>" request
   invokes that workflow tool itself (`match_workflow_trigger`) — routing
   a named workflow never depends on the model.
2. Call the provider with system + history + exposed tools.
3. Interpret the response (`_interpret`): text, tool calls, terminal?
4. Terminal → return the text. Otherwise run all tool calls concurrently,
   append the results as a tool-result message, and go to 2.
5. Stop early when `human_feedback` fired (pause: surface the question,
   the user's next message resumes) or after `max_steps` (default 500).
6. Before accepting a terminal reply, the verification gate runs: if this
   turn changed code and the project declares a test command, the loop
   demands an observed passing run first (see
   [verification.md](verification.md)).

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
