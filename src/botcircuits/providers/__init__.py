"""LLM provider implementations.

Each concrete provider conforms to the `LLMProvider` ABC in `.base` and
exposes both `complete()` and `stream()`. Pick one and pass it to
`Agent(provider=...)`.
"""

import os

from botcircuits.providers.base import LLMProvider
from botcircuits.providers.anthropic import AnthropicProvider
from botcircuits.providers.openai import OpenAIProvider
from botcircuits.providers.gemini import GeminiProvider
from botcircuits.providers.openrouter import OpenRouterProvider


def make_provider(kind: str, model: str | None) -> LLMProvider:
    """Build an `LLMProvider` by short name (`anthropic`/`openai`/`gemini`/
    `openrouter`), falling back to that provider's model env var, then its
    own default.

    Shared factory so callers that need to build a provider dynamically
    (e.g. `NativeRuntime` resolving a per-agent model override) don't
    duplicate the provider-name → class mapping.
    """
    if kind == "openai":
        return OpenAIProvider(model=model or os.getenv("OPENAI_MODEL", "gpt-4.1"))
    if kind == "gemini":
        return GeminiProvider(model=model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    if kind == "openrouter":
        return OpenRouterProvider(model=model or os.getenv("OPENROUTER_MODEL", "openai/gpt-4.1"),
                                  api_key=os.getenv("OPENROUTER_API_KEY"))
    return AnthropicProvider(model=model or os.getenv("ANTHROPIC_MODEL", "claude-opus-4-7"))


__all__ = [
    "LLMProvider",
    "AnthropicProvider",
    "OpenAIProvider",
    "GeminiProvider",
    "OpenRouterProvider",
    "make_provider",
]
