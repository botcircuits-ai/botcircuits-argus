"""Evaluation dataset — schema, loader, validation.

A *dataset* file groups one or more *cases* that exercise the SAME
workflow. The workflow is identified at the dataset level — either by
name (referenced mode) or by natural-language spec (inline mode):

  - **Referenced**: dataset sets `workflow: "<name>"`. The named
    workflow must already be built on disk under
    `.botcircuits/workflows/.build/`. Used for regression cases against
    workflows the team maintains.
  - **Inline**: dataset sets `workflow_spec: "<natural-language>"`.
    The harness translates the spec into a structured `build_workflow`
    payload via the configured LLM provider, writes + indexes the
    workflow exactly like `/workflow add` would, runs every case in
    the dataset against it, then deletes the files. The build happens
    ONCE per dataset — not per case — so every case in the file
    exercises the same generated STM.

The prompt-only baseline always sees the same STM as the workflow
engine: in inline mode that's the LLM-generated workflow, in
referenced mode it's the on-disk file. So both runners are scored
against the same plan; the only variable is whether the engine or the
LLM is driving.

Schema:

    {
      "name":          "blog_post_create_v1",
      "description":   "optional dataset-level description",
      "workflow":      "blog_post_create",          # referenced mode
      "workflow_spec": "Create a workflow that ...", # inline mode (mutually exclusive with `workflow`)
      "cases": [
        {
          "id":           "blog_post.supported_topic",
          "description":  "user gives a topic the workflow accepts",
          "initial_args": {"blog_post_topic": "ai-agents"},
          "turns": [
            {"args": {}},
            {"args": {}}
          ],
          "expected": {
            "trace":        ["check_topic", "generate_text", "save_post"],
            "final_state":  "save_post",
            "must_contain": []
          }
        }
      ]
    }

Single-case files (no wrapping object) are also accepted as a
convenience and read as a one-case referenced-mode dataset; in that
shape the workflow name is taken from the case's `workflow` field.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


EVAL_DIR_ENV = "BOTCIRCUITS_EVAL_DIR"
DEFAULT_EVAL_DIR = ".botcircuits/evaluation"


class EvalDatasetError(ValueError):
    """Raised when a dataset file fails schema validation."""


@dataclass
class EvalTurn:
    """One re-entry into the workflow tool.

    `args` is what the LLM would have produced as tool-call arguments
    for this turn — passed verbatim into `run_workflow`. Leave empty
    `{}` when the LLM would have called the workflow with no
    pre-extracted slots (the normal pattern when the previous step
    just asked the user a question).

    `user_text` is the simulated user reply that just arrived in chat,
    in natural language. The harness threads it into `run_workflow`'s
    `last_user_message`, which is what Layer B (LLM-driven variable
    normalization) extracts slot values from. This is how
    natural-phrasing test cases stress slot extraction the same way
    production does.
    """
    args: dict[str, Any] = field(default_factory=dict)
    user_text: str = ""


@dataclass
class EvalExpected:
    trace: list[str] = field(default_factory=list)
    final_state: str | None = None
    must_contain: list[str] = field(default_factory=list)


@dataclass
class EvalCase:
    id: str
    # Runtime workflow name. Filled by the loader for referenced-mode
    # datasets and by the harness (post-build) for inline-mode ones.
    workflow: str
    description: str
    initial_args: dict[str, Any]
    turns: list[EvalTurn]
    expected: EvalExpected
    # Natural-language opening user message for the very first call.
    # Used the same way as `EvalTurn.user_text`: threaded into
    # `last_user_message` so Layer B can extract slot values from a
    # raw chat reply instead of pre-named args.
    initial_user_text: str = ""
    # Source file the case was loaded from — preserved for reporting.
    source: str = ""


@dataclass
class EvalDataset:
    """One dataset file: a workflow (referenced OR inline) plus cases.

    Exactly one of `workflow` or `workflow_spec` is set. In referenced
    mode `workflow` names a pre-built workflow; in inline mode
    `workflow_spec` carries the natural-language description and
    `workflow` stays empty until the harness has built + named the
    workflow at eval time.
    """
    name: str
    cases: list[EvalCase]
    workflow: str = ""
    workflow_spec: str = ""
    description: str = ""
    source: str = ""

    @property
    def is_inline(self) -> bool:
        """True only when the dataset needs the harness to BUILD the
        workflow from `workflow_spec`. If `workflow` is also set the
        spec is just kept around for the no-workflow baseline's
        system prompt; no build happens."""
        return bool(self.workflow_spec) and not bool(self.workflow)


def resolve_eval_dir() -> Path:
    raw = os.getenv(EVAL_DIR_ENV) or DEFAULT_EVAL_DIR
    return Path(raw).expanduser().resolve()


def _as_dict(obj: Any, where: str) -> dict:
    if not isinstance(obj, dict):
        raise EvalDatasetError(f"{where}: expected object, got {type(obj).__name__}")
    return obj


def _as_str(obj: Any, where: str) -> str:
    if not isinstance(obj, str) or not obj:
        raise EvalDatasetError(f"{where}: expected non-empty string")
    return obj


def _parse_case(
    raw: Any,
    source: str,
    idx: int,
    *,
    require_workflow: bool,
) -> EvalCase:
    """Parse one case object.

    `require_workflow` toggles whether a per-case `workflow` field is
    mandatory. For dataset-shape files (with a dataset-level workflow
    or workflow_spec) it's False — the harness propagates the
    dataset's workflow name onto each case. For the bare single-case
    shape it's True since there's nowhere else to read the name from.
    """
    where = f"{source}:cases[{idx}]"
    case = _as_dict(raw, where)
    id_ = _as_str(case.get("id"), f"{where}.id")

    workflow_raw = case.get("workflow")
    if require_workflow:
        workflow = _as_str(workflow_raw, f"{where}.workflow")
    elif workflow_raw is None:
        workflow = ""
    elif isinstance(workflow_raw, str):
        workflow = workflow_raw
    else:
        raise EvalDatasetError(f"{where}.workflow must be a string")

    description = case.get("description") or ""

    initial_args = case.get("initial_args") or {}
    if not isinstance(initial_args, dict):
        raise EvalDatasetError(f"{where}.initial_args must be an object")
    initial_user_text = case.get("initial_user_text") or ""
    if not isinstance(initial_user_text, str):
        raise EvalDatasetError(
            f"{where}.initial_user_text must be a string when present"
        )

    turns_raw = case.get("turns") or []
    if not isinstance(turns_raw, list):
        raise EvalDatasetError(f"{where}.turns must be an array")
    turns: list[EvalTurn] = []
    for t_idx, t in enumerate(turns_raw):
        t_obj = _as_dict(t, f"{where}.turns[{t_idx}]")
        args = t_obj.get("args") or {}
        if not isinstance(args, dict):
            raise EvalDatasetError(
                f"{where}.turns[{t_idx}].args must be an object"
            )
        user_text = t_obj.get("user_text") or ""
        if not isinstance(user_text, str):
            raise EvalDatasetError(
                f"{where}.turns[{t_idx}].user_text must be a string"
            )
        turns.append(EvalTurn(args=args, user_text=user_text))

    exp_raw = _as_dict(case.get("expected") or {}, f"{where}.expected")
    trace = exp_raw.get("trace") or []
    if not isinstance(trace, list) or not all(isinstance(s, str) for s in trace):
        raise EvalDatasetError(f"{where}.expected.trace must be an array of strings")
    final_state = exp_raw.get("final_state")
    if final_state is not None and not isinstance(final_state, str):
        raise EvalDatasetError(f"{where}.expected.final_state must be a string")
    must = exp_raw.get("must_contain") or []
    if not isinstance(must, list) or not all(isinstance(s, str) for s in must):
        raise EvalDatasetError(
            f"{where}.expected.must_contain must be an array of strings"
        )

    return EvalCase(
        id=id_,
        workflow=workflow,
        description=description,
        initial_args=initial_args,
        initial_user_text=initial_user_text,
        turns=turns,
        expected=EvalExpected(
            trace=trace,
            final_state=final_state,
            must_contain=must,
        ),
        source=source,
    )


def _parse_dataset(data: dict, source: str) -> EvalDataset:
    """Parse the dataset-shape JSON object."""
    name = data.get("name")
    if not isinstance(name, str) or not name:
        # Default to the filename stem so callers always have something
        # to display in reports.
        name = Path(source).stem
    description = data.get("description") or ""

    workflow_raw = data.get("workflow")
    spec_raw = data.get("workflow_spec") or ""
    if workflow_raw is not None and not isinstance(workflow_raw, str):
        raise EvalDatasetError(f"{source}.workflow must be a string")
    if not isinstance(spec_raw, str):
        raise EvalDatasetError(f"{source}.workflow_spec must be a string")
    if not workflow_raw and not spec_raw:
        raise EvalDatasetError(
            f"{source}: dataset must set `workflow` (referenced mode) "
            f"or `workflow_spec` (inline mode)"
        )
    # `workflow` + `workflow_spec` together is a legitimate combo:
    # the workflow exists on disk (referenced mode) AND the spec is
    # retained so the no-workflow baseline can inject it as system
    # instructions. The inline build step is skipped when `workflow`
    # is set; the spec is used only for the baseline prompt.

    cases_raw = data.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise EvalDatasetError(f"{source}.cases must be a non-empty array")

    # Inline mode: each case's `workflow` is filled by the harness
    # post-build. Referenced mode: propagate the dataset-level name
    # onto each case so the runners can read case.workflow uniformly.
    cases: list[EvalCase] = []
    for i, c in enumerate(cases_raw):
        case = _parse_case(c, source, i, require_workflow=False)
        if workflow_raw and not case.workflow:
            case.workflow = workflow_raw
        cases.append(case)

    return EvalDataset(
        name=name,
        description=description,
        workflow=workflow_raw or "",
        workflow_spec=spec_raw,
        cases=cases,
        source=source,
    )


def load_dataset(path: Path) -> EvalDataset:
    """Load a single dataset file."""
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise EvalDatasetError(f"{path}: failed to load: {e}") from e

    source = str(path)
    if not isinstance(data, dict):
        raise EvalDatasetError(f"{source}: top-level must be a JSON object")

    if "cases" in data:
        return _parse_dataset(data, source)

    # Bare single-case convenience shape — the case carries its own
    # `workflow` and we wrap it into a one-case referenced-mode dataset.
    # Inline mode is not allowed in this shape because workflow_spec
    # belongs at the dataset level by definition.
    if "id" in data and "workflow" in data:
        case = _parse_case(data, source, 0, require_workflow=True)
        return EvalDataset(
            name=Path(source).stem,
            workflow=case.workflow,
            cases=[case],
            source=source,
        )
    raise EvalDatasetError(
        f"{source}: top-level must be a dataset object "
        f"({{workflow|workflow_spec, cases: [...]}}) or a bare single case"
    )


def discover_datasets(directory: Path | None = None) -> list[EvalDataset]:
    """Load every `*.json` file under the eval directory."""
    directory = directory or resolve_eval_dir()
    if not directory.is_dir():
        return []
    return [load_dataset(p) for p in sorted(directory.glob("*.json"))]


# ---------------------------------------------------------------------------
# Backwards-compatible helpers — kept so callers that still think in
# "list of cases" terms don't break. New code should prefer
# load_dataset / discover_datasets.
# ---------------------------------------------------------------------------


def load_cases(path: Path) -> list[EvalCase]:
    return load_dataset(path).cases


def discover_cases(directory: Path | None = None) -> list[EvalCase]:
    out: list[EvalCase] = []
    for ds in discover_datasets(directory):
        out.extend(ds.cases)
    return out
