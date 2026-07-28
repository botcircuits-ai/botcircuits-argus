"""Single source of truth for every path under the workflows tree.

Layout:

    <workflows-dir>/
      <name>.json                          # raw, human-authored source
      .build/
        <name>/
          <name>.json                      # built/runnable artifact
          verifications/
            gate.json                       # verification-gate manifest
            checks/
              <check_id>.py                 # generated deterministic checks

Every module that needs one of these paths (the loader, the build_workflow
tool, the CLI, the manager backend, the evaluation harness) imports the
helpers here instead of re-deriving `BUILD_DIR_NAME` / joining paths
inline — that duplication is exactly what caused `local.py` and
`build_workflow.py` to independently define the same constant.
"""

from __future__ import annotations

import os
from pathlib import Path


WORKFLOWS_DIR_ENV = "BOTCIRCUITS_WORKFLOWS_DIR"
DEFAULT_WORKFLOWS_DIR = ".botcircuits/workflows"

# Sub-directory under the workflows dir that holds built, runnable
# workflow output — one sub-directory per workflow (see module docstring).
BUILD_DIR_NAME = ".build"

# Sub-directory under a workflow's build folder holding its verification
# gate (manifest + generated check scripts).
VERIFICATIONS_DIR_NAME = "verifications"
GATE_MANIFEST_NAME = "gate.json"
CHECKS_DIR_NAME = "checks"


def resolve_workflows_dir() -> Path:
    """Source directory holding the raw, human-authored workflow files."""
    raw = os.getenv(WORKFLOWS_DIR_ENV) or DEFAULT_WORKFLOWS_DIR
    return Path(raw).expanduser().resolve()


def resolve_build_dir() -> Path:
    """`<workflows-dir>/.build` — parent of every workflow's build folder."""
    return resolve_workflows_dir() / BUILD_DIR_NAME


def build_dir_for(name: str) -> Path:
    """`<workflows-dir>/.build/<name>/` — this workflow's build folder."""
    return resolve_build_dir() / name


def build_json_path(name: str) -> Path:
    """`<workflows-dir>/.build/<name>/<name>.json` — the built, runnable
    workflow record."""
    return build_dir_for(name) / f"{name}.json"


def verifications_dir_for(name: str) -> Path:
    """`<workflows-dir>/.build/<name>/verifications/`."""
    return build_dir_for(name) / VERIFICATIONS_DIR_NAME


def gate_manifest_path(name: str) -> Path:
    """`<workflows-dir>/.build/<name>/verifications/gate.json`."""
    return verifications_dir_for(name) / GATE_MANIFEST_NAME


def gate_checks_dir(name: str) -> Path:
    """`<workflows-dir>/.build/<name>/verifications/checks/`."""
    return verifications_dir_for(name) / CHECKS_DIR_NAME
