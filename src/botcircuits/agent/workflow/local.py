"""On-disk workflow loader + driver.

Two responsibilities:

  - `fetch_workflows`: discover workflow definition files on disk and
    return one record per file (each carries `name`, `description`, plus
    the full `flow` definition the engine needs).
  - `run_workflow`: drive the engine for one step, persist the paused
    session in process memory keyed by `session_id`, and return a result
    dict the workflow tool can hand back to the LLM.

Layout:

  - Raw, authored workflows live in `$BOTCIRCUITS_WORKFLOWS_DIR` (or
    `.botcircuits/workflows`). This is the source of truth the user
    edits.
  - The CLI `workflow build` command (and `build_workflow` tool) read
    those raw files, run the condition indexer, and emit the runnable
    output into the `.build/` sub-directory.
  - The agent runtime loads only from `.build/` so it always uses
    built workflows. A raw file with no `.build/` counterpart is
    skipped with a stderr warning telling the user to run
    `botcircuits-cli workflow build --name=<name>`.

`name` is the sole identifier — it doubles as the tool name surfaced to
the LLM, so it must match `^[a-zA-Z0-9_-]+$` (OpenAI's tool-name regex,
which is the strictest of the providers). The loader validates this and
defaults to the filename stem when the field is missing.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

from botcircuits.providers.base import LLMProvider
from botcircuits.agent.workflow.engine import run_flow
from botcircuits.agent.workflow.slot_resolver import (
    Missing as _Missing,
    coerce_boolean as _coerce_boolean,
    coerce_number as _coerce_number,
    coerce_string as _coerce_string,
    resolve_slots,
)
from botcircuits.agent.workflow.variable_normalizer import normalize as normalize_variables
from botcircuits.agent.workflow.variable_normalizer import variables_for_step


WORKFLOWS_DIR_ENV = "BOTCIRCUITS_WORKFLOWS_DIR"
DEFAULT_WORKFLOWS_DIR = ".botcircuits/workflows"
# Sub-directory under the workflows dir that holds the built,
# runnable workflow JSON. `workflow build` writes here; the agent
# runtime loads from here.
BUILD_DIR_NAME = ".build"

# Identifier regex for workflow names. Matches OpenAI's tool-name pattern,
# which is the strictest tool-naming surface the agent talks to.
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


# In-process saved-session store, keyed by session_id. A workflow pauses
# on an `agentAction` and we stash its currentStep + slots here so the
# next call with the same session_id can resume.
_SESSIONS: dict[str, dict[str, Any]] = {}


class LocalWorkflowError(RuntimeError):
    """Raised when a workflow file can't be loaded or run."""


def _resolve_workflows_dir() -> Path:
    """Source directory holding the raw, human-authored workflow files."""
    raw = os.getenv(WORKFLOWS_DIR_ENV) or DEFAULT_WORKFLOWS_DIR
    return Path(raw).expanduser().resolve()


def _resolve_build_dir() -> Path:
    """Indexed-workflow output directory the agent runtime reads from."""
    return _resolve_workflows_dir() / BUILD_DIR_NAME


# ---------------------------------------------------------------------------
# Layer A — deterministic type coercion against stm.variables[].dataType
# (scalar coercers live in slot_resolver.py — single source of truth shared
# with the deterministic slot resolver)
# ---------------------------------------------------------------------------


def _coerce_variables(values: dict, schema: list[dict]) -> dict:
    """Coerce `values` against the variable schema, dropping any value
    that can't be safely converted. Returns a new dict; never mutates.

    - Unknown variables (not in `schema`) pass through unchanged.
    - Coercion failures emit a stderr warning and are dropped (the
      default branch in the choice fallback handles the missing value).
    """
    if not isinstance(values, dict):
        return {}
    by_name = {
        v["variableName"]: v
        for v in (schema or [])
        if isinstance(v, dict) and isinstance(v.get("variableName"), str)
    }

    out: dict = {}
    for name, raw in values.items():
        spec = by_name.get(name)
        if spec is None:
            out[name] = raw
            continue
        dtype = (spec.get("dataType") or "string").lower()
        if dtype == "number":
            coerced = _coerce_number(raw)
        elif dtype == "boolean":
            coerced = _coerce_boolean(raw)
        else:
            coerced = _coerce_string(raw)

        if isinstance(coerced, _Missing):
            print(
                f"[workflow] dropping {name}={raw!r}: cannot coerce to {dtype}",
                file=sys.stderr,
            )
            continue
        out[name] = coerced
    return out


def _action_text_for_step(flow: dict, step_id: str) -> str:
    """Return the `action` field of the named step (the natural-language
    instruction emitted to the LLM), or "" if unavailable. Used as part
    of Layer B's source-context for the hallucination guard."""
    steps = flow.get("steps") or {}
    step = steps.get(step_id) or {}
    sc = step.get("settings") or {}
    action = sc.get("action")
    return action if isinstance(action, str) else ""


# ---------------------------------------------------------------------------
# Workflow loader + executor driver
# ---------------------------------------------------------------------------


def _validate_name(name: Any, source: str) -> str:
    """Confirm `name` is a slug-safe identifier, else raise. `source`
    appears in the error so the user knows which file/field is bad."""
    if not isinstance(name, str) or not name:
        raise LocalWorkflowError(
            f"{source}: workflow `name` must be a non-empty string"
        )
    if not _NAME_RE.match(name):
        raise LocalWorkflowError(
            f"{source}: workflow name {name!r} must match {_NAME_RE.pattern!r} "
            f"(letters, digits, underscore, hyphen — no spaces or punctuation)"
        )
    return name


async def fetch_workflows() -> list[dict]:
    """Load every built `*.json` file from the build directory.

    Reads from `<workflows-dir>/.build/`. Raw workflow files that have
    no built counterpart are skipped with a stderr warning telling the
    user to run `workflow build --name=<name>`.

    A missing build directory yields an empty list rather than erroring
    so an app can opt in by just creating the folder.
    """
    build_dir = _resolve_build_dir()
    source_dir = _resolve_workflows_dir()

    if source_dir.is_dir():
        built_stems = (
            {p.stem for p in build_dir.glob("*.json")}
            if build_dir.is_dir() else set()
        )
        for raw_path in sorted(source_dir.glob("*.json")):
            if raw_path.stem not in built_stems:
                print(
                    f"[workflow] skipping {raw_path}: no build at "
                    f"{build_dir / raw_path.name}. Run "
                    f"`botcircuits-cli workflow build --name={raw_path.stem}` "
                    f"to build it.",
                    file=sys.stderr,
                )

    if not build_dir.is_dir():
        return []

    records: list[dict] = []
    for path in sorted(build_dir.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as f:
                record = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise LocalWorkflowError(
                f"failed to load workflow {path}: {e}"
            ) from e

        if not isinstance(record, dict):
            raise LocalWorkflowError(
                f"workflow {path} must be a JSON object at the top level"
            )

        # Defaults: name falls back to the filename stem. Validate that the
        # final value is slug-safe so it's a usable tool name downstream.
        record.setdefault("name", path.stem)
        _validate_name(record["name"], str(path))
        record.setdefault("description", f"Local workflow {record['name']}.")
        records.append(record)
    return records


def _load_workflow_record(name: str) -> dict:
    """Re-read the built workflow file by `name`. The engine needs the
    full STM definition on every call; we don't keep it in memory
    between calls so edits on disk pick up without a restart.

    Reads from the build directory only — un-built raw sources are
    not runnable.

    Lookup strategy: try `<build>/<name>.json` first; if that misses,
    scan every `*.json` in the build dir and match on the record's
    `name` field. The two strategies let authors keep filenames aligned
    with names (the common case) without forcing it.
    """
    directory = _resolve_build_dir()
    direct = directory / f"{name}.json"

    if direct.exists():
        with direct.open("r", encoding="utf-8") as f:
            record = json.load(f)
        record.setdefault("name", direct.stem)
        _validate_name(record["name"], str(direct))
        return record

    if directory.is_dir():
        for path in directory.glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as f:
                    record = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(record, dict) and record.get("name") == name:
                return record
    raise LocalWorkflowError(
        f"no built workflow found with name {name!r} in {directory}. "
        f"Run `botcircuits-cli workflow build --name={name}` to build it."
    )


async def run_workflow(
    workflow_name: str,
    args: dict,
    *,
    session_id: str | None = None,
    provider: LLMProvider | None = None,
    last_assistant_message: str = "",
    last_user_message: str = "",
    normalize_enabled: bool = True,
) -> dict:
    """Drive the STM until it pauses on an agent action or finishes.

    `workflow_name` is the slug-safe identifier of the workflow record
    (its `name` field on disk, doubling as filename and tool name).

    Variable normalization runs only on RE-ENTRY into a state that's
    marked as a `pendingBranch` (i.e. the prior turn paused on an
    agentAction with conditions/choices). Initial calls and re-entries
    into non-branching states skip normalization entirely.

      - The deterministic slot resolver (`slot_resolver.resolve_slots`)
        runs first and satisfies what it can from raw args, the step's
        authored choice values, the user's last reply, and saved slots.
      - Layer B (semantic LLM extraction) runs when `provider` is given,
        `normalize_enabled` is True, and the resolver left variables
        unresolved — it sees only those leftovers. Failures fall through
        silently to raw args.
      - Layer A (deterministic type coercion) always runs against any
        variables the workflow has indexed. It runs on the resolver's
        and B's merged output.
    """
    record = _load_workflow_record(workflow_name)

    flow = record.get("flow")
    if not isinstance(flow, dict):
        raise LocalWorkflowError(
            f"workflow {workflow_name!r} is missing flow"
        )

    sid = session_id or str(uuid.uuid4())
    saved_session = _SESSIONS.get(sid)
    start_step_id = saved_session.get("currentStep") if saved_session else None

    # Normalize incoming args BEFORE merging into slots. Gated on
    # pendingBranch — we only spend an LLM call when a branch decision is
    # actually about to be evaluated.
    incoming_args = dict(args or {})
    pending = (saved_session or {}).get("pendingBranch") or {}
    pending_step_id = pending.get("stepId") if isinstance(pending, dict) else None

    if pending_step_id:
        relevant_variables = variables_for_step(flow, pending_step_id)

        # Deterministic slot resolution — satisfy as many branch
        # variables as possible without an LLM (raw args, choice-value
        # match, typed extraction, question verbatim reply, saved
        # slots). Only the leftovers go to Layer B; when nothing is
        # left, the LLM call is skipped entirely.
        unresolved = relevant_variables
        if relevant_variables:
            saved_slots = (
                (saved_session or {}).get("slots", {}).get(workflow_name, {})
            )
            resolved, unresolved = resolve_slots(
                flow=flow,
                step_id=pending_step_id,
                variables=relevant_variables,
                raw_args=incoming_args,
                saved_slots=saved_slots,
                last_user_message=last_user_message,
            )
            if resolved:
                incoming_args = {**incoming_args, **resolved}

        # Layer B — semantic LLM normalization (optional), only for
        # variables the deterministic resolver could not satisfy. Its
        # allow-list is restricted to `unresolved`, so it can never
        # override a deterministically resolved value.
        if provider is not None and normalize_enabled and unresolved:
            action_text = _action_text_for_step(flow, pending_step_id)
            extracted = await normalize_variables(
                provider=provider,
                variables=unresolved,
                raw_args=incoming_args,
                action_text=action_text,
                last_assistant_message=last_assistant_message,
                last_user_message=last_user_message,
            )
            if extracted:
                incoming_args = {**incoming_args, **extracted}

        # Layer A — type coercion (deterministic, always runs when we
        # have a schema, regardless of whether B ran).
        if relevant_variables:
            incoming_args = _coerce_variables(incoming_args, relevant_variables)

    # Merge incoming args into the slot context.
    slots: dict[str, Any] = {}
    if saved_session:
        slots.update(saved_session.get("slots", {}).get(workflow_name, {}))
    slots.update(incoming_args)

    session_context = {
        "inputText": (args or {}).get("inputText", ""),
        "sessionAttributes": {},
        "requestAttributes": args or {},
        "journeyId": workflow_name,
        "slots": slots,
        "recentSlot": None,
    }

    if saved_session is None:
        saved_session = {
            "slots": {workflow_name: slots},
            "currentStep": None,
            "runningStep": None,
        }
    else:
        saved_session.setdefault("slots", {})[workflow_name] = slots

    message = {
        "sessionId": sid,
        "messageId": str(uuid.uuid4()),
        "inputText": session_context["inputText"],
        "channel": "agent",
        "channelMetaData": {},
        "requestAttributes": args or {},
        "sessionAttributes": {},
        "data": {
            "journeyConfig": record,
            "sessionContext": session_context,
            "savedSession": saved_session,
        },
    }

    result = await run_flow(flow, message, start_step_id, workflow_name)

    data = result.get("data") or {}
    inner = data.get("message") or {}
    action = (inner.get("content") or {}).get("action")
    done = bool(inner.get("end")) or (inner == {})
    conditions = inner.get("conditions", [])
    choices = inner.get("choices", [])
    variables = inner.get("variables", [])
    # `kind` is "question" when the engine paused on a `question` step —
    # the tool wrapper uses it to force a `human_feedback` call. Plain
    # agentAction steps leave it unset.
    kind = inner.get("kind")

    # Capture the step the engine just paused on BEFORE we (maybe) drop
    # the session — callers (evaluation, debugging) want this even on
    # the terminal turn.
    paused_session = result.get("savedSession") or {}
    running_step = paused_session.get("runningStep")
    if not isinstance(running_step, str):
        running_step = None

    # If the engine paused on a branching step, surface the filtered
    # variable schema that branch references. The tool wrapper uses it
    # to (a) widen the tool's input_schema and (b) tell the model to
    # re-call the tool with those values — the model-supplied args then
    # hit the slot resolver's highest-priority source on re-entry.
    branch_variables: list[dict] = []
    pending_after = paused_session.get("pendingBranch")
    if (
        not done
        and isinstance(pending_after, dict)
        and isinstance(pending_after.get("stepId"), str)
    ):
        branch_variables = variables_for_step(flow, pending_after["stepId"])

    if done or not action:
        # Workflow finished — drop the saved session so a fresh call
        # restarts from `start`.
        _SESSIONS.pop(sid, None)
    else:
        _SESSIONS[sid] = result["savedSession"]

    return {
        "status": "ok",
        "workflow_name": workflow_name,
        "session_id": sid,
        "action": action,
        "done": done,
        "kind": kind,
        "running_step": running_step,
        "messages": [inner] if inner else [],
        "conditions": conditions,
        "choices": choices,
        "variables": variables,
        "branch_variables": branch_variables,
        # Audit notes from non-pausing systemAction steps walked this call;
        # the tool wrapper prepends them to the directive so the recorded
        # bookkeeping still reaches the transcript.
        "system_notes": result.get("systemNotes") or [],
    }
