"""Expose a CLI agent runtime (claude-code, codex, …) as an `LLMProvider`.

The workflow BUILD pipeline (`workflow build` / `workflow generate`) is
intelligence the same way the RUN pipeline is: it calls an LLM to compile
NL `conditions` into expressions, aggregate variables, optimize actions, and
author drafts. But it talked to that LLM through `make_provider(...)` — a
DIRECT third-party client (Anthropic/OpenAI/Gemini) needing its own API key —
while the run pipeline dispatches to whatever host agent is already running
(claude-code et al.) via `select_runtime(...)`, no key required.

This adapter closes that gap. It wraps an `AgentRuntimeProvider`'s CLI config
in the `LLMProvider` interface the build helpers expect: `complete()` flattens
the system + message turns into one prompt, shells out through the SAME
`run_cli` machinery the segment executor uses, and returns the host agent's
final assistant text. The build helpers do their own strict-JSON extraction on
that text, so nothing about their prompting changes — only the transport.

`stream()` is implemented over `complete()` (one chunk + the final response);
the build pipeline only ever calls `complete()`, but the ABC requires both.
"""

from __future__ import annotations

import sys
from typing import AsyncIterator

from botcircuits.agent.mcp import MCPServer
from botcircuits.agent.skill import SkillSpec
from botcircuits.agent.tools import LocalTool
from botcircuits.providers.base import LLMProvider
from botcircuits.runtime.base import RuntimeConfig
from botcircuits.runtime.cli_exec import CliExecError, run_cli
from botcircuits.runtime.result import assistant_text_from_stdout
from botcircuits.types import LLMResponse, Message, ProviderStreamEvent


def _flatten_prompt(system: str, messages: list[Message]) -> str:
    """Render a system prompt + message turns into one CLI prompt string.

    The host CLI is stateless and headless (one `-p` invocation), so we
    serialize the whole exchange into a single prompt: the system block first,
    then each turn's text blocks labelled by role. Only `text` blocks carry
    here — the build helpers never send tools or tool results.
    """
    parts: list[str] = []
    if system and system.strip():
        parts.append(system.strip())
    for msg in messages:
        text = "\n".join(
            b.get("text", "")
            for b in (msg.blocks or [])
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
        if not text:
            continue
        # A single user turn (the common build case) needs no role label —
        # keep the prompt clean. Label only when more than one turn is present.
        if len(messages) > 1:
            parts.append(f"[{msg.role}]\n{text}")
        else:
            parts.append(text)
    return "\n\n".join(parts)


class CliLLMProvider(LLMProvider):
    """`LLMProvider` backed by a headless CLI agent (claude-code et al.).

    Construct with the runtime's `RuntimeConfig` (its argv template, timeout,
    and cwd). `name`/`model` are surfaced in build logs so the operator can see
    which agent compiled the workflow.
    """

    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.name = config.name or "cli"
        # The host agent picks its own model; we don't choose one. Surface the
        # runtime name in the `model` slot the CLI build log prints.
        self.model = config.name or "cli"

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[LocalTool],
        hosted_mcp: list[MCPServer],
        skills: list[SkillSpec],
        max_tokens: int,
    ) -> LLMResponse:
        prompt = _flatten_prompt(system, messages)
        try:
            res = await run_cli(
                self.config.command, prompt,
                timeout=self.config.timeout, cwd=self.config.cwd,
            )
        except CliExecError as e:
            raise RuntimeError(
                f"build runtime {self.name!r} could not be started: {e}"
            ) from e

        if not res.ok:
            print(
                f"[runtime:{self.name}] build CLI exited rc={res.returncode}"
                f"{' (timeout)' if res.timed_out else ''}: "
                f"{res.stderr.strip()[:300]}",
                file=sys.stderr,
            )

        text = assistant_text_from_stdout(res.stdout)
        # The host CLI bills its own tokens; we have no usage to record.
        return LLMResponse(
            text=text,
            tool_calls=[],
            stop_reason="end_turn",
            raw=res.stdout,
        )

    async def stream(
        self,
        system: str,
        messages: list[Message],
        tools: list[LocalTool],
        hosted_mcp: list[MCPServer],
        skills: list[SkillSpec],
        max_tokens: int,
    ) -> AsyncIterator[ProviderStreamEvent]:
        """Non-incremental: run `complete()` and yield its text once, then the
        final response. The build pipeline only calls `complete()`; this exists
        to satisfy the ABC and stay usable if a caller ever streams."""
        resp = await self.complete(
            system, messages, tools, hosted_mcp, skills, max_tokens,
        )
        if resp.text:
            yield ("text_delta", resp.text)
        yield ("final", resp)


__all__ = ["CliLLMProvider"]
