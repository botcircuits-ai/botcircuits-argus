"""`local.py`'s `fetch_workflows` / `_load_workflow_record` against the
nested `.build/<name>/<name>.json` layout — the hard-cutover refactor
(no flat-file fallback, see `paths.py`'s module docstring)."""

from __future__ import annotations

import asyncio
import json

import pytest

from botcircuits.agent.workflow import local as wf_local


@pytest.fixture(autouse=True)
def _isolated_workflows_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(wf_local.WORKFLOWS_DIR_ENV, str(tmp_path))
    return tmp_path


def _record(name: str) -> dict:
    return {
        "name": name,
        "description": "test",
        "flow": {"start": "start", "steps": {"start": {"type": "start"}}},
    }


def _write_nested_build(tmp_path, name: str) -> None:
    build = tmp_path / ".build" / name
    build.mkdir(parents=True, exist_ok=True)
    (build / f"{name}.json").write_text(json.dumps(_record(name)), encoding="utf-8")


def test_fetch_workflows_finds_nested_build(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "wf_a.json").write_text(json.dumps(_record("wf_a")), encoding="utf-8")
    _write_nested_build(tmp_path, "wf_a")

    records = asyncio.run(wf_local.fetch_workflows())
    assert [r["name"] for r in records] == ["wf_a"]


def test_fetch_workflows_ignores_stale_flat_leftover(tmp_path):
    """A flat `.build/<name>.json` left over from before the nested-layout
    cutover must NOT be picked up — confirms the hard cutover, no
    dual-read compatibility shim."""
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "wf_flat.json").write_text(
        json.dumps(_record("wf_flat")), encoding="utf-8"
    )
    build_dir = tmp_path / ".build"
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "wf_flat.json").write_text(
        json.dumps(_record("wf_flat")), encoding="utf-8"
    )

    records = asyncio.run(wf_local.fetch_workflows())
    assert records == []


def test_load_workflow_record_direct_hit(tmp_path):
    _write_nested_build(tmp_path, "wf_b")
    record = wf_local._load_workflow_record("wf_b")
    assert record["name"] == "wf_b"


def test_load_workflow_record_scans_when_filename_diverges(tmp_path):
    """The fallback scan (`*/*.json`, match on the record's `name` field)
    still works under the nested layout when a file's stem doesn't match
    its declared name."""
    build = tmp_path / ".build" / "some_dir"
    build.mkdir(parents=True, exist_ok=True)
    (build / "different_filename.json").write_text(
        json.dumps(_record("wf_c")), encoding="utf-8"
    )
    record = wf_local._load_workflow_record("wf_c")
    assert record["name"] == "wf_c"


def test_load_workflow_record_missing_raises(tmp_path):
    with pytest.raises(wf_local.LocalWorkflowError):
        wf_local._load_workflow_record("does_not_exist")
