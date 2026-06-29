"""FastAPI application factory and lifespan.

Run:
  uv run uvicorn botcircuits.gateway:app --reload --port 8000
  # or, equivalently:
  uv run python -m botcircuits.gateway

Configure via env vars (loaded from .env on import):
  LLM_PROVIDER=anthropic|openai|gemini|openrouter   (default anthropic)
  ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY / OPENROUTER_API_KEY
  BOTCIRCUITS_CONFIG=/path/to/settings.json   (optional, explicit override)

The gateway auto-discovers the same layered settings files as the CLI:
  ~/.botcircuits/settings.json
  ./.botcircuits/settings.json
  ./.botcircuits/settings.local.json
Plus `$BOTCIRCUITS_CONFIG` if set (treated as the explicit override
layer — parallel to the CLI's `--config`). Env vars still win over
JSON values for compatibility.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from botcircuits.agent import Agent, default_registry
from botcircuits.cli.config import resolve
from botcircuits.cli.settings import load_layered_settings
from botcircuits.cli.system_prompt import DEFAULT_SYSTEM_PROMPT
from botcircuits.providers import AnthropicProvider, GeminiProvider, OpenAIProvider, OpenRouterProvider
from botcircuits.providers.base import LLMProvider
from botcircuits.gateway.channels import CronChannel, SlackChannel, WebhookChannel, WhatsAppChannel
from botcircuits.gateway.messaging import MessageGateway
from botcircuits.gateway.messaging_config import load as load_messaging_config
from botcircuits.gateway.routes import router


def _make_provider(kind: str, model: str | None) -> LLMProvider:
    if kind == "anthropic":
        return AnthropicProvider(model=model or os.getenv("ANTHROPIC_MODEL", "claude-opus-4-7"))
    if kind == "openai":
        return OpenAIProvider(model=model or os.getenv("OPENAI_MODEL", "gpt-4.1"))
    if kind == "gemini":
        return GeminiProvider(model=model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    if kind == "openrouter":
        return OpenRouterProvider(model=model or os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1"))
    raise ValueError(f"Unknown LLM_PROVIDER: {kind}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    file_values, _used = load_layered_settings(
        explicit=os.getenv("BOTCIRCUITS_CONFIG") or None,
    )
    # Env-var-style overrides for parity with the CLI's --provider/--model.
    cli_values = {
        "provider": os.getenv("LLM_PROVIDER"),
        "model": None,
        "system": None, "session": None, "stream": None,
        "max_tokens": None, "max_steps": None, "show_tool_results": None,
    }
    cfg = resolve(file_values, cli_values)

    provider = _make_provider(cfg.provider, cfg.model)
    agent = Agent(
        provider=provider,
        tools=default_registry(cfg.tools, provider=provider),
        mcp_servers=cfg.mcp_servers,
        max_tokens=cfg.max_tokens,
        max_steps=cfg.max_steps,
        mode=cfg.mode,
    )
    await agent.start()
    app.state.agent = agent
    # Per-request `system` overrides this; if absent, routes fall back
    # to whatever the JSON config set (or the code-gen default).
    app.state.default_system = cfg.system or DEFAULT_SYSTEM_PROMPT

    # Build the message gateway and register every channel whose
    # credentials/config are present. Routers are mounted before
    # `start()` so an inbound request that races startup gets a
    # well-defined 503 from the channel rather than a 404 from the app.
    gateway = MessageGateway(agent, default_system=app.state.default_system)
    msg_cfg = load_messaging_config()
    if msg_cfg.whatsapp is not None:
        gateway.register(WhatsAppChannel(msg_cfg.whatsapp, gateway))
    if msg_cfg.slack is not None:
        gateway.register(SlackChannel(msg_cfg.slack, gateway))
    if msg_cfg.webhook is not None:
        gateway.register(WebhookChannel(msg_cfg.webhook, gateway))
    if msg_cfg.cron_jobs:
        gateway.register(CronChannel(msg_cfg.cron_jobs, gateway))
    for ch in gateway.channels():
        r = ch.routes()
        if r is not None:
            app.include_router(r)
    await gateway.start()
    app.state.gateway = gateway

    try:
        yield
    finally:
        await gateway.stop()
        await agent.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="botcircuits-agent", lifespan=lifespan)
    app.include_router(router)
    return app


app = create_app()
