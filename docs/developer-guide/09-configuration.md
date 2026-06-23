# Configuration

[← Implementation Guide index](../../IMPLEMENTATION.md)

---

## 11. Configuration

Config is layered. Highest wins:

```
CLI flags  >  --config JSON  >  built-in defaults (with $LLM_PROVIDER as the provider default)
```

[cli/config.py](../../src/botcircuits/cli/config.py) implements this with two patterns:

### 11.1 Sentinel-None for CLI flags
Every CLI flag uses `default=None`. After parsing, `None` means "user didn't pass this," and only non-`None` values override the JSON file. This is the standard idiom for layered config in argparse — without it you can't distinguish "user explicitly typed `--max-tokens 4096`" from "argparse filled in the default."

### 11.2 `CLIConfig` dataclass
The resolved config is a single dataclass:

```python
@dataclass
class CLIConfig:
    provider: str = "anthropic"
    model: str | None = None
    system: str | None = None
    session: str | None = None
    stream: bool = True
    max_tokens: int = 4096
    max_steps: int = 10
    show_tool_results: bool = False
    mcp_servers: list[MCPServer] = field(default_factory=list)
    tools: dict[str, Any] = field(default_factory=dict)
    workflow: dict[str, Any] = field(default_factory=lambda: {"normalize": True})
```

`load_config_file(path)` reads `settings.json`, validates keys, rejects an `mcp_servers` block with a migration hint, converts `tools` into the per-tool dispatch dict, and `workflow` into a validated `{normalize: bool}` (unknown keys inside `workflow` are rejected). `resolve(file_values, cli_values)` does the merge. The `workflow` block has its own merge step that layers user overrides over the `{"normalize": True}` default, so a partial block doesn't wipe defaults for keys the user omitted.

MCP entries are loaded separately by `load_mcp_layers` from `mcp.json` files using `parse_mcp_servers_object`, which expects `{"servers": {<name>: {<fields>}}}`. `load_layered_settings` calls both loaders and injects the merged MCP server list onto the returned dict as `mcp_servers` before `resolve()` sees it — so the resolved `CLIConfig` shape is unchanged.

### 11.3 Mutation helpers
`add_mcp_server`, `remove_mcp_server`, `list_mcp_servers` are read-modify-write helpers used by the `mcp` CLI subcommands. They target one `mcp.json` file at a time and strip default-valued fields on write so the JSON stays minimal — adding a hosted server with no auth shows up as just `{"url": "..."}` under its server-name key (mode `hosted` is the default and is omitted; the name is the dict key, not a field). When you copy an entry by hand, omitting fields just means "use the default."

### 11.4 .env loading
[botcircuits/__init__.py](../../src/botcircuits/__init__.py) calls `python-dotenv`'s `load_dotenv()` at import time. Resolution: `BOTCIRCUITS_ENV_FILE` if set, else the nearest `.env` walking up from the cwd. **Existing process env always wins** so CI/production environments aren't overridden.

This runs unconditionally on package import — every entry point (CLI, gateway, library use) picks up `.env` without each one needing its own bootstrap. The `noqa` import in `main.py` is there only to trigger the side effect for that bare entry point.

### 11.5 Why JSON, not YAML/TOML?
Three reasons:
1. **Stdlib only.** Python ships `json`. `pyyaml` is an extra dep with a CVE history; `tomllib` is read-only.
2. **Round-trippable.** The `mcp add` / `mcp remove` subcommands need to mutate the file. JSON's lack of comments is annoying but the writer can serialize cleanly without trying to preserve hand-formatting. YAML round-tripping eats whitespace and reorders keys.
3. **Schemaable.** A JSON Schema for `settings.json` is straightforward if we ever want IDE completion.

The trade-off is no comments and no trailing commas. Worth it.

---
