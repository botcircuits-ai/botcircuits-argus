"""CLI entry point — argument parsing, provider construction, REPL loop.

Examples:
  uv run botcircuits-cli
  uv run botcircuits-cli --provider openai
  uv run botcircuits-cli --provider gemini --model gemini-2.5-flash
  uv run botcircuits-cli --no-stream
  uv run botcircuits-cli --system "You are a personal assistant."
  uv run botcircuits-cli --config ./custom-settings.json    # explicit override
  echo "what's 2+2?" | uv run botcircuits-cli --no-stream

Settings files (auto-loaded, lowest to highest precedence):
  ~/.botcircuits/settings.json            user, applies to all projects
  ./.botcircuits/settings.json            project, checked into VCS
  ./.botcircuits/settings.local.json      project, gitignored
  --config <path>                         explicit override
  CLI flags                               final word

Manage MCP servers in the settings files (defaults to the project's
shared file; pass --user or --local to target other layers):
  uv run botcircuits-cli mcp list
  uv run botcircuits-cli mcp add fs \\
      --mode local --transport stdio --command npx \\
      --args -y,@modelcontextprotocol/server-filesystem,/tmp
  uv run botcircuits-cli mcp test fs
  uv run botcircuits-cli mcp remove fs

Slash commands (interactive only):
  /reset            drop current session and start fresh
  /session [id]     show or switch session_id
  /system <text>    set system prompt for new sessions
  /stream on|off    toggle streaming
  /tools            list registered tools
  /help             show commands
  /quit             exit
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Optional

from botcircuits.agent import (
    Agent, DurableConversationStore, default_registry, register_workflows,
    collect_agents_config,
)
from botcircuits.agent.workflow import LocalWorkflowError
from botcircuits.providers import AnthropicProvider, GeminiProvider, OpenAIProvider, OpenRouterProvider
from botcircuits.providers.base import LLMProvider
from botcircuits.cli.ansi import C, out
from botcircuits.cli.commands import CLIState, handle_slash
from botcircuits.cli.commands_mcp import add_mcp_subparser, run_mcp_command
from botcircuits.cli.commands_manager import add_manager_subparser, run_manager_command
from botcircuits.cli.commands_gateway import add_gateway_subparser, run_gateway_command
from botcircuits.cli.commands_skills import add_skills_subparser, run_skills_command
from botcircuits.cli.commands_init import add_init_subparser, run_init_command
from botcircuits.cli.commands_workflow import add_workflow_subparser, run_workflow_command
from botcircuits.cli.config import CLIConfig, ConfigError, resolve
from botcircuits.cli.render import run_blocking, run_streaming
from botcircuits.cli.settings import load_layered_settings
from botcircuits.cli.setup import add_setup_subparser, run_setup_wizard
from botcircuits.cli.system_prompt import DEFAULT_SYSTEM_PROMPT
from botcircuits.cli.tui import TUISession, set_tui_session


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

_CHAT_FLAGS = (
    "provider", "model", "system", "session", "stream",
    "max_tokens", "max_steps", "show_tool_results",
)


def build_parser() -> argparse.ArgumentParser:
    """All chat flags default to None so we can distinguish 'not passed'
    from 'passed with the default value'. Real defaults live in
    `config.DEFAULTS` and are applied by `config.resolve()`.

    Subcommands (currently just `mcp`) live under a subparser; running
    with no subcommand drops into the chat REPL."""
    p = argparse.ArgumentParser(prog="botcircuits-cli", description="Agent CLI")
    p.add_argument("--config", default=None,
                   help="Path to an explicit JSON settings file. Overrides the "
                        "auto-discovered ~/.botcircuits/ and .botcircuits/ files. "
                        "CLI flags still win over its values.")
    p.add_argument("--provider", default=None,
                   choices=["anthropic", "openai", "gemini", "openrouter"],
                   help="LLM provider (default: $LLM_PROVIDER or 'anthropic')")
    p.add_argument("--model", default=None,
                   help="Override the provider's default model")
    p.add_argument("--system", default=None, help="System prompt")
    p.add_argument("--session", default=None,
                   help="Resume a session id (persisted under .botcircuits/sessions, "
                        "so it survives across CLI runs)")

    # Three-way streaming flag: --stream / --no-stream / unset.
    stream_group = p.add_mutually_exclusive_group()
    stream_group.add_argument("--stream", dest="stream", action="store_true",
                              default=None, help="Force streaming on")
    stream_group.add_argument("--no-stream", dest="stream", action="store_false",
                              default=None, help="Disable streaming responses")

    p.add_argument("--max-tokens", type=int, default=None,
                   help="Per-call token cap (default 4096)")
    p.add_argument("--max-steps", type=int, default=None,
                   help="Max tool-use rounds per turn (default 10)")

    # store_const keeps the 'unset' sentinel as None (vs store_true which
    # would default to False and look like a user choice).
    p.add_argument("--show-tool-results", dest="show_tool_results",
                   action="store_const", const=True, default=None,
                   help="Print full tool result payloads (default: short preview)")

    # --auto skips the y/N gate on every gated tool (shell_exec,
    # write_file, edit_file, plan_and_confirm). Stays None when not
    # passed so it doesn't override per-tool `auto` from JSON.
    p.add_argument("--auto", dest="auto",
                   action="store_const", const=True, default=None,
                   help="Skip y/N confirmation on all gated tools "
                        "(shell_exec, shell_stop, write_file, edit_file, "
                        "plan_and_confirm). A warning still prints before "
                        "each action. Overrides per-tool `auto` in JSON.")

    sub = p.add_subparsers(dest="subcommand")
    add_mcp_subparser(sub)
    add_workflow_subparser(sub)
    add_manager_subparser(sub)
    add_gateway_subparser(sub)
    add_skills_subparser(sub)
    add_setup_subparser(sub)
    add_init_subparser(sub)
    return p


# Tools whose `auto` field should be flipped on by --auto. Every
# gated tool registers `auto` as a config key — adding one here is the
# only wiring needed for --auto to cover it.
_AUTO_GATED_TOOLS = (
    "shell_exec",
    "shell_stop",
    "write_file",
    "edit_file",
    "plan_and_confirm",
    "build_workflow",
)


def load_cli_config(args: argparse.Namespace) -> CLIConfig:
    """Apply CLI args on top of the layered settings files and return
    the resolved CLIConfig.

    Settings discovery (lowest to highest precedence):
        ~/.botcircuits/settings.json
        ./.botcircuits/settings.json           (project, shared)
        ./.botcircuits/settings.local.json     (project, gitignored)
        --config <path>                        (explicit override)
        $LLM_PROVIDER                          (provider only; see below)
        CLI flags                              (highest)

    `--auto` is special: it doesn't have its own CLIConfig field, it
    flows into the `auto` key of every gated tool (shell_exec,
    write_file, edit_file, plan_and_confirm). We merge it after the
    normal resolve so it overrides whatever the settings files said.
    """
    file_values, _used = load_layered_settings(explicit=args.config)
    cli_values = {k: getattr(args, k) for k in _CHAT_FLAGS}
    # `provider` also has an env-var form ($LLM_PROVIDER, same as the
    # gateway's lifespan()) so a provider switch doesn't require editing
    # settings.json — without this, a stale "provider" key written by an
    # earlier `setup llm` run would silently outrank $LLM_PROVIDER, since
    # `resolve()` only lets CLI-equivalent values (non-None here) override
    # file values, and $LLM_PROVIDER was previously only wired into
    # `DEFAULTS`, the lowest-precedence layer. --provider still wins over
    # the env var when both are given.
    resolved_provider = args.provider or os.getenv("LLM_PROVIDER")
    cli_values["provider"] = resolved_provider

    # `model` in settings.json is only meaningful paired with the provider
    # that was active when it was written. If the provider just got
    # overridden above (env var or --provider) away from whatever
    # settings.json's own "provider" says, that file's "model" almost
    # certainly belongs to the OLD provider (e.g. an OpenRouter model id
    # surviving a switch to openai) — so drop it unless the user also gave
    # --model explicitly. `make_provider()` then falls through to that
    # provider's own env var / hardcoded default instead.
    if (resolved_provider and args.model is None
            and file_values.get("provider") not in (None, resolved_provider)):
        file_values = dict(file_values)
        file_values.pop("model", None)

    cfg = resolve(file_values, cli_values)

    if args.auto is not None:
        for tool_name in _AUTO_GATED_TOOLS:
            existing = cfg.tools.get(tool_name, {})
            # Don't resurrect a disabled tool. If the JSON explicitly
            # set `<tool>: null`, --auto is a no-op for that tool —
            # the user disabled it on purpose.
            if existing is None or existing is False:
                continue
            tool_cfg = dict(existing) if isinstance(existing, dict) else {}
            tool_cfg["auto"] = args.auto
            cfg.tools[tool_name] = tool_cfg

    # Fall back to the code-gen system prompt when the user didn't set one.
    # Explicitly empty string disables it (lets the user say "no system prompt").
    if cfg.system is None:
        cfg.system = DEFAULT_SYSTEM_PROMPT

    return cfg


# ---------------------------------------------------------------------------
# Provider construction
# ---------------------------------------------------------------------------

def make_provider(kind: str, model: Optional[str]) -> LLMProvider:
    if kind == "anthropic":
        return AnthropicProvider(model=model or os.getenv("ANTHROPIC_MODEL", "claude-opus-4-7"))
    if kind == "openai":
        return OpenAIProvider(model=model or os.getenv("OPENAI_MODEL", "gpt-4.1"))
    if kind == "gemini":
        return GeminiProvider(model=model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    if kind == "openrouter":
        return OpenRouterProvider(model=model or os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1"),
                                   api_key=os.getenv("OPENROUTER_API_KEY"))
    raise ValueError(f"Unknown provider: {kind}")



# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def amain(args: argparse.Namespace) -> int:
    try:
        cfg = load_cli_config(args)
    except ConfigError as e:
        out(C.red(f"[config] {e}"))
        return 2

    state = CLIState(cfg)
    interactive = sys.stdin.isatty()
    provider = make_provider(cfg.provider, cfg.model)

    try:
        registry = default_registry(cfg.tools, provider=provider, permissions=cfg.permissions)
    except ValueError as e:
        out(C.red(f"[tools] {e}"))
        return 2

    try:
        normalize_enabled = bool(cfg.workflow.get("normalize", True))
        registered, skipped = await register_workflows(
            registry,
            provider=provider,
            normalize_enabled=normalize_enabled,
        )
        if interactive and registered:
            note = "" if normalize_enabled else " (normalize=off)"
            out(C.dim(f"workflows: {', '.join(registered)}{note}"))
        if skipped:
            out(C.yellow(
                f"[workflow] skipped (name collides with built-in tool): "
                f"{', '.join(skipped)}"
            ))
    except LocalWorkflowError as e:
        out(C.red(f"[workflow] {e}"))
        return 2

    # Per-agent bindings from every discovered workflow's `agents` map, so a
    # workflow step pinned to a named agent runs on that agent's in-process
    # model under the native runtime (see Agent._resolve_segment_provider).
    agents_config = await collect_agents_config()

    async with Agent(
        provider=provider,
        tools=registry,
        mcp_servers=cfg.mcp_servers,
        max_tokens=cfg.max_tokens,
        max_steps=cfg.max_steps,
        mode=cfg.mode,
        agents_config=agents_config,
        # Durable sessions: persisted as JSON-L under .botcircuits/sessions
        # after every turn, so `--session <id>` / `/session <id>` resumes
        # across CLI runs and `search_memory` can recall past conversations.
        store=DurableConversationStore(),
    ) as agent:

        if interactive:
            from botcircuits.cli.banner import print_banner
            print_banner(agent, provider, cfg)

        async with TUISession(interactive=interactive) as tui:
            set_tui_session(tui)

            while True:
                msg = await tui.read_message()
                if msg is None:
                    if interactive:
                        out()
                    return 0
                if not msg.strip():
                    continue

                # A background task may have queued a pause while the user was
                # typing. Drain the queue now so dispatch_reply sees it.
                await tui._maybe_activate_next_pause()

                # Route reply to a paused background task (workflow, permission, etc.)
                if await tui.dispatch_reply(msg):
                    # Give the resumed task a few event-loop ticks to run before
                    # we read the next user message.
                    for _ in range(5):
                        await asyncio.sleep(0)
                    continue

                if msg.startswith("/"):
                    if interactive:
                        try:
                            _handled, follow_up = await handle_slash(msg, agent, state)
                        except SystemExit:
                            return 0
                        if follow_up:
                            msg = follow_up
                        else:
                            continue

                # Run the LLM call as a background task so the input prompt
                # stays live during streaming / tool calls.
                async def _chat(msg: str = msg) -> None:
                    import time as _time
                    _t0 = _time.monotonic()
                    try:
                        if state.stream:
                            await run_streaming(agent, msg, state)
                        else:
                            await run_blocking(agent, msg, state)
                    except KeyboardInterrupt:
                        out()
                        out(C.yellow("(interrupted)"))
                        return
                    except Exception as e:
                        out()
                        out(C.red(f"[error] {type(e).__name__}: {e}"))
                        return
                    elapsed = _time.monotonic() - _t0
                    total_tok = provider.usage_input_tokens + provider.usage_output_tokens
                    tok_str = f"{total_tok / 1000:.1f}K" if total_tok >= 1000 else str(total_tok)
                    out(C.dim(
                        f"  {provider.model[:20]}  |  {tok_str}M tokens  "
                        f"|  {elapsed:.0f}s  |  ⊙ {elapsed:.0f}s"
                    ))

                tui.submit(_chat())


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.subcommand == "mcp":
            rc = run_mcp_command(args)
        elif args.subcommand == "workflow":
            rc = run_workflow_command(args)
        elif args.subcommand == "manager":
            rc = run_manager_command(args)
        elif args.subcommand == "gateway":
            rc = run_gateway_command(args)
        elif args.subcommand == "skills":
            rc = run_skills_command(args)
        elif args.subcommand == "setup":
            rc = run_setup_wizard(args)
        elif args.subcommand == "init":
            rc = run_init_command(args)
        else:
            rc = asyncio.run(amain(args))
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)
