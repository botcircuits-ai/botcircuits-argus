# Data Model & The Agent Loop

[← Implementation Guide index](../../IMPLEMENTATION.md)

---

## 4. Normalized Data Model

Provider-neutral types live in [types.py](../../src/botcircuits/types.py):

| Type            | Purpose                                                                  |
|-----------------|--------------------------------------------------------------------------|
| `Message`       | One conversation turn; content is a list of typed blocks                 |
| `ToolCall`      | The model's request to invoke a tool: `id`, `name`, `arguments`          |
| `LLMResponse`   | One provider response normalized: `text`, `tool_calls`, `stop_reason`    |
| `StreamEvent`   | One event in a streamed agent turn (`text_delta`, `tool_call`, …)        |

The remaining shapes live next to whichever subsystem owns them:

| Type            | Module                                       |
|-----------------|----------------------------------------------|
| `LocalTool`     | [agent/tools/registry.py](../../src/botcircuits/agent/tools/registry.py) |
| `MCPServer`     | [agent/mcp.py](../../src/botcircuits/agent/mcp.py) |
| `SkillSpec`     | [agent/skill/spec.py](../../src/botcircuits/agent/skill/spec.py) |
| `LocalSkill`    | [agent/skill/local.py](../../src/botcircuits/agent/skill/local.py) |
| `MemorySnapshot`| [agent/memory.py](../../src/botcircuits/agent/memory.py) |

### Why blocks instead of strings?

A user message might be plain text, but an assistant turn can contain text **and** several tool calls; a follow-up user turn might be entirely tool results. Modeling this as a list of typed blocks (`text`, `tool_call`, `tool_result`) lets the same `Message` shape carry every kind of turn without conditional fields.

When a provider needs to send history back to its API, it walks the blocks and emits the right wire format:
- Anthropic: `tool_use` / `tool_result` content blocks
- OpenAI Responses: separate `function_call` and `function_call_output` items
- Gemini: `function_call` / `function_response` parts

The conversion is mechanical, lives entirely inside the provider, and never leaks.

---

## 5. The Agent Loop

[agent/core.py](../../src/botcircuits/agent/core.py). `Agent.chat()` and `Agent.chat_stream()` share the same logic; the streaming version yields events through it.

```python
for step in range(max_steps):
    response = await provider.complete(...)        # or .stream(...)

    record assistant turn (text + tool_calls)

    if response.stop_reason != "tool_use":
        return response.text

    run all requested tools concurrently
    record user turn (tool_results)
```

Three details that matter:

### 5.1 Concurrent tool execution
When the model returns multiple tool calls in one turn, they run via `asyncio.gather` (blocking) or `asyncio.as_completed` (streaming). Independent MCP queries fan out. Result blocks are appended to history in the **original order** to keep call/result pairing intuitive — even though they may have completed out of order.

### 5.2 The `max_steps` ceiling
Without a cap, a misbehaving model could loop forever. Default is 10 rounds per user turn; raise it for deeper agentic tasks. Configurable via JSON (`max_steps`) or `--max-steps`.

### 5.3 Per-conversation lock
`Conversation.lock` is an `asyncio.Lock`. Two concurrent `chat()` calls on the same `session_id` serialize automatically — without it, message order would corrupt. Different sessions still run in parallel.

### 5.4 Workflow advancement, human-feedback pause, and reminders

Workflow execution is **engine-driven**: once a workflow tool fires, the workflow *engine* owns the loop and calls the LLM per branch-delimited segment, instead of the LLM driving and re-calling the workflow tool to advance. The full design lives in [§8.6.13 in Local Tools & Workflows](05-local-tools-and-workflows.md#8613-engine-driven-execution-inversion-of-control). What the agent loop ([agent/core.py](../../src/botcircuits/agent/core.py)) contributes:

**Control handoff (`run_segment`).** The loop builds a per-turn `tool_context` that carries `run_segment` — a callback bound to `Agent._run_segment`. When a workflow tool's handler runs, it enters the engine (`run_workflow_engine`), which calls `run_segment` once per segment with a constant-size, cache-stable prompt; the workflow-tool call returns only when the engine yields (workflow end → a one-line summary; or a user-interaction pause → the pending question). The model never re-calls the workflow tool to step it forward. `Agent._run_segment` reuses the same tools / skills / MCP wiring as the main loop and intercepts two synthetic concerns: `record_slots` (Tier-1 branch-slot capture) and `human_feedback` (pause).

> **Removed:** the old `_auto_recall_calls` / `_quiet_workflow_finish` machinery (the loop used to synthesize empty-args re-calls when the model "forgot" to advance). The engine owning the loop makes deterministic advancement structural, so that fallback no longer exists. The legacy per-step `run_workflow` path is retained only as a fallback for callers that don't supply `run_segment`.

**Human-feedback pause.** A `question`-type step (or the model's own judgment) routes a question to the user through the `human_feedback` builtin (§8.4). Inside a segment, the engine yields on a `human_feedback` call and the workflow tool returns the question; the loop's `_human_feedback_pause(tool_calls, results)` also still catches a direct `human_feedback` call and ends the turn, surfacing the question as the reply. Either way the user's next `chat()` call is their answer and resumes the run from the parked segment cursor. This is a *terminal-turn* pause — it fits the existing `chat()` / REPL / gateway contract with no new control flow.

**System-prompt reminders.** Before every provider call, the loop runs the active system prompt through `_with_workflow_reminder()`. It appends one of two blocks:

- `[Active workflow]` — if any registered workflow tool reports `session_id != None` (the engine **paused** the workflow waiting on the user), telling the model to treat the user's latest message as the answer and re-call the tool to **resume**.
- `[Available workflows]` — when *no* workflow is active but workflow tools exist, listing every registered workflow with its description and stating that calling the matching tool is **mandatory** as the model's first action whenever the user's request matches one. Without this, long histories cause the model to imitate prior "ask topic, then call tool" turns and skip the tool call entirely.

Note the engine-mode segment prompt itself is **static** (`ENGINE_SYSTEM_PROMPT`) — the reminder above applies to the conversational loop, not to segment calls, which keep a cache-stable prefix. Computing the reminder per-call (not caching on `convo.system`) costs one dict lookup but means the active set can change between turns without an invalidation step. See [§8.6 in Local Tools & Workflows](05-local-tools-and-workflows.md#86-botcircuits-workflows-as-tools).

---
