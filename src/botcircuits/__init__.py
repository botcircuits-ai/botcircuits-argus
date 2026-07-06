"""botcircuits-agent package.

Loads environment variables from `.env` files on import so that any entry
point — library, CLI, or FastAPI gateway — picks them up without each one
needing its own bootstrap.

Resolution order (highest to lowest precedence; existing process env vars
always win over all of these, and `load_dotenv(..., override=False)` never
overwrites a var a higher-precedence file already set):
  1. `BOTCIRCUITS_ENV_FILE`, if set  (explicit override)
  2. <cwd>/.env                      (legacy/plain dotenv convention)
  3. <cwd>/.botcircuits/.env         (project, written by `setup --project`/`--local`)
  4. ~/.botcircuits/.env             (user, written by `botcircuits-cli setup`)

This mirrors `cli.settings`'s settings.json layering so "where did this
env var come from" has one answer instead of two unrelated lookup rules —
previously this walked up from cwd via `dotenv.find_dotenv`, which never
looked in `~/.botcircuits/` at all, so an API key saved there by the setup
wizard silently went unused unless the CLI happened to be run from `~`.

Public API:

    from botcircuits import (
        Agent, default_registry, LocalTool, ToolRegistry,
        MCPServer, SkillSpec,
        AnthropicProvider, OpenAIProvider, GeminiProvider, OpenRouterProvider,
        StreamEvent, Message, ToolCall, LLMResponse,
    )
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def _load_env() -> None:
    # `load_dotenv(..., override=False)` never clobbers a var that's
    # already set in the process environment — so to honor the documented
    # precedence (explicit > project > user), load highest-precedence
    # first; each subsequent file only fills in vars still unset.
    explicit = os.getenv("BOTCIRCUITS_ENV_FILE")
    if explicit:
        load_dotenv(explicit, override=False)

    for candidate in (
        Path.cwd() / ".env",
        Path.cwd() / ".botcircuits" / ".env",
        Path.home() / ".botcircuits" / ".env",
    ):
        if candidate.is_file():
            load_dotenv(candidate, override=False)


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
    OpenRouterProvider,
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
    "OpenRouterProvider",
    # types
    "LLMResponse",
    "Message",
    "StreamEvent",
    "ToolCall",
]
