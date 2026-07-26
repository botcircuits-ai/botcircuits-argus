"""`supervisor.start()` must not report success for a service that never
actually bound its port — the bug this guards against: another process
(often from a DIFFERENT project, left running on the same fixed default
port) squats the port, the newly spawned process dies immediately, but
`start()` used to write its dead PID into the state file and tell the
caller "started" anyway. The caller's browser then kept talking to the
OLD process, which looked like "the manager is pointed at the wrong
project" even though the new one really did try to start from the right
cwd."""

from __future__ import annotations

import pytest

from botcircuits.manager import supervisor as sup


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTCIRCUITS_WORKFLOWS_DIR", str(tmp_path / "workflows"))
    monkeypatch.setenv("BOTCIRCUITS_MANAGER_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("BOTCIRCUITS_MANAGER_ADMIN_PASSWORD", "pw")
    monkeypatch.chdir(tmp_path)
    return tmp_path


class _FakeProc:
    """A spawned process that never binds its port and exits immediately
    with a nonzero code — simulating uvicorn's "address already in use"
    failure mode."""

    def __init__(self, argv):
        self.argv = argv
        self.pid = 4242
        self.returncode = 1

    def poll(self):
        return self.returncode


class _FakeSlowButOkProc:
    """A process that hasn't exited and hasn't bound its port YET — the
    "still starting" state _wait_for_startup must tolerate, not fail on
    the first poll."""

    def __init__(self, argv):
        self.argv = argv
        self.pid = 4343
        self.returncode = None

    def poll(self):
        return None


def test_start_raises_when_port_collision_kills_the_process(env, monkeypatch):
    """The exact reported bug: a stale process from a different project
    already holds the port, the new spawn dies, `start()` must raise —
    never silently write a dead PID and report success."""
    monkeypatch.setattr(sup, "_spawn", lambda *a, **kw: _FakeProc(a))
    monkeypatch.setattr(sup, "_pgid_of", lambda pid: pid)
    monkeypatch.setattr(sup, "_port_is_listening", lambda port, host="127.0.0.1": False)
    monkeypatch.setattr(sup, "_STARTUP_POLL_INTERVAL", 0.01)

    with pytest.raises(sup.SupervisorError, match="already using that port"):
        sup.start(backend_only=True)

    # No lying PID left behind for `status`/`stop` to trip over.
    assert not sup._state_path().exists()


def test_start_succeeds_once_the_port_comes_up(env, monkeypatch):
    """A process that's merely slow to bind (not dead) must not be
    treated as a failure — only "port never came up AND process died /
    timed out" counts as a real failure."""
    calls = {"n": 0}

    def _port_check(port, host="127.0.0.1"):
        calls["n"] += 1
        return calls["n"] >= 3  # "not yet, not yet, now it's up"

    monkeypatch.setattr(sup, "_spawn", lambda *a, **kw: _FakeSlowButOkProc(a))
    monkeypatch.setattr(sup, "_pgid_of", lambda pid: pid)
    monkeypatch.setattr(sup, "_port_is_listening", _port_check)
    monkeypatch.setattr(sup, "_STARTUP_POLL_INTERVAL", 0.01)

    state = sup.start(backend_only=True)
    assert state["_started"] == [sup.BACKEND]
    assert calls["n"] >= 3


def test_start_times_out_if_port_never_comes_up_and_process_never_dies(env, monkeypatch):
    monkeypatch.setattr(sup, "_spawn", lambda *a, **kw: _FakeSlowButOkProc(a))
    monkeypatch.setattr(sup, "_pgid_of", lambda pid: pid)
    monkeypatch.setattr(sup, "_port_is_listening", lambda port, host="127.0.0.1": False)
    monkeypatch.setattr(sup, "_STARTUP_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(sup, "_STARTUP_TIMEOUT_SECONDS", 0.05)

    with pytest.raises(sup.SupervisorError, match="timed out"):
        sup.start(backend_only=True)


def test_failed_backend_start_terminates_the_dead_process_group(env, monkeypatch):
    """Even though the process already died on its own (bind failure),
    `start()` still calls `_terminate` to reap it / its process group —
    belt-and-suspenders against a half-alive child."""
    terminated: list[int] = []
    monkeypatch.setattr(sup, "_spawn", lambda *a, **kw: _FakeProc(a))
    monkeypatch.setattr(sup, "_pgid_of", lambda pid: pid)
    monkeypatch.setattr(sup, "_port_is_listening", lambda port, host="127.0.0.1": False)
    monkeypatch.setattr(sup, "_STARTUP_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(sup, "_terminate", lambda svc, **kw: terminated.append(svc.pid) or True)

    with pytest.raises(sup.SupervisorError):
        sup.start(backend_only=True)

    assert terminated == [4242]
