"""Memory storage location (`agent/memory.py`).

Resolution mirrors the rest of the `.botcircuits/` surface: a project with
its own `.botcircuits/` folder keeps its memory locally (per-project
notes); anywhere else falls back to the global `~/.botcircuits/memories/`;
the env override wins over both.
"""

from __future__ import annotations

from pathlib import Path

from botcircuits.agent.memory import (
    DEFAULT_MEMORY_DIRNAME,
    MEMORY_DIR_ENV,
    memory_dir,
)


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv(MEMORY_DIR_ENV, str(tmp_path / "custom"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".botcircuits").mkdir()  # even with a local project folder
    assert memory_dir() == tmp_path / "custom"


def test_project_with_botcircuits_folder_stores_locally(tmp_path, monkeypatch):
    monkeypatch.delenv(MEMORY_DIR_ENV, raising=False)
    (tmp_path / ".botcircuits").mkdir()
    monkeypatch.chdir(tmp_path)
    assert memory_dir() == tmp_path / DEFAULT_MEMORY_DIRNAME


def test_without_project_folder_falls_back_to_home(tmp_path, monkeypatch):
    monkeypatch.delenv(MEMORY_DIR_ENV, raising=False)
    monkeypatch.chdir(tmp_path)  # no .botcircuits/ here
    assert memory_dir() == Path.home() / DEFAULT_MEMORY_DIRNAME
