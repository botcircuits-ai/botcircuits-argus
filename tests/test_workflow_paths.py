"""`agent/workflow/paths.py` — the single source of truth for every
path under the workflows tree. Covers the nested build layout
(`.build/<name>/<name>.json`, `.build/<name>/verifications/...`) and
that `local.py`/`manager/workflows.py` re-export the SAME functions
rather than keeping drifted duplicates (the bug this module fixes)."""

from __future__ import annotations

from botcircuits.agent.workflow import local as wf_local
from botcircuits.agent.workflow import paths


def test_resolve_workflows_dir_honors_env(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.WORKFLOWS_DIR_ENV, str(tmp_path))
    assert paths.resolve_workflows_dir() == tmp_path.resolve()


def test_build_paths_are_nested_per_workflow(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.WORKFLOWS_DIR_ENV, str(tmp_path))
    assert paths.resolve_build_dir() == (tmp_path / ".build").resolve()
    assert paths.build_dir_for("order_fulfillment") == (
        tmp_path / ".build" / "order_fulfillment"
    ).resolve()
    assert paths.build_json_path("order_fulfillment") == (
        tmp_path / ".build" / "order_fulfillment" / "order_fulfillment.json"
    ).resolve()


def test_verification_paths_live_inside_the_workflows_build_folder(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.WORKFLOWS_DIR_ENV, str(tmp_path))
    base = tmp_path / ".build" / "order_fulfillment"
    assert paths.verifications_dir_for("order_fulfillment") == (
        base / "verifications"
    ).resolve()
    assert paths.gate_manifest_path("order_fulfillment") == (
        base / "verifications" / "gate.json"
    ).resolve()
    assert paths.gate_checks_dir("order_fulfillment") == (
        base / "verifications" / "checks"
    ).resolve()


def test_local_reexports_the_same_path_helpers_no_drift():
    """`local.py` must import from `paths.py` rather than keeping its own
    copy — this is a regression guard against the exact duplication that
    motivated the refactor (BUILD_DIR_NAME was independently defined in
    `local.py` and `build_workflow.py` before this module existed)."""
    assert wf_local._resolve_workflows_dir is paths.resolve_workflows_dir
    assert wf_local._resolve_build_dir is paths.resolve_build_dir
    assert wf_local.BUILD_DIR_NAME == paths.BUILD_DIR_NAME


def test_manager_workflows_reexports_the_same_path_helpers_no_drift():
    from botcircuits.manager import workflows as mgr

    assert mgr._resolve_workflows_dir is paths.resolve_workflows_dir
    assert mgr._resolve_build_dir is paths.resolve_build_dir
