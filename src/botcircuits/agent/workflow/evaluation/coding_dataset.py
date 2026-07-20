"""Coding-eval dataset: tasks with OBJECTIVE ground truth.

Unlike the deterministic-workflow datasets (`dataset.py`), a coding case has
no fixed trace to match — "did it work" is decided by running the project's
tests. Each case names a **fixture repo** (a small self-contained project
committed under the eval dir) that is copied into a throwaway sandbox per run;
a correct change turns the `test_command` GREEN without breaking the
`guard_command`.

Layout:

    <eval-dir>/coding/
      coding_tasks.json           # the dataset (this schema)
      fixtures/<fixture>/...       # one seed repo per fixture

Case schema:

    {
      "id": "coding.add_slugify",
      "fixture": "slugify_util",          # dir under coding/fixtures/
      "prompt": "<the coding request handed to the agent>",
      "goal": "<plain-language definition of done, for the LLM judge>",
      "test_command": "python -m pytest -q tests/test_slugify.py",
      "guard_command": "python -m pytest -q tests/test_shout.py",  # optional
      "category": "add-feature | fix-bug | refactor",              # optional
      "expected": {"tests_pass": true}
    }

`test_command` is the TARGET (starts red, correct change makes it green).
`guard_command` (optional) is the pre-existing behavior that must STAY green —
run separately so a collection error in the target file can't mask it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from botcircuits.agent.workflow.evaluation.dataset import (
    EvalDatasetError,
    resolve_eval_dir,
)

#: Sub-directory of the eval dir holding coding datasets + their fixtures.
CODING_SUBDIR = "coding"
FIXTURES_SUBDIR = "fixtures"


@dataclass
class CodingCase:
    id: str
    fixture: str
    prompt: str
    goal: str = ""
    test_command: str = "python -m pytest -q"
    guard_command: str | None = None
    category: str = ""
    expected_tests_pass: bool = True
    source: str = ""


@dataclass
class CodingDataset:
    name: str
    cases: list[CodingCase] = field(default_factory=list)
    description: str = ""
    source: str = ""
    #: Absolute path to the coding/fixtures directory the cases copy from.
    fixtures_dir: Path | None = None


def _coding_dir() -> Path:
    return resolve_eval_dir() / CODING_SUBDIR


def _parse_case(raw: dict, source: str) -> CodingCase:
    if not isinstance(raw, dict):
        raise EvalDatasetError(f"{source}: each case must be a JSON object")
    cid = raw.get("id")
    if not isinstance(cid, str) or not cid:
        raise EvalDatasetError(f"{source}: a case is missing a string `id`")
    fixture = raw.get("fixture")
    if not isinstance(fixture, str) or not fixture:
        raise EvalDatasetError(f"{source}: case {cid!r} needs a `fixture` name")
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise EvalDatasetError(f"{source}: case {cid!r} needs a `prompt`")
    expected = raw.get("expected") or {}
    return CodingCase(
        id=cid,
        fixture=fixture,
        prompt=prompt,
        goal=raw.get("goal") or "",
        test_command=raw.get("test_command") or "python -m pytest -q",
        guard_command=raw.get("guard_command") or None,
        category=raw.get("category") or "",
        expected_tests_pass=bool(expected.get("tests_pass", True)),
        source=source,
    )


def load_coding_dataset(path: Path) -> CodingDataset:
    """Parse one coding dataset JSON file. Its fixtures are resolved relative
    to the file's own `coding/` directory (so a dataset moved with its
    fixtures stays self-contained)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise EvalDatasetError(f"failed to load coding dataset {path}: {e}") from e
    if not isinstance(raw, dict):
        raise EvalDatasetError(f"{path}: dataset must be a JSON object")
    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise EvalDatasetError(f"{path}: dataset needs a non-empty `cases` list")

    cases = [_parse_case(c, str(path)) for c in cases_raw]
    fixtures_dir = path.parent / FIXTURES_SUBDIR
    return CodingDataset(
        name=raw.get("name") or path.stem,
        cases=cases,
        description=raw.get("description") or "",
        source=str(path),
        fixtures_dir=fixtures_dir,
    )


def discover_coding_datasets(directory: Path | None = None) -> list[CodingDataset]:
    """Load every `*.json` coding dataset under `<eval-dir>/coding/` (the
    `fixtures/` subdir is skipped). Returns [] when the directory is absent."""
    base = directory or _coding_dir()
    if not base.is_dir():
        return []
    out: list[CodingDataset] = []
    for path in sorted(base.glob("*.json")):
        out.append(load_coding_dataset(path))
    return out


def fixture_path(dataset: CodingDataset, case: CodingCase) -> Path:
    """Absolute path to the case's seed fixture repo."""
    base = dataset.fixtures_dir or (_coding_dir() / FIXTURES_SUBDIR)
    return base / case.fixture


__all__ = [
    "CodingCase",
    "CodingDataset",
    "CODING_SUBDIR",
    "load_coding_dataset",
    "discover_coding_datasets",
    "fixture_path",
]
