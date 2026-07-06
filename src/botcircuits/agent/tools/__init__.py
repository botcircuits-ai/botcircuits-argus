"""Local tools subpackage.

Public surface:

    from botcircuits.agent.tools import (
        LocalTool, ToolRegistry, ToolHandler, default_registry,
    )

`default_registry()` accepts a `tools_config` dict so callers (the CLI,
the gateway) can thread per-tool overrides from the layered
`settings.json` files. The CLI reads `cfg.tools` and passes it through
unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from botcircuits.agent.tools.builtins import (
    arithmetic,
    build_workflow,
    edit_file,
    glob_search,
    grep_search,
    human_feedback,
    list_dir,
    memory,
    plan_and_confirm,
    read_file,
    shell,
    shell_status,
    shell_stop,
    time,
    todo_write,
    web_extract,
    web_search,
    write_file,
)
from botcircuits.agent.permissions import PermissionSet
from botcircuits.agent.tools.registry import LocalTool, ToolHandler, ToolRegistry

if TYPE_CHECKING:
    from botcircuits.providers.base import LLMProvider


# Map of builtin tool name -> module exposing register(reg, **config).
# Adding a new builtin is one entry here plus its file under .builtins.
_BUILTINS = {
    "add": arithmetic,
    "now": time,
    "shell_exec": shell,
    "shell_status": shell_status,
    "shell_stop": shell_stop,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "list_dir": list_dir,
    "glob_search": glob_search,
    "grep_search": grep_search,
    "todo_write": todo_write,
    "plan_and_confirm": plan_and_confirm,
    "build_workflow": build_workflow,
    "memory": memory,
    "human_feedback": human_feedback,
    "web_search": web_search,
    "web_extract": web_extract,
}


# Tools that accept a `provider=` kwarg at register time (for LLM-driven
# behavior like the workflow indexer). `default_registry` threads the
# caller's provider into these so the CLI/gateway don't have to register
# them separately after construction.
_PROVIDER_AWARE_TOOLS = ("build_workflow",)


# Tools that are NOT registered eagerly by `default_registry`. They stay
# off the LLM's tool list at startup and are loaded on demand — e.g., the
# CLI's `/workflow ...` slash command lazy-registers `build_workflow` via
# `register_builtin()` only when the user explicitly asks for it. Keeps
# the tool surface (and provider tool-spec payload) small for normal use.
_LAZY_BUILTINS = ("build_workflow",)


def default_registry(
    tools_config: dict[str, dict[str, Any] | None] | None = None,
    *,
    provider: LLMProvider | None = None,
    permissions: dict[str, Any] | None = None,
) -> ToolRegistry:
    """Registry preloaded with the built-in local tools.

    Currently registers `add`, `now`, and `shell_exec`. The shell tool
    ships with no command allow-list; every command is gated only by a
    per-call y/N confirmation (or skipped with `auto=True`).

    `tools_config` is a per-tool override map. Each key is a tool name,
    each value is one of:

      - dict          — config overrides for the tool
      - None or False — disable the tool (skip registration)
      - {} or omitted — register with built-in defaults

    Example:

        default_registry({
            "shell_exec": {
                "timeout_seconds": 60,
                "auto": True,
            },
            "now": None,                 # disable the time tool
        })

    `provider` is forwarded to provider-aware tools (`build_workflow`)
    so their LLM-driven steps (e.g., condition indexing) can run. Tools
    that don't use it ignore the kwarg.

    `permissions` is the `{"allow": [...], "ask": [...], "deny": [...]}`
    block from settings (see `agent/permissions.py`). Rules are evaluated
    on every `ToolRegistry.run()` call, deny -> ask -> allow; a call that
    matches no rule falls back to the tool's own gate unchanged.

    Unknown tool names raise ValueError so typos surface immediately.
    """
    cfg = tools_config or {}
    unknown = set(cfg) - set(_BUILTINS)
    if unknown:
        raise ValueError(
            f"tools config has unknown tool names: {sorted(unknown)}. "
            f"Known: {sorted(_BUILTINS)}"
        )

    reg = ToolRegistry(permissions=PermissionSet.from_config(permissions))
    for name, module in _BUILTINS.items():
        if name in cfg and (cfg[name] is None or cfg[name] is False):
            continue  # explicitly disabled
        # Lazy tools stay off the registry unless the caller put an
        # explicit override block in tools_config for them (which we read
        # as "user wants this on by default again").
        if name in _LAZY_BUILTINS and name not in cfg:
            continue
        overrides = dict(cfg.get(name) or {})
        if name in _PROVIDER_AWARE_TOOLS and provider is not None:
            overrides.setdefault("provider", provider)
        module.register(reg, **overrides)
    return reg


def register_builtin(
    reg: ToolRegistry,
    name: str,
    *,
    provider: LLMProvider | None = None,
    config: dict[str, Any] | None = None,
) -> bool:
    """Lazy-register a single builtin onto an existing registry.

    Used by the CLI's slash-trigger map to load tools that
    `default_registry()` deliberately skipped (see `_LAZY_BUILTINS`).
    Returns True if the tool was newly added, False if it was already
    on the registry (re-registration is a no-op so repeated /workflow
    invocations don't churn the tool list).
    """
    if name not in _BUILTINS:
        raise ValueError(
            f"unknown builtin tool: {name!r}. Known: {sorted(_BUILTINS)}"
        )
    if reg.has(name):
        return False
    overrides = dict(config or {})
    if name in _PROVIDER_AWARE_TOOLS and provider is not None:
        overrides.setdefault("provider", provider)
    _BUILTINS[name].register(reg, **overrides)
    return True


__all__ = [
    "LocalTool",
    "ToolHandler",
    "ToolRegistry",
    "default_registry",
    "register_builtin",
]
