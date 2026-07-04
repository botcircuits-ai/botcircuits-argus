"""Task-specific coding workflows live in `<workflows-dir>/coding/`.

`workflow build` must locate sources there (directly by filename and by
scanning `name` fields), and `fetch_workflows` must include them in the
"no build counterpart" warning sweep. Built copies stay FLAT in
`.build/`, so the run path needs no subdirectory awareness.
"""

import json

import pytest

from botcircuits.cli.commands_workflow import _locate_workflow_file
from botcircuits.agent.workflow.local import LocalWorkflowError, WORKFLOWS_DIR_ENV


def _write(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))


@pytest.fixture
def workflows_dir(tmp_path, monkeypatch):
    d = tmp_path / "workflows"
    d.mkdir()
    monkeypatch.setenv(WORKFLOWS_DIR_ENV, str(d))
    return d


def test_locate_finds_coding_subdir_by_filename(workflows_dir):
    record = {"name": "user_login_workflow", "flow": {"start": "start", "steps": {}}}
    _write(workflows_dir / "coding" / "user_login_workflow.json", record)

    path, loaded = _locate_workflow_file("user_login_workflow")

    assert path == workflows_dir / "coding" / "user_login_workflow.json"
    assert loaded["name"] == "user_login_workflow"


def test_locate_finds_coding_subdir_by_name_field(workflows_dir):
    record = {"name": "user_login_workflow", "flow": {"start": "start", "steps": {}}}
    _write(workflows_dir / "coding" / "some_other_filename.json", record)

    path, loaded = _locate_workflow_file("user_login_workflow")

    assert path == workflows_dir / "coding" / "some_other_filename.json"
    assert loaded["name"] == "user_login_workflow"


def test_locate_prefers_top_level_over_coding(workflows_dir):
    _write(workflows_dir / "dup.json", {"name": "dup", "marker": "top"})
    _write(workflows_dir / "coding" / "dup.json", {"name": "dup", "marker": "coding"})

    path, loaded = _locate_workflow_file("dup")

    assert path == workflows_dir / "dup.json"
    assert loaded["marker"] == "top"


def test_locate_missing_still_raises(workflows_dir):
    _write(workflows_dir / "coding" / "a.json", {"name": "a"})
    with pytest.raises(LocalWorkflowError):
        _locate_workflow_file("nope")


def test_fetch_workflows_warns_for_unbuilt_coding_source(workflows_dir, capsys):
    import asyncio

    from botcircuits.agent.workflow.local import fetch_workflows

    _write(
        workflows_dir / "coding" / "user_login_workflow.json",
        {"name": "user_login_workflow", "flow": {"start": "start", "steps": {}}},
    )

    records = asyncio.run(fetch_workflows())

    assert records == []  # nothing built yet
    err = capsys.readouterr().err
    assert "user_login_workflow" in err
