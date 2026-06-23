# Capability Matrix, Extension Points & Trade-offs

[← Implementation Guide index](../../IMPLEMENTATION.md)

---

## 15. Capability Matrix

| Capability                       | Anthropic | OpenAI    | Gemini       |
|----------------------------------|-----------|-----------|--------------|
| Local Python tools               | ✅        | ✅        | ✅            |
| Hosted MCP (provider executes)   | ✅        | ✅        | ❌ (auto-promoted to local) |
| Local MCP (we execute)           | ✅        | ✅        | ✅            |
| Skills (named bundles)           | ✅        | n/a       | n/a          |
| Hosted code execution            | ✅        | ✅ (`code_interpreter`) | ✅ (`code_execution`) |
| Streaming                        | ✅        | ✅        | ✅            |
| Async                            | ✅        | ✅        | ✅            |

---

## 16. Extension Points

### Add a new provider
Subclass `LLMProvider`, implement `complete` and `stream`, drop the file in [providers/](../../src/botcircuits/providers/), add it to `providers/__init__.py`. Look at any existing provider for the shape — they're each ~100 lines. Most of the work is translating `Message` blocks to/from the vendor's wire format.

### Add a new transport for local MCP
[`LocalMCPManager._open_session`](../../src/botcircuits/agent/mcp.py) switches on `cfg.transport`. Add a new branch for the new transport (e.g. websocket), keep the same `read, write` interface.

### Add a built-in tool with config
See §8.3. One file in `builtins/`, one entry in `_BUILTINS`. JSON config flows in automatically.

### Add a tool that needs context (DB, user info, …)
Capture it in a closure when registering:

```python
def make_lookup_user(db):
    async def handler(args: dict) -> dict:
        return await db.users.find_one(args["user_id"])
    return handler

reg.register(LocalTool(name="lookup_user", description="...",
                       input_schema=..., handler=make_lookup_user(db)))
```

Do this in your own bootstrap code on top of `default_registry()`. Don't put DB connections into the JSON config.

### Add a tool that needs the surrounding conversation (last assistant text, etc.)
Take a second `context` arg on the handler. The registry inspects the signature and only passes the dict to handlers that accept it, so opting in is one keyword:

```python
async def handler(args: dict, context: dict | None = None) -> str:
    ctx = context or {}
    last = ctx.get("last_assistant_message", "")
    ...
```

The agent loop fills `context` per turn with `{last_assistant_message, session_id}`; add new keys in [agent/core.py](../../src/botcircuits/agent/core.py)'s `tool_context` dict and they become available to every context-aware handler with no further plumbing.

### Add streaming token usage
`LLMResponse.raw` carries each provider's native response object. Read `usage` off it inside the provider, attach to `LLMResponse` as a new field, then surface it as a new `StreamEvent` type. The Agent loop won't change.

### Persist conversations
Subclass `ConversationStore`. Override `get_or_create` and `reset`. Keep the `asyncio.Lock` on each `Conversation` (don't try to make it cross-process; serialize at the request layer instead). The memory snapshot is injected by the base class inside `get_or_create`; subclass implementations should preserve that call (or re-implement it) so persisted sessions still pick up `MEMORY.md` / `USER.md` at session creation.

### Add a filesystem skill
Drop a directory under `./skills/` (or `./.botcircuits/skills/`) containing a `SKILL.md` with `name` / `description` frontmatter and a markdown body. The agent picks it up on `start()` and exposes it as a tool named after the directory. See §8b for the SKILL.md format and dynamic substitution rules. No code change needed.

### Add or rename a persistent-memory target
Edit `_TARGETS` and `_file_for()` in [agent/memory.py](../../src/botcircuits/agent/memory.py), add a corresponding cap in `_cap_for()`, and update the `memory` tool's `enum` for `target` in [agent/tools/builtins/memory.py](../../src/botcircuits/agent/tools/builtins/memory.py). `render_for_system_prompt` will need a matching `<...>` wrapper. Keep the caps tight — the snapshot ships in every prompt.

### Add a new messaging channel
Subclass `Channel` ([gateway/channels/base.py](../../src/botcircuits/gateway/channels/base.py)), set `name`, implement `send(OutboundMessage)`, and optionally `routes()` (for HTTP-driven inbound) or `start()`/`stop()` (for polling adapters). Convert platform-native payloads into `InboundMessage` and hand them to `gateway.dispatch(...)`. Wire it into `messaging_config.load()` and `app.py`'s lifespan beside the existing channels. The agent doesn't need to change — it's just another session.

### Tool-search for many tools
When you have dozens of MCP tools, the registry passes all of them to the model on every turn — wasteful. Add a `tool_search` LocalTool that takes a query and returns the most relevant tool names. Pair with an `allowed_tools` filter to limit what's sent. Anthropic's hosted MCP supports `defer_loading`; OpenAI supports `allowed_tools` directly on hosted MCP entries.

---

## 17. Design Trade-offs Worth Naming

**Provider-specific niceties hidden by default.** Anthropic's prompt caching, OpenAI's structured outputs, Gemini's grounding — none are exposed in the unified interface. Add them as optional kwargs on specific provider constructors when you need them; they won't be portable, and that's fine.

**No retry / backoff in the core.** Hot loops over flaky providers belong in middleware, not the agent. Wrap the provider with whatever retry library you like.

**Tool name namespacing for local MCP.** `server__tool` rather than just `tool`. Solves disambiguation when two MCP servers expose the same tool name; small cost in prompt readability.

**Skills abstraction is honest, not symmetric.** OpenAI and Gemini don't have named skill bundles, so `SkillSpec.skill_id` is meaningful only on Anthropic. Better than pretending otherwise.

**`shell_exec` ships enabled.** The y/N confirmation per call, timeout, and output cap make it safe by construction — the user gates every call — and useful out of the box, with no policy guessing about which commands are "safe" and no false sense of sandboxing from a fixed cwd. Anyone who needs unattended runs flips `auto: true`; anyone who wants nothing has `"shell_exec": null`. Non-tty contexts auto-engage auto mode so the gateway works without ceremony.

**Tool parameters in JSON, tool implementations in code.** The JSON config can override `shell_exec`'s timeout/output/auto because those are policy. It cannot register a brand-new tool because that would mean either loading code paths from disk (security review goes out the window) or hand-writing JSON-schema-as-code (worse than just writing Python). Code stays in code, parameters stay in config.

**In-memory store as default.** Persistence is the user's call. The Agent is stateless across sessions, the store is a swappable interface, and adding Redis/Postgres is a small subclass.

**One JSON schema, two consumers.** The CLI and the gateway share `cli/config.py`. If the gateway grows its own config concerns we'll lift the module out of `cli/` rather than duplicate the schema.

---

## 18. Suggested Next Improvements

- **Token-usage events** in the stream; aggregate per-session.
- **JSON Schema for `settings.json`** so editors offer completion and red-squiggle on typos before runtime.
- **Tool approval gates** for local MCP write tools (confirm before delete/move). Could reuse OpenAI's `require_approval` semantics for parity.
- **Structured logging** of provider requests / responses with PII redaction.
- **Tests** — provider mocks for the Agent loop, plus integration tests against real APIs gated by env vars. Extra value in covering the layered config (`config.resolve` precedence) and the `mcp` CLI roundtrips.
- **Lift `cli/config.py` to `botcircuits/config.py`** once the gateway's config needs diverge from the CLI's.
- **`botcircuits-cli tool test <name> --argv ...`** — symmetric with `mcp test` for verifying tool config without launching a chat.
- **Variable normalization caching.** If the LLM retries with identical args (model jitter, network retry), Layer B currently re-runs. Hashing `(workflow_name, state_id, args, last_assistant_message_hash)` and short-circuiting on cache hit would cut latency for retries without changing behavior.
- **`allowedValues` on indexed variables.** Lets Layer A snap case-variant strings (`"Delivered"` → `"delivered"`) to a known enum without the risk of corrupting case-sensitive ids elsewhere.
- **Persistent cron job history.** Cron jobs currently share the in-memory `ConversationStore`, so a daily-summary job loses its context on restart. A persistent `ConversationStore` (Redis/SQLite) would let cron jobs build long-running state.
- **More inbound channels.** Telegram, Discord, SMS (Twilio), Email (IMAP poll). Each is one file under `gateway/channels/` plus a config block — the agent loop doesn't change.
- **Streaming replies through channels.** The message gateway currently calls `agent.chat`, not `agent.chat_stream`. Slack and WhatsApp both support edit-in-place; we could stream `text_delta` events into a single message and edit it as the agent thinks.
- **Richer normalizer context.** Currently Layer B sees `last_assistant_message` and `last_user_message`. Surfacing recent tool results or a longer message tail would let it extract variables from upstream tool outputs (a `get_order` blob, say) without requiring the LLM to copy them by hand. Cost: more tokens per normalization call, more cache invalidation surface.
- **Mid-session memory refresh.** Today the memory snapshot is frozen at session start to keep the prompt cache warm. A `/memory reload` slash command (or a `force_refresh: true` arg on the `memory` tool) could surface fresh content to the live session for cases where the user explicitly wants the trade-off (cache miss for immediate effect).
- **JSON Schema for SKILL.md frontmatter.** With multiple optional keys (`allowed-tools`, `disable-model-invocation`, future ones), a schema would let editors red-squiggle typos before the skill silently fails to load.
