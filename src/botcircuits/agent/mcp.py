"""MCPServer config and the local-mode MCP session manager.

Two configurations:
  - mode="hosted": the provider runs the MCP server-side (Anthropic / OpenAI).
  - mode="local":  we open the MCP session in-process via stdio / HTTP / SSE
                   and expose its tools as `LocalTool`s. Works on every
                   provider, including Gemini.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Literal

from botcircuits.agent.tools import LocalTool, ToolHandler

MCPMode = Literal["hosted", "local"]
MCPTransport = Literal["http", "sse", "stdio"]


@dataclass
class MCPServer:
    """One MCP server config.

    mode='local'  -> we open the MCP session in this process, list its tools,
                     and expose them to the model as local function tools.
                     Works with EVERY provider. This is the default — a bad
                     URL/token fails at agent boot instead of poisoning every
                     chat turn at the provider layer.
    mode='hosted' -> the provider connects to it server-side. Requires url.
                     Only Anthropic and OpenAI support this today.

    For local stdio: transport='stdio', command=..., args=[...].
    For local HTTP/SSE: transport='http' or 'sse' and url.
    For hosted: transport is ignored; only url matters.
    """
    name: str
    mode: MCPMode = "local"
    url: str | None = None
    authorization_token: str | None = None

    # local-only fields
    transport: MCPTransport = "http"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None

    # hosted-only knob (OpenAI)
    require_approval: Literal["always", "never"] = "never"

    # tool-name filter applied AFTER discovery; lets you trim a 50-tool
    # server down to the few you actually want exposed to the model.
    allowed_tools: list[str] | None = None


class LocalMCPManager:
    """Opens local MCP sessions, lists their tools, and exposes them as
    LocalTool objects with async handlers. Reused by every provider.

    Tool names are namespaced: '<server_name>__<tool_name>'. That keeps the
    model from confusing tools across servers and keeps registry lookups
    unambiguous when two servers expose the same tool name.
    """

    NAME_SEP = "__"

    def __init__(self, servers: list[MCPServer]):
        self._configs = [s for s in servers if s.mode == "local"]
        self._sessions: dict[str, Any] = {}     # server_name -> ClientSession
        self._stack: AsyncExitStack | None = None
        self._tools: list[LocalTool] = []
        # MCP ClientSession isn't documented as concurrent-safe; serialize
        # call_tool per manager. Cheap; remove if your servers are known-safe.
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "LocalMCPManager":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    async def start(self) -> None:
        if self._stack or not self._configs:
            return
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        for cfg in self._configs:
            session = await self._open_session(cfg)
            self._sessions[cfg.name] = session
            await self._discover_tools(cfg, session)

    async def stop(self) -> None:
        if self._stack:
            await self._stack.__aexit__(None, None, None)
            self._stack = None
        self._sessions.clear()
        self._tools.clear()

    def tools(self) -> list[LocalTool]:
        return list(self._tools)

    # -- internal -----------------------------------------------------------

    async def _open_session(self, cfg: MCPServer):
        from mcp import ClientSession
        if cfg.transport == "stdio":
            if not cfg.command:
                raise ValueError(f"MCP '{cfg.name}': stdio needs command")
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client
            params = StdioServerParameters(command=cfg.command,
                                           args=cfg.args, env=cfg.env)
            read, write = await self._stack.enter_async_context(stdio_client(params))
        elif cfg.transport in ("http", "sse"):
            if not cfg.url:
                raise ValueError(f"MCP '{cfg.name}': {cfg.transport} needs url")
            headers = ({"Authorization": f"Bearer {cfg.authorization_token}"}
                       if cfg.authorization_token else None)
            if cfg.transport == "http":
                from mcp.client.streamable_http import streamablehttp_client
                read, write, _ = await self._stack.enter_async_context(
                    streamablehttp_client(cfg.url, headers=headers))
            else:
                from mcp.client.sse import sse_client
                read, write = await self._stack.enter_async_context(
                    sse_client(cfg.url, headers=headers))
        else:
            raise ValueError(f"Unknown MCP transport: {cfg.transport}")

        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    async def _discover_tools(self, cfg: MCPServer, session) -> None:
        listing = await session.list_tools()
        for t in listing.tools:
            if cfg.allowed_tools and t.name not in cfg.allowed_tools:
                continue
            qualified = f"{cfg.name}{self.NAME_SEP}{t.name}"
            self._tools.append(LocalTool(
                name=qualified,
                description=t.description or f"MCP tool {t.name} from {cfg.name}",
                input_schema=t.inputSchema or {"type": "object", "properties": {}},
                handler=self._make_handler(cfg.name, t.name),
            ))

    def _make_handler(self, server_name: str, tool_name: str) -> ToolHandler:
        async def handler(args: dict) -> str:
            session = self._sessions[server_name]
            async with self._lock:
                result = await session.call_tool(tool_name, args or {})
            parts: list[str] = []
            for c in (result.content or []):
                if getattr(c, "type", None) == "text":
                    parts.append(c.text)
                else:
                    parts.append(json.dumps({"type": getattr(c, "type", "unknown")}))
            text = "\n".join(parts) if parts else ""
            if getattr(result, "isError", False):
                raise RuntimeError(text or "MCP tool returned error")
            return text
        return handler
