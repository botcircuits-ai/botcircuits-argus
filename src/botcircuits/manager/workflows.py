"""Workflow source store + builder for the manager backend.

Read/write the raw, human-authored workflow JSON files under the workflows
directory (``$BOTCIRCUITS_WORKFLOWS_DIR`` or ``.botcircuits/workflows``). The
manager web edits the **source** files; building the runnable ``.build/`` copy
is a separate explicit step (see :func:`build`), mirroring the CLI split where
``workflow build`` compiles natural-language conditions into deterministic
choices.

This module is the single place that knows the on-disk layout, so the API
layer deals in plain dicts. It reuses the loader's directory resolution and
name validation so writer and reader never diverge from the engine.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from botcircuits.agent.workflow.local import (
    BUILD_DIR_NAME,
    _resolve_build_dir,
    _resolve_workflows_dir,
)

#: Same identifier regex the loader enforces — name doubles as filename and
#: as the tool name surfaced to the LLM, so it must be slug-safe.
_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class WorkflowStoreError(RuntimeError):
    """Raised for invalid input the API turns into a 4xx."""


def is_valid_name(name: str) -> bool:
    return bool(name) and bool(_NAME_RE.match(name))


def _require_name(name: str) -> str:
    if not is_valid_name(name):
        raise WorkflowStoreError(
            f"invalid workflow name {name!r}: must match {_NAME_RE.pattern} "
            "(letters, digits, underscore, hyphen — no spaces or slashes)"
        )
    return name


def _source_path(name: str) -> Path:
    """``<workflows-dir>/<name>.json`` for a validated name."""
    return _resolve_workflows_dir() / f"{name}.json"


def _read(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def _summary(name: str, doc: dict[str, Any], *, built: bool, mtime: float) -> dict[str, Any]:
    """Compact record for the list endpoint (no full flow)."""
    flow = doc.get("flow") or {}
    steps = flow.get("steps") or {}
    return {
        "name": doc.get("name") or name,
        "description": doc.get("description") or "",
        "step_count": len(steps) if isinstance(steps, dict) else 0,
        "built": built,
        "updated_at": mtime,
    }


def list_workflows() -> list[dict[str, Any]]:
    """All source workflows, newest first, as compact summaries.

    ``built`` reflects whether a ``.build/<name>.json`` counterpart exists, so
    the UI can flag sources that still need a build before they're runnable.
    """
    src_dir = _resolve_workflows_dir()
    if not src_dir.is_dir():
        return []
    build_dir = _resolve_build_dir()
    built_stems = (
        {p.stem for p in build_dir.glob("*.json")} if build_dir.is_dir() else set()
    )
    out: list[dict[str, Any]] = []
    for path in src_dir.glob("*.json"):
        doc = _read(path)
        if doc is None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        out.append(
            _summary(path.stem, doc, built=path.stem in built_stems, mtime=mtime)
        )
    out.sort(key=lambda s: s.get("updated_at") or 0, reverse=True)
    return out


def get_workflow(name: str) -> dict[str, Any] | None:
    """The full raw source document for ``name``, or ``None`` if missing."""
    _require_name(name)
    return _read(_source_path(name))


def save_workflow(name: str, doc: dict[str, Any]) -> dict[str, Any]:
    """Write (create or overwrite) the raw source file for ``name``.

    The stored ``name`` field is normalized to the path name so the file and
    its identifier never drift. The flow shape is validated lightly (must have
    a ``flow`` object); deeper validation happens at build time.
    """
    _require_name(name)
    if not isinstance(doc, dict):
        raise WorkflowStoreError("workflow body must be a JSON object")

    flow = doc.get("flow")
    if not isinstance(flow, dict):
        raise WorkflowStoreError("workflow must have a `flow` object")
    if not isinstance(flow.get("steps"), dict):
        raise WorkflowStoreError("workflow `flow.steps` must be an object")

    record = dict(doc)
    record["name"] = name
    record.setdefault("description", "")

    src_dir = _resolve_workflows_dir()
    src_dir.mkdir(parents=True, exist_ok=True)
    path = _source_path(name)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return record


def is_built(name: str) -> bool:
    """Whether a runnable ``.build/<name>.json`` copy exists for ``name``."""
    _require_name(name)
    return (_resolve_build_dir() / f"{name}.json").exists()


def delete_workflow(name: str) -> bool:
    """Delete the source file and any built copy. Returns False if no source."""
    _require_name(name)
    path = _source_path(name)
    if not path.exists():
        return False
    path.unlink()
    built = _resolve_build_dir() / f"{name}.json"
    try:
        built.unlink()
    except OSError:
        pass
    return True


def build(name: str) -> dict[str, Any]:
    """Compile the source workflow into the runnable ``.build/`` copy.

    Shells out to ``botcircuits workflow build --name <name>`` (the same path
    the CLI and authoring skill use) so condition indexing / optimization /
    segmentation stay single-sourced. Returns ``{ok, stdout, stderr}``.
    """
    _require_name(name)
    if not _source_path(name).exists():
        raise WorkflowStoreError(f"no workflow source named {name!r}")
    # Call the CLI's main() in-interpreter via -c so we run in the same venv
    # as the installed package (no reliance on a console script on PATH), the
    # same approach the supervisor uses for uvicorn.
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from botcircuits.cli import main; "
            "sys.argv = ['botcircuits', 'workflow', 'build', '--name', sys.argv[1]]; "
            "main()",
            name,
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


__all__ = [
    "WorkflowStoreError",
    "is_valid_name",
    "list_workflows",
    "get_workflow",
    "save_workflow",
    "delete_workflow",
    "build",
    "BUILD_DIR_NAME",
]
