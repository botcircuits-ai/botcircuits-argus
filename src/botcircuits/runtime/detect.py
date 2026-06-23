"""Select which agent runtime hosts a workflow run.

Resolution order (first hit wins):

  1. **Explicit config** — `$BOTCIRCUITS_RUNTIME`, then the `runtime` key in
     the layered `.botcircuits/settings.json`. Deterministic and always
     overrides detection; this is how a host pins itself.
  2. **Auto-detect** — environment markers the host CLI sets (claude-code
     exports `CLAUDECODE` / `CLAUDE_CODE_*`; codex `CODEX_*`; …), then a
     `which` probe for a known CLI binary on PATH.
  3. **Default** — `native`, the in-process loop. Keeps today's behavior when
     nothing else matches.

The per-provider argv TEMPLATE (and default binary name) lives in
`_REGISTRY` below so adding a CLI provider that emits the same JSON contract
is config/registry-only, no new code.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Callable

from botcircuits.runtime.base import AgentRuntimeProvider, RuntimeConfig


NATIVE = "native"
SELF = "self"
CLAUDE_CODE = "claude-code"
CODEX = "codex"
OPENCLAW = "openclaw"

#: Settings/env key naming the runtime explicitly.
RUNTIME_ENV = "BOTCIRCUITS_RUNTIME"


@dataclass(frozen=True)
class _RuntimeSpec:
    """Static facts about a known runtime provider."""
    name: str
    #: Env var names that, when present & truthy, mark this host as active.
    env_markers: tuple[str, ...]
    #: CLI binary to probe on PATH (empty for native).
    binary: str
    #: Default argv template; ``{prompt}`` is the prompt placeholder.
    command: tuple[str, ...]


_REGISTRY: dict[str, _RuntimeSpec] = {
    NATIVE: _RuntimeSpec(NATIVE, (), "", ()),
    # The inline/self runtime needs no binary or argv — the host agent performs
    # segments in-session via the step driver. Selected explicitly (the
    # workflow-running skill drives it), never auto-probed.
    SELF: _RuntimeSpec(SELF, (), "", ()),
    CLAUDE_CODE: _RuntimeSpec(
        CLAUDE_CODE,
        env_markers=("CLAUDECODE", "CLAUDE_CODE", "CLAUDE_CODE_ENTRYPOINT"),
        binary="claude",
        # Headless, one segment per process. No permission flag: the segment
        # runs in the MAIN agent's working directory (see ClaudeCodeRuntime),
        # so it inherits the project's `.claude/settings.json` permission rules
        # — the same policy the user already approved for the main session.
        command=("claude", "-p", "{prompt}", "--output-format", "json"),
    ),
    # Stubs: registered for detection/selection now; their result parsing
    # may need a per-provider adapter in result.py when implemented.
    CODEX: _RuntimeSpec(
        CODEX,
        env_markers=("CODEX_SANDBOX", "CODEX_HOME"),
        binary="codex",
        command=("codex", "exec", "{prompt}", "--json"),
    ),
    OPENCLAW: _RuntimeSpec(
        OPENCLAW,
        env_markers=("OPENCLAW", "OPENCLAW_SESSION"),
        binary="openclaw",
        command=("openclaw", "run", "{prompt}", "--json"),
    ),
}

#: Order auto-detection probes runtimes in. Native is the implicit default,
#: not probed.
_DETECT_ORDER = (CLAUDE_CODE, CODEX, OPENCLAW)


def _truthy_env(name: str) -> bool:
    val = os.environ.get(name)
    return bool(val) and val.strip().lower() not in ("0", "false", "no", "off")


def detect_runtime_name(settings: dict | None = None) -> str:
    """Resolve the runtime provider NAME using the precedence above.

    `settings` is the merged settings dict (from `load_layered_settings`);
    its `runtime` key, when set, is the explicit choice second only to the
    env override. Returns a name guaranteed to be in `_REGISTRY`.
    """
    # 1. Explicit — env var, then settings.
    explicit = os.environ.get(RUNTIME_ENV, "").strip()
    if not explicit and isinstance(settings, dict):
        val = settings.get("runtime")
        if isinstance(val, str):
            explicit = val.strip()
    if explicit:
        normalized = explicit.lower()
        if normalized in _REGISTRY:
            return normalized
        # Unknown explicit value: fall through to detection rather than
        # erroring, but it's worth a warning at the call site.
        return normalized

    # 2. Auto-detect — env markers first (cheap, definitive), then PATH probe.
    for name in _DETECT_ORDER:
        spec = _REGISTRY[name]
        if any(_truthy_env(m) for m in spec.env_markers):
            return name
    for name in _DETECT_ORDER:
        spec = _REGISTRY[name]
        if spec.binary and shutil.which(spec.binary):
            return name

    # 3. Default.
    return NATIVE


def runtime_config(name: str, settings: dict | None = None) -> RuntimeConfig:
    """Build a `RuntimeConfig` for `name`, layering settings overrides over
    the registry defaults.

    Settings may override the argv template / timeout under a `runtimes`
    map keyed by provider name, e.g.::

        {"runtimes": {"claude-code": {"command": [...], "timeout": 300}}}
    """
    spec = _REGISTRY.get(name)
    command = list(spec.command) if spec else []
    timeout = 600.0
    # Run CLI segments in the main agent's working directory so the spawned
    # CLI inherits the project's `.claude/settings.json` permission rules
    # (the policy the user already approved for the main session). Settings
    # may override this; `None` falls back to a fresh isolated temp dir.
    cwd: str | None = os.getcwd()

    if isinstance(settings, dict):
        overrides = (settings.get("runtimes") or {}).get(name) or {}
        if isinstance(overrides, dict):
            if isinstance(overrides.get("command"), list):
                command = [str(t) for t in overrides["command"]]
            if isinstance(overrides.get("timeout"), (int, float)):
                timeout = float(overrides["timeout"])
            if "cwd" in overrides:
                val = overrides["cwd"]
                cwd = str(val) if isinstance(val, str) and val else None

    return RuntimeConfig(name=name, command=command, timeout=timeout, cwd=cwd)


def select_runtime(
    settings: dict | None = None,
    *,
    native_factory: Callable[[], AgentRuntimeProvider] | None = None,
    name: str | None = None,
) -> AgentRuntimeProvider:
    """Instantiate the selected runtime provider.

    `name` forces a specific provider (skips detection). `native_factory`
    builds the native provider lazily — it wraps a live `Agent`, which the
    caller owns, so this module never constructs one itself. When the
    selected runtime is `native` but no factory is supplied, that's a
    configuration error the caller must handle.
    """
    chosen = (name or detect_runtime_name(settings)).lower()
    config = runtime_config(chosen, settings)

    if chosen == NATIVE:
        if native_factory is None:
            raise ValueError(
                "native runtime selected but no native_factory provided; "
                "the native provider wraps an Agent the caller must build."
            )
        return native_factory()

    if chosen == SELF:
        # The host agent performs segments in-session; the step driver
        # (`step_workflow`) owns the per-segment loop, so this provider just
        # hands segments back. Built directly here for callers that want it.
        from botcircuits.runtime.providers.inline import InlineRuntime

        return InlineRuntime()

    # CLI providers. Imported lazily so `native`-only callers don't pull the
    # CLI exec machinery (and so a missing provider module is a clear error).
    from botcircuits.runtime.providers.claude_code import ClaudeCodeRuntime

    if chosen in (CLAUDE_CODE, CODEX, OPENCLAW):
        # claude-code is the reference CLI impl; codex/openclaw reuse it via
        # config (same JSON contract) until they need a bespoke parser.
        return ClaudeCodeRuntime(config)

    # Unknown explicit name: fall back to native if we can, else error.
    if native_factory is not None:
        return native_factory()
    raise ValueError(f"unknown runtime {chosen!r} and no native fallback available")


__all__ = [
    "NATIVE",
    "SELF",
    "CLAUDE_CODE",
    "CODEX",
    "OPENCLAW",
    "RUNTIME_ENV",
    "detect_runtime_name",
    "runtime_config",
    "select_runtime",
]
