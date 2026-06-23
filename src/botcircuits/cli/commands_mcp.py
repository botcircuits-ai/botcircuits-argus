"""`botcircuits-cli mcp ...` subcommands.

  mcp list                       Print servers from the targeted file
  mcp add <name> ...             Add (or replace with --replace) a server
  mcp remove <name>              Drop a server by name
  mcp test <name>                Connect, list its tools, disconnect

Mutating commands target one mcp.json file at a time (writing a merged
view would silently flatten user-level entries into the project file).
By default they read/write `.botcircuits/mcp.json` (project shared).
Pass `--user` for `~/.botcircuits/mcp.json` or `--local` for
`.botcircuits/mcp.local.json`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from botcircuits.agent.mcp import LocalMCPManager, MCPServer
from botcircuits.cli.ansi import C, out
from botcircuits.cli.config import (
    ConfigError,
    add_mcp_server,
    list_mcp_servers,
    remove_mcp_server,
)
from botcircuits.cli.settings import (
    ensure_local_gitignored,
    ensure_parent_dir,
    project_local_mcp_path,
    resolve_mcp_target_path,
)


# ---------------------------------------------------------------------------
# Subparser wiring
# ---------------------------------------------------------------------------


def _add_scope_flags(p: argparse.ArgumentParser) -> None:
    """Add `--user` / `--local` so every `mcp` subcommand can pick which
    mcp.json file it operates on. Mutually exclusive — the dispatcher
    enforces this via `resolve_mcp_target_path`."""
    g = p.add_mutually_exclusive_group()
    g.add_argument("--user", action="store_true",
                   help="Target ~/.botcircuits/mcp.json (user-wide)")
    g.add_argument("--local", action="store_true",
                   help="Target .botcircuits/mcp.local.json "
                        "(project, gitignored personal overrides)")


def add_mcp_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Wire the `mcp` command and its own sub-subcommands onto an
    argparse subparser action."""
    mcp = subparsers.add_parser("mcp", help="Manage MCP servers in .botcircuits/mcp.json")
    mcp_subs = mcp.add_subparsers(dest="mcp_cmd", required=True)

    # mcp list
    list_p = mcp_subs.add_parser("list", help="Print servers in the targeted mcp.json file")
    _add_scope_flags(list_p)

    # mcp add <name> [...]
    add_p = mcp_subs.add_parser("add", help="Add an MCP server to the targeted mcp.json file")
    _add_scope_flags(add_p)
    add_p.add_argument("name", help="Server name (used to namespace its tools)")
    add_p.add_argument("--mode", choices=["hosted", "local"], default="local")
    add_p.add_argument("--url", default=None,
                       help="Server URL (hosted, or local with http/sse transport)")
    add_p.add_argument("--authorization-token", default=None,
                       help="Bearer token sent in the Authorization header")
    add_p.add_argument("--transport", choices=["http", "sse", "stdio"], default="http",
                       help="Local transport (ignored when mode=hosted)")
    add_p.add_argument("--command", default=None,
                       help="stdio: executable to launch")
    # nargs='*' lets the user write `--args -y @scope/pkg /tmp` without
    # quoting; values starting with `-` need `--` first or `--args=-y,...`.
    add_p.add_argument("--args", nargs="*", default=None,
                       help="stdio: command args, space-separated "
                            "(use --args=-y,foo if first arg starts with '-')")
    add_p.add_argument("--require-approval", choices=["always", "never"], default="never",
                       help="Hosted-only (OpenAI). When the model must ask before invoking.")
    add_p.add_argument("--allowed-tools", nargs="*", default=None,
                       help="Tool name allow-list, space-separated")
    add_p.add_argument("--replace", action="store_true",
                       help="Overwrite an existing entry with the same name")

    # mcp remove <name>
    rm_p = mcp_subs.add_parser("remove", help="Drop a server by name")
    _add_scope_flags(rm_p)
    rm_p.add_argument("name")

    # mcp test <name>
    test_p = mcp_subs.add_parser("test", help="Connect to a server and list its tools")
    _add_scope_flags(test_p)
    test_p.add_argument("name")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def run_mcp_command(args: argparse.Namespace) -> int:
    """Entry point for `botcircuits-cli mcp ...`. Returns process exit code."""
    try:
        path = resolve_mcp_target_path(
            user=getattr(args, "user", False),
            local=getattr(args, "local", False),
        )

        if args.mcp_cmd == "list":
            return _cmd_list(str(path))
        if args.mcp_cmd == "add":
            return _cmd_add(args, path)
        if args.mcp_cmd == "remove":
            return _cmd_remove(str(path), args.name)
        if args.mcp_cmd == "test":
            return _cmd_test(str(path), args.name)
    except ConfigError as e:
        out(C.red(f"[mcp] {e}"))
        return 2

    out(C.red(f"[mcp] unknown subcommand: {args.mcp_cmd}"))
    return 2


# ---------------------------------------------------------------------------
# Subcommand bodies
# ---------------------------------------------------------------------------


def _cmd_list(path: str) -> int:
    servers = list_mcp_servers(path)
    if not servers:
        out(C.dim(f"(no MCP servers in {path})"))
        return 0
    for s in servers:
        name = s.get("name", "?")
        mode = s.get("mode", "local")
        if mode == "hosted":
            detail = s.get("url", "(no url)")
        else:
            transport = s.get("transport", "http")
            if transport == "stdio":
                detail = f"stdio: {s.get('command', '?')} {' '.join(s.get('args') or [])}"
            else:
                detail = f"{transport}: {s.get('url', '(no url)')}"
        out(f"  {C.cyan(name):<24}  {C.dim(mode):<12}  {detail}")
    return 0


def _cmd_add(args: argparse.Namespace, path) -> int:
    # nargs='*' returns a list when the flag is present (possibly empty)
    # and None when it isn't. A single comma-joined string is also
    # accepted as a convenience for `--args=-y,foo,bar` style.
    arg_list: list[str] = _split_listish(args.args)
    allowed = _split_listish(args.allowed_tools) or None

    server = MCPServer(
        name=args.name,
        mode=args.mode,
        url=args.url,
        authorization_token=args.authorization_token,
        transport=args.transport,
        command=args.command,
        args=arg_list,
        require_approval=args.require_approval,
        allowed_tools=allowed,
    )

    _validate_for_mode(server)
    ensure_parent_dir(path)
    add_mcp_server(str(path), server, replace=args.replace)

    # Creating the local file for the first time? Tell git to ignore it
    # so personal overrides don't slip into a shared commit.
    if path == project_local_mcp_path() and ensure_local_gitignored(path):
        out(C.dim(f"(added {path.name!r} to .gitignore)"))

    out(C.dim(f"(added {server.name!r} to {path})"))
    return 0


def _cmd_remove(path: str, name: str) -> int:
    remove_mcp_server(path, name)
    out(C.dim(f"(removed {name!r} from {path})"))
    return 0


def _cmd_test(path: str, name: str) -> int:
    raw = next((s for s in list_mcp_servers(path) if s.get("name") == name), None)
    if raw is None:
        out(C.red(f"[mcp] no server named {name!r} in {path}"))
        return 2

    server = MCPServer(**raw)
    if server.mode == "hosted":
        out(C.yellow(f"[mcp] {name!r} is mode='hosted'; "
                     f"`test` only supports local servers."))
        return 2

    return asyncio.run(_test_local(server))


async def _test_local(server: MCPServer) -> int:
    mgr = LocalMCPManager([server])
    try:
        await mgr.start()
    except Exception as e:
        out(C.red(f"[mcp] connect failed: {type(e).__name__}: {e}"))
        return 1
    try:
        tools = mgr.tools()
        if not tools:
            out(C.yellow(f"(connected to {server.name!r} but no tools listed)"))
        else:
            out(C.dim(f"connected to {server.name!r} — {len(tools)} tool(s):"))
            for t in tools:
                # Strip the `<server>__` prefix the manager adds.
                short = t.name.split(LocalMCPManager.NAME_SEP, 1)[-1]
                desc = (t.description or "").splitlines()[0][:80]
                out(f"  {C.cyan(short)}  {C.dim(desc)}")
        return 0
    finally:
        await mgr.stop()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_listish(raw) -> list[str]:
    """Accept either nargs='*' output (a list) or a single comma-joined
    string and return a flat list. Returns [] for None / empty input.

    Lets users write either of these:
        --args -y @scope/pkg /tmp
        --args=-y,@scope/pkg,/tmp
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [p for p in raw.split(",") if p]
    out: list[str] = []
    for item in raw:
        out.extend(p for p in item.split(",") if p)
    return out


def _validate_for_mode(s: MCPServer) -> None:
    """Surface obvious misconfigurations at `mcp add` time so the user
    finds out now rather than at chat startup."""
    if s.mode == "hosted":
        if not s.url:
            raise ConfigError("hosted servers require --url")
    else:  # local
        if s.transport == "stdio":
            if not s.command:
                raise ConfigError("local stdio servers require --command")
        elif s.transport in ("http", "sse"):
            if not s.url:
                raise ConfigError(f"local {s.transport} servers require --url")
