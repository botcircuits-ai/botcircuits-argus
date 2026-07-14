# MCP (`agent/mcp.py`)

```
 MCPServer(name, mode, transport)
        │
        ├── mode="hosted" ──► passed through on the provider call —
        │                     the VENDOR runs the server (Anthropic/OpenAI)
        │        │
        │        └── provider lacks hosted MCP? auto-promoted to local ─┐
        │                                                               │
        └── mode="local" ◄──────────────────────────────────────────────┘
                 │
                 ▼
        LocalMCPManager opens the session in-process
              stdio │ http │ sse            (Agent.start / aclose)
                 │
                 ▼
        each MCP tool ──► LocalTool ──► agent's registry
                          (same permission gate as builtins)
```

`MCPServer` config + the local-mode session manager. Two modes:

- `hosted` — the provider runs the MCP server-side (Anthropic / OpenAI).
  Passed through on the provider call as `hosted_mcp`.
- `local` — `LocalMCPManager` opens the session in-process (stdio / HTTP /
  SSE) and exposes each MCP tool as a `LocalTool` on the agent's registry.
  Works on every provider, including Gemini.

One config works everywhere: on providers without hosted-MCP support, hosted
servers are auto-promoted to local at `Agent` construction.

Sessions open in `Agent.start()` and close in `aclose()`. MCP tools go
through the same registry dispatch as builtins, so permission rules apply to
them too.
