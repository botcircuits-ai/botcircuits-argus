"""Layered settings discovery for the CLI and gateway.

BotCircuits Agent reads settings from up to four layers, lowest to
highest precedence:

    1. ~/.botcircuits/settings.json            (user, applies everywhere)
    2. <cwd>/.botcircuits/settings.json        (project, shared via VCS)
    3. <cwd>/.botcircuits/settings.local.json  (project, gitignored)
    4. explicit --config / BOTCIRCUITS_CONFIG  (final override)

MCP servers live in sibling files at each tier and are layered with the
same precedence (user → project shared → project local):

    1. ~/.botcircuits/mcp.json
    2. <cwd>/.botcircuits/mcp.json
    3. <cwd>/.botcircuits/mcp.local.json

Each settings layer uses the JSON schema accepted by
`cli.config.load_config_file`. Layers are deep-merged by key:

    - Scalar values: later layer wins.
    - `tools` (object keyed by tool name): merged per tool. A later
      layer that sets `tools.X = null` disables tool X even if an
      earlier layer configured it.

MCP layers are loaded separately (via `parse_mcp_servers_object`),
merged by server name across tiers (later tier wins on collision), and
attached to the merged settings dict as `mcp_servers` before resolve().

Validation runs once per layer file so a typo surfaces with that file's
path in the error message.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from botcircuits.cli.config import ConfigError, load_config_file, parse_mcp_servers_object
from botcircuits.agent.mcp import MCPServer


SETTINGS_DIR = ".botcircuits"
SHARED_FILE = "settings.json"
LOCAL_FILE = "settings.local.json"
MCP_SHARED_FILE = "mcp.json"
MCP_LOCAL_FILE = "mcp.local.json"


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------


def user_settings_path() -> Path:
    """`~/.botcircuits/settings.json` — applies to all projects."""
    return Path.home() / SETTINGS_DIR / SHARED_FILE


def project_shared_settings_path(cwd: Path | None = None) -> Path:
    """`<cwd>/.botcircuits/settings.json` — checked into VCS."""
    return (cwd or Path.cwd()) / SETTINGS_DIR / SHARED_FILE


def project_local_settings_path(cwd: Path | None = None) -> Path:
    """`<cwd>/.botcircuits/settings.local.json` — gitignored personal overrides."""
    return (cwd or Path.cwd()) / SETTINGS_DIR / LOCAL_FILE


def user_mcp_path() -> Path:
    """`~/.botcircuits/mcp.json` — MCP servers shared across all projects."""
    return Path.home() / SETTINGS_DIR / MCP_SHARED_FILE


def project_shared_mcp_path(cwd: Path | None = None) -> Path:
    """`<cwd>/.botcircuits/mcp.json` — project MCP servers (checked into VCS)."""
    return (cwd or Path.cwd()) / SETTINGS_DIR / MCP_SHARED_FILE


def project_local_mcp_path(cwd: Path | None = None) -> Path:
    """`<cwd>/.botcircuits/mcp.local.json` — gitignored MCP overrides."""
    return (cwd or Path.cwd()) / SETTINGS_DIR / MCP_LOCAL_FILE


def discover_mcp_layers(cwd: Path | None = None) -> list[Path]:
    """MCP layer files that exist on disk, lowest precedence first.

    The `--config` / `BOTCIRCUITS_CONFIG` override is intentionally not
    a candidate here: that path targets `settings.json` (full CLI config),
    not the MCP-specific file. Users wanting to override MCP from an
    arbitrary path can edit one of the layered mcp.json files directly.
    """
    candidates = [
        user_mcp_path(),
        project_shared_mcp_path(cwd),
        project_local_mcp_path(cwd),
    ]
    return [p for p in candidates if p.is_file()]


def discover_layers(
    cwd: Path | None = None,
    explicit: str | os.PathLike[str] | None = None,
) -> list[Path]:
    """Return the ordered list of settings files that exist, lowest
    precedence first. `explicit` (from --config or BOTCIRCUITS_CONFIG)
    is always appended last when set, so it wins.

    Missing files are silently skipped; this is the normal case (most
    projects won't have all four layers)."""
    candidates: list[Path] = [
        user_settings_path(),
        project_shared_settings_path(cwd),
        project_local_settings_path(cwd),
    ]
    if explicit:
        candidates.append(Path(explicit))
    return [p for p in candidates if p.is_file()]


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------


def _merge_mcp_servers(base: list[MCPServer], overlay: list[MCPServer]) -> list[MCPServer]:
    """Merge overlay list onto base list by `name`. Overlay entries with a
    matching name replace the base entry; new names are appended in
    overlay order."""
    by_name: dict[str, MCPServer] = {}
    order: list[str] = []
    for entry in list(base) + list(overlay):
        if entry.name not in by_name:
            order.append(entry.name)
        by_name[entry.name] = entry
    return [by_name[n] for n in order]


def _merge_tools(base: dict, overlay: dict) -> dict:
    """Merge overlay tools dict onto base. Per-tool: if overlay sets
    None (disable), that wins. Otherwise dict values are shallow-merged
    so partial overrides work (e.g. local file flips just `auto: true`)."""
    merged: dict[str, Any] = dict(base)
    for name, value in overlay.items():
        if value is None:
            merged[name] = None
            continue
        existing = merged.get(name)
        if isinstance(existing, dict) and isinstance(value, dict):
            combined = dict(existing)
            combined.update(value)
            merged[name] = combined
        else:
            merged[name] = value
    return merged


def _merge_permissions(base: dict, overlay: dict) -> dict:
    """Concatenate allow/ask/deny rule lists across layers instead of
    replacing them — so a user-level deny rule still applies even when a
    project layer also sets `permissions`, matching the additive way
    Claude Code layers permission rules across settings scopes."""
    merged: dict[str, Any] = {
        "allow": list(base.get("allow", [])),
        "ask": list(base.get("ask", [])),
        "deny": list(base.get("deny", [])),
    }
    for key in ("allow", "ask", "deny"):
        merged[key].extend(overlay.get(key, []))
    return merged


def merge_layers(layers: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce a list of already-validated settings layer dicts (lowest
    precedence first) into a single dict ready for `cli.config.resolve`.

    MCP servers are NOT layered here — they live in `mcp.json` files and
    are merged separately via `load_mcp_layers`.
    """
    out: dict[str, Any] = {}
    for layer in layers:
        for key, value in layer.items():
            if key == "tools":
                out[key] = _merge_tools(out.get(key, {}), value)
            elif key == "permissions":
                out[key] = _merge_permissions(out.get(key, {}), value)
            else:
                out[key] = value
    return out


def load_mcp_layers(cwd: Path | None = None) -> tuple[list[MCPServer], list[Path]]:
    """Discover, validate, and merge all applicable `mcp.json` layers.

    Each file uses the object-keyed shape:
        {"servers": {"<name>": {<fields without name>}}}

    Returns `(merged_servers, used_paths)`. Servers are merged by name
    with later tiers (project shared > user, project local > project
    shared) overriding earlier ones.
    """
    import json

    paths = discover_mcp_layers(cwd=cwd)
    merged: list[MCPServer] = []
    for p in paths:
        try:
            with p.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigError(f"{p}: not valid JSON ({e})") from e
        except OSError as e:
            raise ConfigError(f"{p}: {e}") from e
        try:
            layer_servers = parse_mcp_servers_object(data)
        except ConfigError as e:
            raise ConfigError(f"{p}: {e}") from e
        merged = _merge_mcp_servers(merged, layer_servers)
    return merged, paths


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def resolve_target_path(
    *,
    explicit: str | os.PathLike[str] | None = None,
    user: bool = False,
    local: bool = False,
    cwd: Path | None = None,
) -> Path:
    """Pick which single settings file a mutating command should write to.
    Exactly one of `explicit`, `user`, or `local` may be set; otherwise
    the default is the project's shared file.

    Mutations target one file — never the merged view — because writing
    back a merged blob would silently inline a `~/`-level entry into the
    project file (or vice versa).
    """
    chosen = sum(bool(x) for x in (explicit, user, local))
    if chosen > 1:
        raise ConfigError(
            "pass at most one of --config / --user / --local "
            "(they pick different settings files)"
        )
    if explicit:
        return Path(explicit)
    if user:
        return user_settings_path()
    if local:
        return project_local_settings_path(cwd)
    return project_shared_settings_path(cwd)


def resolve_mcp_target_path(
    *,
    user: bool = False,
    local: bool = False,
    cwd: Path | None = None,
) -> Path:
    """Pick which mcp.json file a mutating command (`mcp add` / `mcp
    remove`) should write to. Exactly one of `user` or `local` may be
    set; otherwise the default is the project's shared `mcp.json`.

    No `--config` override here — that flag points at a settings.json,
    not the MCP file.
    """
    if user and local:
        raise ConfigError(
            "pass at most one of --user / --local "
            "(they pick different mcp.json files)"
        )
    if user:
        return user_mcp_path()
    if local:
        return project_local_mcp_path(cwd)
    return project_shared_mcp_path(cwd)


def ensure_parent_dir(path: Path) -> None:
    """Make sure the `.botcircuits/` directory exists before writing to
    a file under it. Idempotent."""
    path.parent.mkdir(parents=True, exist_ok=True)


def ensure_local_gitignored(local_path: Path) -> bool:
    """When creating a `.botcircuits/*.local.json` file (settings or mcp)
    for the first time, append it to the nearest `.gitignore` so personal
    overrides don't accidentally get committed. Returns True iff we added
    an entry.

    No-op when:
      - the local file is not under a git repo, or
      - it's already covered by an existing pattern (we look for an
        exact-line match against the file's repo-relative path).
    """
    # Walk up from the local file until we find a `.git` dir (repo root).
    repo_root: Path | None = None
    for parent in [local_path.parent, *local_path.parent.parents]:
        if (parent / ".git").exists():
            repo_root = parent
            break
    if repo_root is None:
        return False

    gitignore = repo_root / ".gitignore"
    rel = local_path.relative_to(repo_root).as_posix()
    if gitignore.exists():
        existing = gitignore.read_text(encoding="utf-8").splitlines()
        if any(line.strip() == rel for line in existing):
            return False
        sep = "" if gitignore.read_text(encoding="utf-8").endswith("\n") else "\n"
        with gitignore.open("a", encoding="utf-8") as f:
            f.write(f"{sep}\n# BotCircuits local settings (do not commit)\n{rel}\n")
    else:
        gitignore.write_text(
            f"# BotCircuits local settings (do not commit)\n{rel}\n",
            encoding="utf-8",
        )
    return True


def load_layered_settings(
    cwd: Path | None = None,
    explicit: str | os.PathLike[str] | None = None,
) -> tuple[dict[str, Any], list[Path]]:
    """Discover, validate, and merge all applicable settings + mcp layers.

    Returns `(merged_values, used_paths)` where `used_paths` is the
    ordered list of files actually read (settings layers first, then
    mcp.json layers). The merged dict carries `mcp_servers` injected
    from the mcp.json layers so downstream `resolve()` sees the same
    shape it always has. Each file is validated independently, so a typo
    surfaces with that file's path in the error message.
    """
    settings_paths = discover_layers(cwd=cwd, explicit=explicit)
    layers: list[dict[str, Any]] = []
    for p in settings_paths:
        try:
            layers.append(load_config_file(str(p)))
        except ConfigError as e:
            raise ConfigError(f"{p}: {e}") from e
    merged = merge_layers(layers)

    mcp_servers, mcp_paths = load_mcp_layers(cwd=cwd)
    if mcp_servers:
        merged["mcp_servers"] = mcp_servers

    return merged, settings_paths + mcp_paths
