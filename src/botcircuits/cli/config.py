"""CLI configuration: built-in defaults, optional JSON config file, CLI flags.

Precedence (highest wins):
    CLI flags  >  --config JSON  >  built-in defaults

The argparse parser uses `default=None` for every flag so we can tell
which flags the user actually passed. Anything still `None` after parsing
falls through to the JSON config; anything missing from JSON falls
through to `DEFAULTS`.

Schema for the JSON file (all keys optional):

    {
      "provider": "anthropic",
      "model": "claude-opus-4-7",
      "system": "You are a helpful assistant.",
      "session": null,
      "stream": true,
      "max_tokens": 4096,
      "max_steps": 10,
      "show_tool_results": false
    }

MCP servers live in a sibling file, `.botcircuits/mcp.json`:

    { "servers": { "fs": { "mode": "local", "transport": "stdio",
                            "command": "npx", "args": [...] } } }

Tool registration stays in code (the security review for things like
`shell_exec` lives there).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from botcircuits.agent.mcp import MCPServer


# ---------------------------------------------------------------------------
# Resolved configuration
# ---------------------------------------------------------------------------


@dataclass
class CLIConfig:
    provider: str = "anthropic"
    model: str | None = None         # None -> provider-specific env/default
    system: str | None = None
    session: str | None = None
    stream: bool = True
    max_tokens: int = 4096
    max_steps: int = 500
    show_tool_results: bool = False
    mcp_servers: list[MCPServer] = field(default_factory=list)

    # Tool-use strategy passed to the Agent. "native" (default) uses the
    # provider's structured function-calling API; "react" describes tools
    # in the system prompt and parses Thought/Action text. See
    # agent/react.py.
    mode: str = "native"

    # Per-tool config overrides keyed by tool name. Each value is either a
    # dict of overrides or null to disable the tool. Threaded into
    # `default_registry()` unchanged — see agent/tools/__init__.py for
    # the resolution rules.
    tools: dict[str, Any] = field(default_factory=dict)

    # Workflow engine knobs. Currently just controls Layer B variable
    # normalization on workflow re-entry. `normalize` defaults to True so
    # indexed workflows get LLM-driven extraction out of the box; set
    # `normalize: false` to keep deterministic type coercion only.
    workflow: dict[str, Any] = field(default_factory=lambda: {"normalize": True})


# Default `provider` honors the existing env var so behavior matches the
# pre-config-file CLI when neither --config nor --provider is passed.
DEFAULTS = CLIConfig(provider=os.getenv("LLM_PROVIDER", "anthropic"))


# ---------------------------------------------------------------------------
# JSON loading
# ---------------------------------------------------------------------------


_ALLOWED_KEYS = {f.name for f in fields(CLIConfig)}
_MCP_FIELDS = {f.name for f in fields(MCPServer)}


class ConfigError(ValueError):
    """Raised when --config points at a file we can't load or that contains
    keys we don't recognize."""


def _parse_workflow(raw: Any) -> dict[str, Any]:
    """Validate the `workflow` config block. Currently only `normalize`
    (bool) is recognized; reject anything else so typos surface."""
    if not isinstance(raw, dict):
        raise ConfigError("`workflow` must be a JSON object")
    allowed = {"normalize"}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(
            f"`workflow` has unknown keys: {sorted(unknown)}. "
            f"Allowed: {sorted(allowed)}"
        )
    out: dict[str, Any] = {}
    if "normalize" in raw:
        if not isinstance(raw["normalize"], bool):
            raise ConfigError("`workflow.normalize` must be a boolean")
        out["normalize"] = raw["normalize"]
    return out


def _parse_tools(raw: Any) -> dict[str, Any]:
    """Validate the `tools` config block. Each entry must be either an
    object (override config), null/false (disable), or omitted (defaults).
    Specific keys inside each override are validated by the tool's own
    `register()` function — the CLI doesn't know what params each tool
    accepts."""
    if not isinstance(raw, dict):
        raise ConfigError("`tools` must be a JSON object keyed by tool name")
    out: dict[str, Any] = {}
    for name, value in raw.items():
        if value is None or value is False:
            out[name] = None
            continue
        if not isinstance(value, dict):
            raise ConfigError(
                f"`tools.{name}` must be an object, null, or false"
            )
        out[name] = value
    return out


def _parse_mcp_servers(raw: Any) -> list[MCPServer]:
    """Validate and convert the `mcp_servers` JSON array into MCPServer
    instances. Each entry must be an object; unknown keys are rejected so
    typos surface immediately.

    Used only for the legacy array form that used to live in settings.json.
    The current home for MCP config is `.botcircuits/mcp.json`, which uses
    `parse_mcp_servers_object`.
    """
    if not isinstance(raw, list):
        raise ConfigError("`mcp_servers` must be a JSON array")
    servers: list[MCPServer] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigError(f"`mcp_servers[{i}]` must be an object")
        if "name" not in entry:
            raise ConfigError(f"`mcp_servers[{i}]` missing required key 'name'")
        unknown = set(entry) - _MCP_FIELDS
        if unknown:
            raise ConfigError(
                f"`mcp_servers[{i}]` ({entry['name']!r}) has unknown keys: "
                f"{sorted(unknown)}. Allowed: {sorted(_MCP_FIELDS)}"
            )
        try:
            servers.append(MCPServer(**entry))
        except TypeError as e:
            raise ConfigError(f"`mcp_servers[{i}]` ({entry['name']!r}): {e}")
    return servers


def parse_mcp_servers_object(raw: Any) -> list[MCPServer]:
    """Validate the new `mcp.json` shape: `{"servers": {name: {...}}}`.

    The server's `name` is the dict key (not duplicated inside the value).
    Unknown keys inside each entry are rejected so typos surface; passing
    a `name` field inside the value is also rejected because the key is
    authoritative.
    """
    if not isinstance(raw, dict):
        raise ConfigError("mcp.json root must be a JSON object")
    servers_obj = raw.get("servers", {})
    if not isinstance(servers_obj, dict):
        raise ConfigError("`servers` must be a JSON object keyed by server name")
    allowed_fields = _MCP_FIELDS - {"name"}
    out: list[MCPServer] = []
    for name, entry in servers_obj.items():
        if not isinstance(name, str) or not name:
            raise ConfigError(
                f"`servers` key must be a non-empty string (got {name!r})"
            )
        if not isinstance(entry, dict):
            raise ConfigError(f"`servers.{name}` must be an object")
        if "name" in entry:
            raise ConfigError(
                f"`servers.{name}` must not include a `name` field "
                f"(the object key is the name)"
            )
        unknown = set(entry) - allowed_fields
        if unknown:
            raise ConfigError(
                f"`servers.{name}` has unknown keys: {sorted(unknown)}. "
                f"Allowed: {sorted(allowed_fields)}"
            )
        try:
            out.append(MCPServer(name=name, **entry))
        except TypeError as e:
            raise ConfigError(f"`servers.{name}`: {e}")
    return out


def load_config_file(path: str) -> dict[str, Any]:
    """Read a JSON config file and return its values as a dict ready for
    `resolve()`. Validates that:
      - the file exists and is valid JSON
      - the top level is an object
      - every key matches a known CLIConfig field
      - `mcp_servers` (if present) is a well-formed array of server entries
    Returns an empty dict if `path` is falsy.
    """
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ConfigError(f"--config file not found: {path}")
    except json.JSONDecodeError as e:
        raise ConfigError(f"--config file is not valid JSON ({path}): {e}")

    if not isinstance(data, dict):
        raise ConfigError(f"--config root must be a JSON object: {path}")

    if "mcp_servers" in data:
        raise ConfigError(
            "`mcp_servers` no longer lives in settings.json. Move it to "
            "`.botcircuits/mcp.json` using the new object shape: "
            '`{"servers": {"<name>": {<fields>}}}`. '
            "See `botcircuits-cli mcp add --help` for help authoring entries."
        )
    # `runtime` is written by `botcircuits init --runtime ...` to record which
    # host agent (claude-code, hermes) a project is pinned to, for the
    # workflow skills to read back. It isn't a CLIConfig knob, so it's not in
    # _ALLOWED_KEYS — but it's a legitimate, intentionally-written field that
    # coexists in the same settings.json, not a typo to reject. Accept it,
    # then drop it before the rest of the dict flows into CLIConfig (which
    # has no such field).
    data = {k: v for k, v in data.items() if k != "runtime"}
    unknown = set(data) - _ALLOWED_KEYS
    if unknown:
        raise ConfigError(
            f"--config has unknown keys: {sorted(unknown)}. "
            f"Allowed: {sorted(_ALLOWED_KEYS)}"
        )
    if "mode" in data:
        if data["mode"] not in ("native", "react"):
            raise ConfigError(
                f"`mode` must be 'native' or 'react' (got {data['mode']!r})"
            )
    if "tools" in data:
        data["tools"] = _parse_tools(data["tools"])
    if "workflow" in data:
        # Merge user overrides over the {"normalize": True} default so a
        # partial block doesn't wipe defaults for keys the user omitted.
        data["workflow"] = {"normalize": True, **_parse_workflow(data["workflow"])}
    return data


# ---------------------------------------------------------------------------
# Layering
# ---------------------------------------------------------------------------


def resolve(file_values: dict[str, Any], cli_values: dict[str, Any]) -> CLIConfig:
    """Merge defaults <- file <- cli. CLI values that are `None` are
    treated as 'not provided' and don't override."""
    merged: dict[str, Any] = asdict(DEFAULTS)
    merged.update(file_values)
    for k, v in cli_values.items():
        if v is not None:
            merged[k] = v
    # asdict() converted any MCPServer dataclasses in DEFAULTS to dicts;
    # rebuild them so callers always get real instances.
    merged["mcp_servers"] = [
        s if isinstance(s, MCPServer) else MCPServer(**s)
        for s in merged.get("mcp_servers", [])
    ]
    return CLIConfig(**merged)


# ---------------------------------------------------------------------------
# Mutation: read/modify/write `.botcircuits/mcp.json` (used by `mcp add` /
# `mcp remove` / `mcp list`). Operates on a single layer file at a time.
# ---------------------------------------------------------------------------


def _read_raw(path: str) -> dict[str, Any]:
    """Read the JSON file as a plain dict for in-place editing. Unlike
    `load_config_file`, this does NOT convert `mcp_servers` to dataclasses
    so we can write the file back unchanged."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"--config file is not valid JSON ({path}): {e}")
    if not isinstance(data, dict):
        raise ConfigError(f"--config root must be a JSON object: {path}")
    return data


def _write_raw(path: str, data: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def _mcp_to_dict(server: MCPServer) -> dict[str, Any]:
    """Serialize an MCPServer's non-default fields for JSON (without the
    `name` — the caller uses it as the dict key). Keeps the file minimal."""
    full = asdict(server)
    full.pop("name", None)
    defaults = asdict(MCPServer(name=server.name))
    defaults.pop("name", None)
    return {k: v for k, v in full.items() if v != defaults.get(k)}


def _read_servers_object(path: str) -> dict[str, Any]:
    """Read the `servers` object out of mcp.json. Returns {} if the file
    doesn't exist; raises ConfigError on a malformed root or `servers`."""
    data = _read_raw(path)
    if not data:
        return {}
    servers = data.get("servers", {})
    if not isinstance(servers, dict):
        raise ConfigError(f"`servers` in {path} must be a JSON object")
    return servers


def add_mcp_server(path: str, server: MCPServer, *, replace: bool = False) -> None:
    """Insert (or replace) an MCP server entry in the mcp.json file at `path`.
    Raises ConfigError if a server with the same name already exists and
    `replace` is False."""
    data = _read_raw(path)
    servers = data.get("servers")
    if servers is None:
        servers = {}
    elif not isinstance(servers, dict):
        raise ConfigError(f"`servers` in {path} must be a JSON object")

    if server.name in servers and not replace:
        raise ConfigError(
            f"MCP server {server.name!r} already exists in {path}. "
            f"Pass --replace to overwrite."
        )
    servers[server.name] = _mcp_to_dict(server)
    data["servers"] = servers
    _write_raw(path, data)


def remove_mcp_server(path: str, name: str) -> None:
    data = _read_raw(path)
    servers = data.get("servers")
    if not isinstance(servers, dict) or name not in servers:
        raise ConfigError(f"No MCP server named {name!r} in {path}.")
    del servers[name]
    data["servers"] = servers
    _write_raw(path, data)


def list_mcp_servers(path: str) -> list[dict[str, Any]]:
    """Return a list-of-dicts view (each with `name` re-inflated) so the
    CLI display code can render entries uniformly."""
    servers = _read_servers_object(path)
    return [{"name": name, **entry} for name, entry in servers.items()
            if isinstance(entry, dict)]
