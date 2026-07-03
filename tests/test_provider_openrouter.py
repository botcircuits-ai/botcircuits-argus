"""OpenRouter provider construction and registry wiring.

`OpenRouterProvider` reuses `OpenAIProvider`'s request/response shaping
wholesale (see providers/openrouter.py) — these tests only cover the parts
that are actually new: client construction (base_url/headers/model default)
and the `make_provider` dispatch, not the inherited complete()/stream() logic
(already whatever OpenAIProvider's own behavior is).
"""

from __future__ import annotations

import os

from botcircuits.providers import make_provider
from botcircuits.providers.openrouter import OPENROUTER_BASE_URL, OpenRouterProvider


def test_default_model_and_base_url():
    p = OpenRouterProvider(api_key="test-key")
    assert p.model == "openai/gpt-4.1"
    assert p.name == "openrouter"
    assert str(p.client.base_url).rstrip("/") == OPENROUTER_BASE_URL


def test_custom_model_and_base_url_override():
    p = OpenRouterProvider(model="anthropic/claude-3.7-sonnet",
                            api_key="test-key",
                            base_url="https://example.com/v1")
    assert p.model == "anthropic/claude-3.7-sonnet"
    assert str(p.client.base_url).rstrip("/") == "https://example.com/v1"


def test_attribution_headers_set():
    p = OpenRouterProvider(api_key="test-key")
    headers = p.client.default_headers
    assert headers.get("X-Title") == "BotCircuits"
    assert "HTTP-Referer" in headers


def test_make_provider_dispatches_to_openrouter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    p = make_provider("openrouter", None)
    assert isinstance(p, OpenRouterProvider)
    assert p.model == "openai/gpt-4.1"


def test_make_provider_openrouter_honors_model_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-chat")
    p = make_provider("openrouter", None)
    assert p.model == "deepseek/deepseek-chat"


def test_make_provider_openrouter_explicit_model_wins_over_env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "deepseek/deepseek-chat")
    p = make_provider("openrouter", "meta-llama/llama-3.3-70b-instruct")
    assert p.model == "meta-llama/llama-3.3-70b-instruct"
