"""OpenRouter provider — OpenAI-compatible Responses API, multi-vendor routing.

OpenRouter exposes a Responses-API-compatible endpoint at a custom `base_url`,
so this reuses `OpenAIProvider`'s request/response shaping wholesale and only
overrides client construction: a different `base_url`, `OPENROUTER_API_KEY`,
and the attribution headers OpenRouter's docs recommend (`HTTP-Referer`,
`X-Title`) for usage to show up correctly on the dashboard.

Model ids are OpenRouter's own `vendor/model` form (e.g.
`anthropic/claude-3.7-sonnet`, `deepseek/deepseek-chat`), not native vendor ids.

OpenRouter's Responses API is marked "beta" in their docs — tool-calling and
usage-field parity across the many vendors it fans out to isn't guaranteed the
way it is for OpenAI's own models. Verify a target model supports function
calling before relying on it in a workflow `agents` map.
"""

from __future__ import annotations

from botcircuits.providers.openai import OpenAIProvider

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterProvider(OpenAIProvider):
    name = "openrouter"

    def __init__(self, model: str = "openai/gpt-4.1", api_key: str | None = None,
                 base_url: str | None = None):
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or OPENROUTER_BASE_URL,
            default_headers={
                "HTTP-Referer": "https://github.com/botcircuits-ai/botcircuits-argus",
                "X-Title": "BotCircuits",
            },
        )
        self.model = model
