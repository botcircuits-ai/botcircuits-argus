"""`build_workflow` — author a workflow JSON file from natural language.

The model collects the user's intent in plain text (asking follow-up
questions when scope is ambiguous), then calls this tool ONCE with a
structured `workflow` payload describing the steps. The tool:

  1. Validates the payload against the engine's supported shape
     (`start`, `agentAction`, `question`, and `systemAction` step types;
     branching lives on the step via `conditions`. `systemAction` is
     non-pausing engine-side bookkeeping — no LLM round-trip).
  2. Renders a confirmation block — "Workflow: <summary>\\nSteps: <list>"
     — and gates the write behind a single y/N answer (unless `auto`).
  3. Writes the workflow JSON to `$BOTCIRCUITS_WORKFLOWS_DIR` (or
     `.botcircuits/workflows/`).
  4. Runs the same condition indexer the CLI's `workflow build` command
     uses, so the file lands ready-to-run with `expCondition` strings
     and an aggregated `flow.variables` list.

The tool registers a closure capturing the `LLMProvider` used for
indexing; without it the file is still written but indexing is skipped
(the resulting file then has no `choices`/`variables` derived from
natural-language `conditions`).

Schema accepted from the model is intentionally close to the on-disk
file format so the LLM can reason about it directly:

    {
      "name":        "<slug-safe identifier; doubles as filename + tool name>",
      "description": "<one-line description of when to run it>",
      "summary":     "<short prose summary used in the confirm block>",
      "steps": {
        "<step_id>": {
          "type":       "start" | "agentAction" | "question" | "systemAction",
          "next":       "<step_id>",          # optional, control flow
          "conditions": [                       # optional, control flow
            { "condition": "<NL>", "next": "<step_id>" }
          ],
          "settings": {
            "action": "<NL step (agentAction); the question to ask the "
                      "user (question)>"
          }
        }
      },
      "start": "<step_id>"             # optional; defaults to first
    }

`conditions` lives at the step root next to `type` and `next` because
it describes *where to go next* — that's control flow, not the
step-type-specific payload that belongs inside `settings`.
"""

from __future__ import annotations

import copy
import inspect
import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Union

from botcircuits.agent.tools.registry import LocalTool, ToolRegistry
from botcircuits.agent.tools.builtins import _confirm

if TYPE_CHECKING:
    from botcircuits.providers.base import LLMProvider


# Callback fired after a workflow is successfully built (raw source +
# indexed build artifact both on disk). The CLI uses it to refresh the
# agent's tool registry so the new/edited workflow is callable on the
# very next turn, without a restart. Sync and async callbacks are both
# accepted; both shapes get awaited transparently.
OnBuiltCallback = Callable[[dict], Union[None, Awaitable[None]]]


SUPPORTED_STEP_TYPES = {
    "start", "agentAction", "question", "systemAction", "listDecision",
}

# Step types that carry a natural-language `settings.action` and may
# branch via `conditions`. `question` behaves like `agentAction` for
# authoring/validation; the engine routes it through `human_feedback`.
# `systemAction` carries the same shape but never pauses — the engine
# records its action text as an audit note and (if it has conditions)
# branches immediately on already-filled slots; use it for bookkeeping
# steps that need no model intelligence.
_ACTION_STEP_TYPES = {"agentAction", "question", "systemAction"}

WORKFLOWS_DIR_ENV = "BOTCIRCUITS_WORKFLOWS_DIR"
DEFAULT_WORKFLOWS_DIR = ".botcircuits/workflows"
# Sub-directory under the workflows dir that holds indexed, runnable
# workflow JSON. Mirrors `agent.workflow.local.BUILD_DIR_NAME`; kept
# inline here to avoid a cross-package import for one constant.
BUILD_DIR_NAME = ".build"

# Slug-safe identifier: doubles as filename and as the tool name surfaced
# to the LLM, so it must match the strictest provider's tool-name regex.
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _resolve_workflows_dir() -> Path:
    raw = os.getenv(WORKFLOWS_DIR_ENV) or DEFAULT_WORKFLOWS_DIR
    return Path(raw).expanduser().resolve()


def _validate_workflow(workflow: dict) -> str | None:
    """Return an error message if `workflow` is malformed, else None.

    Checks are limited to what the engine cares about: name is slug-safe,
    step types are supported, every `next` points at a known step.
    """
    if not isinstance(workflow, dict):
        return "`workflow` must be an object"

    wf_name = workflow.get("name")
    if not isinstance(wf_name, str) or not wf_name.strip():
        return "`workflow.name` must be a non-empty string"
    if not _NAME_RE.match(wf_name):
        return (
            f"`workflow.name` {wf_name!r} must match "
            f"{_NAME_RE.pattern!r} (letters, digits, underscore, hyphen)"
        )

    steps = workflow.get("steps")
    if not isinstance(steps, dict) or not steps:
        return "`workflow.steps` must be a non-empty object"

    start = workflow.get("start")
    if start is not None and start not in steps:
        return (
            f"`workflow.start` {start!r} does not match any step id"
        )

    for sid, step in steps.items():
        if not isinstance(step, dict):
            return f"step {sid!r} must be an object"
        stype = step.get("type")
        if stype not in SUPPORTED_STEP_TYPES:
            return (
                f"step {sid!r} has unsupported type {stype!r}; "
                f"supported types: {sorted(SUPPORTED_STEP_TYPES)}"
            )
        nxt = step.get("next")
        if nxt is not None and nxt not in steps:
            return (
                f"step {sid!r}.next = {nxt!r} does not match any step id"
            )

        sc = step.get("settings") or {}
        if not isinstance(sc, dict):
            return f"step {sid!r}.settings must be an object"
        if stype in _ACTION_STEP_TYPES:
            action = sc.get("action")
            if not isinstance(action, str) or not action.strip():
                return (
                    f"step {sid!r} is a {stype} but has no `action` "
                    f"text in settings"
                )
            for i, cond in enumerate(step.get("conditions") or []):
                if not isinstance(cond, dict):
                    return (
                        f"step {sid!r}.conditions[{i}] must be an object"
                    )
                if not isinstance(cond.get("condition"), str):
                    return (
                        f"step {sid!r}.conditions[{i}].condition must be a "
                        f"string"
                    )
                cn = cond.get("next")
                if cn is not None and cn not in steps:
                    return (
                        f"step {sid!r}.conditions[{i}].next = {cn!r} does "
                        f"not match any step id"
                    )
    return None


def _build_file_record(workflow: dict) -> dict:
    """Translate the input schema into the on-disk file shape the local
    workflow loader expects (`flow`)."""
    steps = workflow["steps"]
    start = workflow.get("start") or next(iter(steps))

    flow: dict[str, Any] = {
        "start": start,
        "steps": steps,
    }

    name = workflow["name"]
    record: dict[str, Any] = {
        "name": name,
        "description": (
            workflow.get("description")
            or f"Local workflow {name}."
        ),
        "flow": flow,
    }
    return record


def _step_summary(workflow: dict) -> list[str]:
    """Render one bullet per step for the confirmation block."""
    steps = workflow["steps"]
    start = workflow.get("start") or next(iter(steps))

    # Walk in `next` order starting from `start` so the user sees flow
    # rather than dict-insertion order. Anything unreachable is appended
    # at the end so it can't silently disappear from the preview.
    visited: list[str] = []
    seen: set[str] = set()
    cur: str | None = start
    while cur and cur in steps and cur not in seen:
        visited.append(cur)
        seen.add(cur)
        cur = steps[cur].get("next")
    for sid in steps:
        if sid not in seen:
            visited.append(sid)

    bullets: list[str] = []
    for idx, sid in enumerate(visited, 1):
        step = steps[sid]
        stype = step.get("type")
        sc = step.get("settings") or {}
        if stype == "start":
            label = "(start)"
        else:
            label = sc.get("action") or "(no action)"
        bullets.append(f"  {idx}. [{sid}] {label}")
        for cond in step.get("conditions") or []:
            bullets.append(
                f"       ↳ if {cond.get('condition', '')!r} → "
                f"{cond.get('next', '')}"
            )
    return bullets


def build_workflow_tool(
    *,
    provider: LLMProvider | None = None,
    auto: bool = False,
    on_built: OnBuiltCallback | None = None,
) -> LocalTool:
    """Factory returning a `LocalTool` that authors workflow JSON files.

    `provider` enables the condition indexer (NL → expressions +
    variables). Without it the file is still written, but `conditions`
    won't be converted into runnable `choices`; the model will need to
    run `botcircuits-cli workflow build --name=<name>` separately.

    `on_built` is fired exactly once per successful build (raw source +
    indexed artifact both on disk). The CLI uses it to re-register
    workflow tools on the live `ToolRegistry`, so a freshly authored or
    edited workflow becomes callable on the agent's next turn without a
    restart. Callback failures are caught and logged into the tool
    result as `refresh_error` rather than raising — a missed refresh
    must not fail the write.
    """
    effective_auto = _confirm.effective_auto(auto)

    async def _handler(args: dict) -> dict:
        workflow = args.get("workflow")
        summary = args.get("summary") or ""
        if not isinstance(summary, str) or not summary.strip():
            return {"error": "`summary` must be a non-empty string"}
        err = _validate_workflow(workflow if isinstance(workflow, dict) else {})
        if err:
            return {"error": err}

        raw_record = _build_file_record(workflow)  # type: ignore[arg-type]

        directory = _resolve_workflows_dir()
        build_directory = directory / BUILD_DIR_NAME
        source_path = directory / f"{raw_record['name']}.json"
        build_path = build_directory / f"{raw_record['name']}.json"
        existed = source_path.exists()

        # Build the confirm block: "Workflow: <summary>\nSteps: <list>"
        lines = [
            f"Workflow: {summary.strip()}",
            "Steps:",
            *_step_summary(workflow),  # type: ignore[arg-type]
            "",
            f"source: {source_path}",
            f"built:  {build_path}",
            f"action: {'overwrite' if existed else 'create'}",
        ]
        if effective_auto:
            _confirm.warn("build_workflow writing:", lines)
        else:
            allowed = await _confirm.confirm(
                "build_workflow proposes:", lines,
                prompt="create workflow? [y/N]: ",
            )
            if not allowed:
                return {
                    "denied": True,
                    "workflow_name": raw_record["name"],
                    "message": (
                        "User did not approve the workflow. Stop and ask "
                        "what to change — do not retry with a slightly "
                        "different workflow."
                    ),
                }

        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {
                "error": (
                    f"Failed to create workflows dir {directory}: "
                    f"{type(e).__name__}: {e}"
                )
            }

        # Write the raw, un-indexed source first so the editable copy
        # always lands on disk even if indexing fails below.
        try:
            with source_path.open("w", encoding="utf-8") as f:
                json.dump(raw_record, f, indent=2, ensure_ascii=False)
                f.write("\n")
        except OSError as e:
            return {
                "error": (
                    f"Failed to write {source_path}: "
                    f"{type(e).__name__}: {e}"
                )
            }

        # Index a deep copy so the raw record we just wrote stays
        # un-mutated; the indexer rewrites flow in place.
        index_summary: dict[str, Any] | None = None
        index_error: str | None = None
        built_record = copy.deepcopy(raw_record)
        if provider is not None:
            # Imported lazily — the condition_processor module pulls in
            # provider code; importing at module top would re-trigger
            # the agent/providers circular import this builtin lives in.
            from ...workflow.condition_processor import (
                generate_expressions_and_variables,
            )
            from ...workflow.engine.segments import compute_segments
            try:
                index_summary = await generate_expressions_and_variables(
                    built_record["flow"], provider
                )
                # Derive branch-delimited segments AFTER indexing so the
                # `choices` the indexer emits are present. The engine
                # runner batches each segment into one LLM call.
                built_record["flow"]["segments"] = compute_segments(
                    built_record["flow"]
                )
            except Exception as e:
                index_error = f"{type(e).__name__}: {e}"

        # Only write the build artifact when indexing actually ran and
        # succeeded. Without a provider, or after an indexer failure,
        # `.build/` would otherwise hold an un-indexed copy that
        # masquerades as runnable.
        built_written = False
        if index_summary is not None:
            try:
                build_directory.mkdir(parents=True, exist_ok=True)
                with build_path.open("w", encoding="utf-8") as f:
                    json.dump(built_record, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                built_written = True
            except OSError as e:
                index_error = (
                    f"failed to write build artifact {build_path}: "
                    f"{type(e).__name__}: {e}"
                )

        # Notify the host (CLI/gateway) that a runnable build artifact
        # was just produced. The hook re-registers workflow tools so the
        # agent can call the new/edited workflow on its next turn
        # without a restart. Indexer-only failures skip the callback —
        # there's no runnable build to expose.
        refresh_error: str | None = None
        if built_written and on_built is not None:
            try:
                ret = on_built({
                    "workflow_name": raw_record["name"],
                    "source_path": str(source_path),
                    "build_path": str(build_path),
                    "created": not existed,
                })
                if inspect.isawaitable(ret):
                    await ret
            except Exception as e:
                refresh_error = f"{type(e).__name__}: {e}"

        # Static token footprint: how many tokens the workflow DEFINITION
        # occupies (its raw JSON source and, when built, the runnable
        # artifact). This is a size/context-cost estimate, NOT tokens billed
        # by the indexer's LLM calls. Counted with the tokenizer for the
        # provider that authored it so the number matches whoever will read it
        # at runtime (Claude under claude-code, GPT under codex, heuristic
        # otherwise). Best-effort — a counting hiccup never fails the write.
        token_usage: dict[str, Any] | None = None
        try:
            from botcircuits.usage.token_counter import token_footprint

            token_usage = token_footprint(
                raw=raw_record,
                built=built_record if built_written else None,
                provider=getattr(provider, "name", None),
            )
        except Exception:
            token_usage = None

        result: dict[str, Any] = {
            "ok": True,
            "workflow_name": raw_record["name"],
            "path": str(source_path),
            "source_path": str(source_path),
            "build_path": str(build_path) if built_written else None,
            "created": not existed,
            "message": (
                f"Workflow '{raw_record['name']}' "
                f"{'created' if not existed else 'updated'} at {source_path}."
            ),
        }
        if token_usage is not None:
            result["token_usage"] = token_usage
        if refresh_error:
            result["refresh_error"] = (
                f"Workflow tool registry refresh failed: {refresh_error}. "
                f"The file was written successfully but the agent may need "
                f"a restart before it can call this workflow."
            )
        if built_written:
            result["indexed"] = True
            result["index_summary"] = index_summary
        else:
            result["indexed"] = False
            if index_error:
                result["index_error"] = (
                    f"Indexer failed: {index_error}. The raw workflow "
                    f"source was written but no build artifact was "
                    f"produced. Run "
                    f"`botcircuits-cli workflow build "
                    f"--name={raw_record['name']}` to retry."
                )
            elif provider is None:
                result["index_note"] = (
                    "No LLM provider available to the tool; conditions "
                    "were NOT converted to expressions and no build "
                    "artifact was written. Run "
                    f"`botcircuits-cli workflow build "
                    f"--name={raw_record['name']}` to build manually."
                )
        return result

    gate = (
        "Auto mode: the workflow is shown as a warning and written "
        "without prompting. "
        if effective_auto else
        "The user is prompted y/N with the workflow summary + steps. "
        "On denial, stop and ask what to change — do not retry with a "
        "slightly different workflow. "
    )
    return LocalTool(
        name="build_workflow",
        description=(
            "Author a BotCircuits workflow JSON file under "
            "`.botcircuits/workflows/` (raw source) plus a built copy "
            "under `.botcircuits/workflows/.build/` (runnable). Use this "
            "whenever the user asks to CREATE a new workflow or UPDATE "
            "an existing one (the tool overwrites by name). Do NOT "
            "hand-edit workflow JSON with write_file/edit_file — this "
            "tool also runs the condition indexer that converts "
            "natural-language branches into runnable expressions, which "
            "write_file cannot do. The agent runtime only loads "
            "workflows from `.build/`, so a workflow that hasn't been "
            "built isn't callable.\n\n"
            "Flow:\n"
            "  1. Collect the user's intent in plain text. Ask one focused "
            "round of follow-up questions if scope, branching, inputs, or "
            "the steps the agent should perform are ambiguous.\n"
            "  2. Call this tool ONCE with `summary` (one-sentence prose) "
            "and `workflow` (name + steps map).\n"
            "  3. On the returned `denied: true`, stop and ask what to "
            "change. On success, report the returned `path` to the user; "
            "if the result carries `index_error` or `index_note`, also "
            "tell the user to run `botcircuits-cli workflow build "
            "--name=<name>` to retry building.\n\n"
            "Schema notes: the workflow engine supports step types "
            "'start' (no action, just a `next` pointer), 'agentAction' "
            "(the LLM performs `settings.action`), and 'question' (the "
            "LLM asks the user `settings.action` via the human_feedback "
            "tool and waits for their reply — use this for any step that "
            "needs input from the user). Branches live on "
            "agentAction/question via `conditions` at the step ROOT "
            "(sibling of `type`/`next`, NOT inside `settings`) — a list of "
            "{condition: <natural-language>, next: <step_id>}. The tool "
            "converts NL conditions to expressions automatically; do NOT "
            "write `expCondition`, `choices`, or `variables` yourself. "
            + gate
        ),
        input_schema={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": (
                        "One-sentence prose summary of what this workflow "
                        "does. Shown to the user in the confirmation block."
                    ),
                },
                "workflow": {
                    "type": "object",
                    "description": (
                        "Workflow definition. `name` (slug-safe: letters, "
                        "digits, _ and -) doubles as the JSON filename and "
                        "as the tool name surfaced to the LLM. `steps` "
                        "keys are step ids. Each step has type 'start', "
                        "'agentAction', or 'question'. agentAction/question "
                        "steps carry `settings.action` (natural-language "
                        "instruction, or the question to ask for a "
                        "'question' step) and optionally a step-root "
                        "`conditions` (list of {condition, next} for "
                        "branching — sibling of `type`/`next`, NOT "
                        "inside `settings`)."
                    ),
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Workflow name (snake_case, matches "
                                "`^[a-zA-Z0-9_-]+$`). Used as the JSON "
                                "filename and as the tool name."
                            ),
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "When-to-use description shown to the LLM "
                                "as the tool's description at runtime."
                            ),
                        },
                        "start": {
                            "type": "string",
                            "description": (
                                "Step id of the entry step. Defaults to "
                                "the first step in `steps`."
                            ),
                        },
                        "steps": {
                            "type": "object",
                            "description": (
                                "Map of step_id -> step. Each step has "
                                "`type` ('start', 'agentAction', or "
                                "'question'), an optional `next`, an "
                                "optional `conditions` (branching), and a "
                                "`settings`. agentAction/question steps "
                                "need `settings.action`; branching uses a "
                                "step-root `conditions` (sibling of "
                                "`type`/`next`) with entries shaped "
                                "{condition: <NL>, next: <step_id>}."
                            ),
                        },
                    },
                    "required": ["name", "steps"],
                },
            },
            "required": ["summary", "workflow"],
        },
        handler=_handler,
    )


def register(reg: ToolRegistry, **config) -> None:
    allowed = {"auto", "provider", "on_built"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(
            f"build_workflow config has unknown keys: {sorted(unknown)}. "
            f"Allowed: {sorted(allowed)}"
        )
    reg.register(build_workflow_tool(**config))
