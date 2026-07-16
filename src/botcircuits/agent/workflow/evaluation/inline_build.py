"""Inline workflow build for evaluation cases.

A normal evaluation case targets an EXISTING workflow on disk. An
*inline* case carries a natural-language `workflow_spec` and asks the
harness to author the workflow on the fly at eval time, run the
comparison, then delete the file. This mirrors what `/workflow add
"<prompt>"` does in the CLI but runs unattended:

  1. Ask the LLM to translate `workflow_spec` into the structured
     `build_workflow` payload (`summary` + `workflow`).
  2. Call the `build_workflow` tool's handler programmatically with
     `auto=True` so the y/N gate is skipped. This writes both the raw
     source under `.botcircuits/workflows/<name>.json` and the indexed
     runnable artifact under `.botcircuits/workflows/.build/<name>.json`.
  3. Return the chosen workflow name. The harness then runs the
     existing workflow + prompt-only runners against it.
  4. After the eval pass, delete both files. Cleanup runs even when
     the build or the runners themselves fail.

This is the same code path the CLI uses (no parallel implementation),
so eval-time correctness is bounded by build_workflow's correctness in
production.
"""

from __future__ import annotations

import contextlib
import json
import re

from botcircuits.providers.base import LLMProvider
from botcircuits.types import Message
from botcircuits.agent.tools.builtins.build_workflow import (
    BUILD_DIR_NAME,
    _resolve_workflows_dir,
    build_workflow_tool,
)


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


# Single round-trip: the LLM emits the full build_workflow payload. We
# don't run the agent loop here because (a) no clarifying questions
# make sense in an unattended eval and (b) keeping it to one provider
# call makes the build step's cost predictable.
_BUILD_INSTRUCTIONS = """\
You are translating a natural-language workflow description into the
structured payload required by the `build_workflow` tool.

Reply with a SINGLE JSON object and nothing else (no prose, no code
fences). Schema:

  {
    "summary": "<one-sentence prose summary>",
    "workflow": {
      "name":        "<slug-safe identifier, ^[a-zA-Z0-9_-]+$>",
      "description": "<one-line description used as the tool's runtime description>",
      "start":       "<step id of the entry step, optional>",
      "steps": {
        "<step_id>": {
          "type":       "start" | "agentAction",
          "next":       "<step_id, optional>",
          "conditions": [
            { "condition": "<NL>", "next": "<step_id>" }
          ],
          "settings": {
            "action": "<natural-language step, agentAction only>"
          }
        }
      }
    }
  }

Rules:
  - Only step types `start` and `agentAction` are supported.
  - Every `next` must point at a defined step id.
  - Branching lives on `agentAction` via a step-root `conditions`
    list (sibling of `type` and `next`, NOT nested inside `settings`).
    Do NOT add a separate `choice` step.
  - Do NOT include `expCondition`, `choices`, or `variables` — those
    are derived by the indexer.
"""


class InlineBuildError(RuntimeError):
    """Raised when the inline build pipeline can't produce a runnable
    workflow (LLM reply unparseable, build_workflow rejected it, etc.)."""


def _extract_json(reply: str) -> dict | None:
    if not reply:
        return None
    m = _JSON_BLOCK_RE.search(reply)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


async def generate_build_payload(
    spec: str,
    provider: LLMProvider,
    *,
    max_tokens: int = 8000,
) -> dict:
    """Ask the LLM for the structured `build_workflow` payload.

    Raises `InlineBuildError` on unparseable / malformed replies so the
    harness can record the failure on the case and move on.
    """
    messages = [Message(role="user", blocks=[{
        "type": "text",
        "text": f"Workflow description:\n{spec}",
    }])]
    resp = await provider.complete(
        system=_BUILD_INSTRUCTIONS,
        messages=messages,
        tools=[],
        hosted_mcp=[],
        skills=[],
        max_tokens=max_tokens,
    )
    parsed = _extract_json(resp.text or "")
    if not parsed:
        raise InlineBuildError(
            f"LLM did not return a parseable JSON payload. Raw reply: "
            f"{(resp.text or '')[:400]}"
        )
    if not isinstance(parsed.get("workflow"), dict):
        raise InlineBuildError("`workflow` missing from build payload")
    if not isinstance(parsed.get("summary"), str):
        # The build_workflow handler requires a non-empty summary; fall
        # back to a generic one rather than failing the case for a
        # cosmetic field.
        parsed["summary"] = (
            f"Inline-built workflow {parsed['workflow'].get('name', '?')}"
        )
    return parsed


async def build_inline_workflow(
    spec: str,
    provider: LLMProvider,
) -> str:
    """End-to-end: NL spec -> indexed workflow on disk. Returns the
    workflow name.

    Uses `build_workflow_tool(provider=..., auto=True)` so the same
    code path the CLI's `/workflow add` runs is exercised here, just
    without the y/N gate. The tool both writes the raw source and runs
    the indexer, producing the runnable build artifact eval needs.
    """
    payload = await generate_build_payload(spec, provider)
    tool = build_workflow_tool(provider=provider, auto=True)

    result = await tool.handler(payload)
    if not isinstance(result, dict):
        raise InlineBuildError(f"build_workflow returned non-dict: {result!r}")
    if result.get("error"):
        raise InlineBuildError(f"build_workflow rejected payload: {result['error']}")
    if result.get("denied"):
        # Should be impossible with auto=True, but guard anyway.
        raise InlineBuildError("build_workflow denied the payload (unexpected with auto=True)")

    # `build_workflow` writes the raw source BEFORE running the
    # indexer. When the indexer fails the source is left on disk —
    # that's right for the CLI use case (user retries the indexer)
    # but wrong for the eval where the dataset owns the lifetime of
    # the file. Tear it down before raising so we don't leak a
    # partial workflow into the project.
    name = result.get("workflow_name")
    if not result.get("indexed"):
        err = result.get("index_error") or result.get("index_note") or "indexer skipped"
        if isinstance(name, str):
            cleanup_inline_workflow(name)
        raise InlineBuildError(f"workflow built but not indexed: {err}")

    if not isinstance(name, str):
        raise InlineBuildError(f"build_workflow returned no workflow_name: {result!r}")
    return name


def cleanup_inline_workflow(name: str) -> list[str]:
    """Remove the source and build artifacts written by
    `build_inline_workflow`. Returns the list of paths actually
    removed. Idempotent — missing files are skipped silently so the
    cleanup is safe to call from a `finally` block even when the build
    half-failed.
    """
    directory = _resolve_workflows_dir()
    build_dir = directory / BUILD_DIR_NAME
    removed: list[str] = []
    for path in (directory / f"{name}.json", build_dir / f"{name}.json"):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
            removed.append(str(path))
    return removed
