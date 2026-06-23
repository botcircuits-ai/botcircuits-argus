# MCP: Hosted vs Local

[← Implementation Guide index](../../IMPLEMENTATION.md)

---

## 7. MCP: Hosted vs Local

[agent/mcp.py](../../src/botcircuits/agent/mcp.py). The single `MCPServer` config has a `mode` field:

```python
@dataclass
class MCPServer:
    name: str
    mode: Literal["hosted", "local"] = "hosted"
    url: str | None = None
    transport: Literal["http", "sse", "stdio"] = "http"
    command: str | None = None      # local stdio
    args: list[str] = []
    authorization_token: str | None = None
    allowed_tools: list[str] | None = None
    require_approval: Literal["always", "never"] = "never"
```

### 7.1 Hosted mode
The Agent collects all `mode="hosted"` servers and passes them through to `provider.complete(..., hosted_mcp=...)`. The provider wires them into its own MCP parameter. The provider's runtime does the entire round trip — list tools, call tools, return results — server-side.

### 7.2 Local mode — the `LocalMCPManager`
This is the part that makes MCP usable on every provider, including ones without hosted support.

```python
class LocalMCPManager:
    async def start(self):
        # 1. Open each MCP session inside an AsyncExitStack
        # 2. Call list_tools() on each
        # 3. Wrap each MCP tool as a LocalTool with a namespaced name
        #    "{server_name}__{tool_name}"
```

Why this works: from the model's perspective, an MCP tool and a Python function tool are identical — both have a name, a description, and a JSON schema. By exposing MCP tools **as** `LocalTool` instances, every provider handles them through the same code path it already uses for built-in and user tools. No provider needs to know MCP exists for local mode.

The handler returned by `_make_handler` is a closure that:
1. Takes the model's argument dict.
2. Awaits `session.call_tool(name, args)`.
3. Flattens the MCP result's content parts to text.
4. Raises if the MCP server reports `isError`, which the registry turns into an `is_error: True` tool result.

A single `asyncio.Lock` serializes calls into the manager because the MCP `ClientSession` isn't documented as concurrent-safe. Drop the lock if your servers handle concurrency.

### 7.3 Auto-promotion
When `provider.supports_hosted_mcp()` is `False` and a config has `mode="hosted"`, the Agent flips it to `"local"` at construction time and prints an info line. This means a single config works across providers — Gemini just runs everything locally.

### 7.4 Declaring servers in JSON
MCP servers live in `.botcircuits/mcp.json`, separate from `settings.json`. Each file's on-disk shape is `{"servers": {"<name>": {<fields without name>}}}` — the dict key is the server name. The layered loader ([cli/settings.py](../../src/botcircuits/cli/settings.py)) reads three tiers (`~/.botcircuits/mcp.json`, `.botcircuits/mcp.json`, `.botcircuits/mcp.local.json`), merges by name with later tiers winning, and injects the merged list into the resolved settings dict as `mcp_servers`. Putting an `mcp_servers` block in `settings.json` is rejected at parse time with a pointer to `mcp.json`. The CLI's `mcp add/remove/list/test` subcommands ([cli/commands_mcp.py](../../src/botcircuits/cli/commands_mcp.py)) operate on one layer at a time (default: project shared; `--user` / `--local` pick others) — writing back to a merged view would silently inline user-level entries into the project file. Server entries written by the CLI strip default-valued fields so the file stays minimal.

The merged list **replaces** any servers passed to `Agent(mcp_servers=...)` in code. The CLI and gateway both read it and pass the resolved list straight through.

---
