"""Tests for the manager process supervisor.

These never spawn real servers: we monkeypatch the spawn + liveness primitives
so we exercise the PID-tracking / state / teardown logic deterministically.
"""

from __future__ import annotations

import json

import pytest

from botcircuits.manager import supervisor as sup


@pytest.fixture
def env(tmp_path, monkeypatch):
    # Sessions/manager dirs resolve under this temp workflows dir.
    monkeypatch.setenv("BOTCIRCUITS_WORKFLOWS_DIR", str(tmp_path / "workflows"))
    monkeypatch.setenv("BOTCIRCUITS_MANAGER_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("BOTCIRCUITS_MANAGER_ADMIN_PASSWORD", "pw")
    # Fake a manager_web that looks installed (package.json + node_modules).
    fe = tmp_path / "manager_web"
    (fe / "node_modules").mkdir(parents=True)
    (fe / "package.json").write_text("{}")
    monkeypatch.chdir(tmp_path)
    return tmp_path


class _FakeProc:
    _next = 1000

    def __init__(self, argv):
        self.argv = argv
        _FakeProc._next += 1
        self.pid = _FakeProc._next


@pytest.fixture
def fake_spawn(monkeypatch):
    spawned: list[list[str]] = []

    def _spawn(argv, *, cwd, log_path, env):
        spawned.append(argv)
        return _FakeProc(argv)

    monkeypatch.setattr(sup, "_spawn", _spawn)
    monkeypatch.setattr(sup, "_pgid_of", lambda pid: pid)
    return spawned


def test_start_launches_both_and_writes_state(env, fake_spawn):
    import botcircuits.manager.supervisor as s

    state = s.start()
    assert set(state["_started"]) == {s.BACKEND, s.FRONTEND}

    on_disk = json.loads((sup._state_path()).read_text())
    assert s.BACKEND in on_disk and s.FRONTEND in on_disk
    assert on_disk[s.BACKEND]["port"] == sup.DEFAULT_BACKEND_PORT
    assert on_disk[s.FRONTEND]["port"] == sup.DEFAULT_FRONTEND_PORT
    # Two processes were spawned: uvicorn + npm.
    assert any("uvicorn" in a for a in fake_spawn[0])
    assert fake_spawn[1][:3] == ["npm", "run", "dev:port"]


def test_start_is_idempotent_per_service(env, fake_spawn, monkeypatch):
    s = sup
    monkeypatch.setattr(s, "_pid_alive", lambda pid: True)  # pretend running
    s.start()  # first
    second = s.start()  # nothing new
    assert second["_started"] == []


def test_start_backend_only(env, fake_spawn):
    state = sup.start(backend_only=True)
    assert state["_started"] == [sup.BACKEND]
    assert sup.FRONTEND not in state


def test_frontend_requires_node_modules(env, fake_spawn, monkeypatch):
    # Remove node_modules → frontend start should error, backend still ok.
    import shutil

    shutil.rmtree(env / "manager_web" / "node_modules")
    with pytest.raises(sup.SupervisorError):
        sup.start()


def test_status_reaps_dead_pids(env, fake_spawn, monkeypatch):
    sup.start()
    # All dead now.
    monkeypatch.setattr(sup, "_pid_alive", lambda pid: False)
    rows = sup.status()
    # status reports them not-running and prunes the state file.
    assert all(r["running"] is False for r in rows)
    assert not sup._state_path().exists()


def test_stop_terminates_and_clears_state(env, fake_spawn, monkeypatch):
    sup.start()
    killed: list[int] = []

    def _term(svc, timeout=8.0):
        killed.append(svc.pid)
        return True

    monkeypatch.setattr(sup, "_terminate", _term)
    results = sup.stop()
    assert {k for k, _ in results} == {sup.BACKEND, sup.FRONTEND}
    assert all(ok for _, ok in results)
    assert len(killed) == 2
    assert not sup._state_path().exists()


def test_stop_with_nothing_tracked(env):
    assert sup.stop() == []
