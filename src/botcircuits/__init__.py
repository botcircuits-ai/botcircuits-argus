"""botcircuits-agent package.

Loads environment variables from a `.env` file (if present) on import so
that any entry point — library, CLI, or FastAPI gateway — picks them up
without each one needing its own bootstrap.

Resolution order:
  1. Path in `BOTCIRCUITS_ENV_FILE` (if set)
  2. Walk up from the current working directory for the nearest `.env`
Existing process env vars are NOT overridden.

Public API:

    from botcircuits import (
        Agent, default_registry, LocalTool, ToolRegistry,
        MCPServer, SkillSpec,
        AnthropicProvider, OpenAIProvider, GeminiProvider,
        StreamEvent, Message, ToolCall, LLMResponse,
    )
"""

from __future__ import annotations

import os

from dotenv import find_dotenv, load_dotenv


def _load_env() -> None:
    explicit = os.getenv("BOTCIRCUITS_ENV_FILE")
    if explicit:
        load_dotenv(explicit, override=False)
        return
    found = find_dotenv(usecwd=True)
    if found:
        load_dotenv(found, override=False)


_load_env()


# Public API re-exports. Keep this list tight — only what's meant to be
# used externally. Internal modules should import from the submodule
# directly (e.g. `from .agent.core import Agent`) to avoid circular
# imports during package initialization.
from botcircuits.agent import (
    Agent,
    ConversationStore,
    LocalSkill,
    LocalTool,
    MCPServer,
    SkillSpec,
    ToolRegistry,
    default_registry,
)
from botcircuits.providers import (
    AnthropicProvider,
    GeminiProvider,
    LLMProvider,
    OpenAIProvider,
)
from botcircuits.types import LLMResponse, Message, StreamEvent, ToolCall

__all__ = [
    # agent
    "Agent",
    "ConversationStore",
    "LocalSkill",
    "LocalTool",
    "MCPServer",
    "SkillSpec",
    "ToolRegistry",
    "default_registry",
    # providers
    "AnthropicProvider",
    "GeminiProvider",
    "LLMProvider",
    "OpenAIProvider",
    # types
    "LLMResponse",
    "Message",
    "StreamEvent",
    "ToolCall",
]
