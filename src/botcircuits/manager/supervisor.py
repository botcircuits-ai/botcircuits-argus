"""Process supervisor for `botcircuits manager start|stop|status`.

Starts the manager **backend** (uvicorn serving ``botcircuits.manager:app``) and
the **frontend** (the Next.js dev server in ``manager_web/``) as detached
background processes, and tracks their PIDs in a small state file so a later
``stop`` can find and terminate them — including the frontend's child process
tree (``npm run dev`` spawns ``next``), which we handle by launching each
service in its own **process group** and signalling the whole group.

State file: ``.botcircuits/manager/manager.pid.json`` ::

    {"backend": {"pid": 123, "pgid": 123, "port": 8700, "log": "..."},
     "frontend": {"pid": 456, "pgid": 456, "port": 3700, "log": "..."}}

Cross-platform note: process-group signalling uses POSIX ``os.killpg`` /
``os.setsid``. On Windows we fall back to terminating the single PID (the
frontend child tree is best-effort there); the manager is primarily a
local/dev tool on macOS/Linux.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from botcircuits.manager import auth

_POSIX = os.name == "posix"

DEFAULT_BACKEND_PORT = 8700
DEFAULT_FRONTEND_PORT = 3700

#: Service keys in the state file.
BACKEND = "backend"
FRONTEND = "frontend"


def _state_dir() -> Path:
    # Sits alongside the workflows/sessions data under .botcircuits/.
    from botcircuits.agent.workflow.local import _resolve_workflows_dir

    return (_resolve_workflows_dir() / ".." / "manager").resolve()


def _state_path() -> Path:
    return _state_dir() / "manager.pid.json"


def _repo_root() -> Path:
    """The project root (where ``manager_web/`` lives).

    Prefers the cwd if it has its own ``manager_web/`` (a source checkout the
    user is intentionally running from), otherwise falls back to the
    installed ``botcircuits`` package location (``src/botcircuits/`` sits two
    levels below the repo root) — so ``botcircuits manager start`` also works
    from an unrelated cwd.
    """
    cwd = Path.cwd()
    if (cwd / "manager_web").is_dir():
        return cwd

    import botcircuits

    pkg_root = Path(botcircuits.__file__).resolve().parent.parent.parent
    if (pkg_root / "manager_web").is_dir():
        return pkg_root
    return cwd


def _read_state() -> dict:
    p = _state_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _clear_state() -> None:
    try:
        _state_path().unlink()
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours to signal
    return True


@dataclass
class Service:
    key: str
    pid: int
    pgid: int
    port: int
    log: str

    @classmethod
    def from_dict(cls, key: str, d: dict) -> "Service | None":
        if not isinstance(d, dict) or "pid" not in d:
            return None
        return cls(
            key=key,
            pid=int(d.get("pid", 0)),
            pgid=int(d.get("pgid", d.get("pid", 0))),
            port=int(d.get("port", 0)),
            log=str(d.get("log", "")),
        )

    def to_dict(self) -> dict:
        return {"pid": self.pid, "pgid": self.pgid, "port": self.port, "log": self.log}


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

def _spawn(argv: list[str], *, cwd: Path, log_path: Path, env: dict) -> subprocess.Popen:
    """Spawn a detached background process in its own session/process group,
    redirecting output to `log_path`."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_f = open(log_path, "ab", buffering=0)
    popen_kw: dict = {
        "cwd": str(cwd),
        "stdout": log_f,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "env": env,
    }
    if _POSIX:
        # New session → its own process group, so we can kill the whole tree.
        popen_kw["start_new_session"] = True
    else:  # pragma: no cover - Windows best-effort
        popen_kw["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    return subprocess.Popen(argv, **popen_kw)


def _backend_cmd() -> list[str]:
    # Use the current interpreter's uvicorn so it runs in the same venv as the
    # installed package (no reliance on a `uvicorn` console script on PATH).
    return [
        sys.executable, "-m", "uvicorn", "botcircuits.manager:app",
        "--host", os.getenv("BOTCIRCUITS_MANAGER_HOST", "127.0.0.1"),
        "--port", str(_backend_port()),
    ]


def _frontend_cmd() -> list[str]:
    # `dev:port` is `next dev` with NO hardcoded port, so the `-p <port>` we
    # append (after `--`) is the single, authoritative port flag.
    return ["npm", "run", "dev:port"]


def _backend_port() -> int:
    return int(os.getenv("BOTCIRCUITS_MANAGER_PORT", str(DEFAULT_BACKEND_PORT)))


def _frontend_port() -> int:
    return int(os.getenv("BOTCIRCUITS_MANAGER_WEB_PORT", str(DEFAULT_FRONTEND_PORT)))


def _frontend_dir() -> Path:
    return _repo_root() / "manager_web"


class SupervisorError(Exception):
    pass


def start(*, backend_only: bool = False, frontend_only: bool = False) -> dict:
    """Start the manager service(s) in the background. Returns the new state.

    Idempotent per-service: a service already running (live PID in the state
    file) is left as-is. Raises :class:`SupervisorError` for hard problems
    (e.g. frontend requested but ``manager_web`` not set up).
    """
    state = _read_state()
    started: list[str] = []
    log_dir = _state_dir() / "logs"

    want_backend = not frontend_only
    want_frontend = not backend_only

    if want_backend:
        existing = Service.from_dict(BACKEND, state.get(BACKEND, {}))
        if existing and _pid_alive(existing.pid):
            pass  # already running
        else:
            if not (os.getenv(auth.USERNAME_ENV) and os.getenv(auth.PASSWORD_ENV)):
                print(
                    f"[manager] warning: {auth.USERNAME_ENV} / {auth.PASSWORD_ENV} "
                    f"not set — using default credentials "
                    f"({auth.DEFAULT_USERNAME}/{auth.DEFAULT_PASSWORD}). "
                    "Override both vars for any real deployment.",
                    file=sys.stderr,
                )
            log = log_dir / "backend.log"
            # cwd is the user's invocation directory, not `_repo_root()` — the
            # backend resolves `.botcircuits/workflows` (via
            # `_resolve_workflows_dir`) relative to its own cwd, so spawning it
            # from the package/repo root would silently read/write the wrong
            # project's workflows and traces.
            proc = _spawn(_backend_cmd(), cwd=Path.cwd(), log_path=log, env=os.environ.copy())
            state[BACKEND] = Service(
                BACKEND, proc.pid, _pgid_of(proc.pid), _backend_port(), str(log),
            ).to_dict()
            started.append(BACKEND)

    if want_frontend:
        fe_dir = _frontend_dir()
        existing = Service.from_dict(FRONTEND, state.get(FRONTEND, {}))
        if existing and _pid_alive(existing.pid):
            pass
        elif not (fe_dir / "package.json").is_file():
            raise SupervisorError(
                f"frontend not found at {fe_dir}. Run from the repo root, or "
                "use `botcircuits manager start --backend-only`."
            )
        elif not (fe_dir / "node_modules").is_dir():
            raise SupervisorError(
                f"frontend deps not installed. Run `npm install` in {fe_dir}, "
                "or use `botcircuits manager start --backend-only`."
            )
        else:
            log = log_dir / "frontend.log"
            env = os.environ.copy()
            # Pin the dev port and point the web at the backend by default.
            env.setdefault(
                "NEXT_PUBLIC_API_BASE",
                f"http://127.0.0.1:{_backend_port()}",
            )
            argv = _frontend_cmd() + ["--", "-p", str(_frontend_port())]
            proc = _spawn(argv, cwd=fe_dir, log_path=log, env=env)
            state[FRONTEND] = Service(
                FRONTEND, proc.pid, _pgid_of(proc.pid), _frontend_port(), str(log),
            ).to_dict()
            started.append(FRONTEND)

    _write_state(state)
    state["_started"] = started
    return state


def _pgid_of(pid: int) -> int:
    if not _POSIX:
        return pid
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, OSError):
        return pid


# ---------------------------------------------------------------------------
# Stop / status
# ---------------------------------------------------------------------------

def _terminate(svc: Service, *, timeout: float = 8.0) -> bool:
    """Signal a service's process group (POSIX) or PID (Windows), escalating
    to SIGKILL if it doesn't exit. Returns True if it's gone afterwards."""
    if not _pid_alive(svc.pid):
        return True
    try:
        if _POSIX and svc.pgid > 0:
            os.killpg(svc.pgid, signal.SIGTERM)
        else:
            os.kill(svc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        pass

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(svc.pid):
            return True
        time.sleep(0.2)

    # Escalate.
    try:
        if _POSIX and svc.pgid > 0:
            os.killpg(svc.pgid, signal.SIGKILL)
        else:
            os.kill(svc.pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    time.sleep(0.3)
    return not _pid_alive(svc.pid)


def stop() -> list[tuple[str, bool]]:
    """Stop all tracked services. Returns ``[(key, stopped?), ...]`` and clears
    the state file."""
    state = _read_state()
    results: list[tuple[str, bool]] = []
    for key in (FRONTEND, BACKEND):  # frontend first (depends on backend)
        svc = Service.from_dict(key, state.get(key, {}))
        if svc is None:
            continue
        results.append((key, _terminate(svc)))
    _clear_state()
    return results


def status() -> list[dict]:
    """Current state of each tracked service, reconciled against live PIDs."""
    state = _read_state()
    out: list[dict] = []
    changed = False
    for key in (BACKEND, FRONTEND):
        svc = Service.from_dict(key, state.get(key, {}))
        if svc is None:
            continue
        alive = _pid_alive(svc.pid)
        if not alive:
            # Reap stale entries so `status` reflects reality.
            state.pop(key, None)
            changed = True
        out.append({
            "service": key,
            "pid": svc.pid,
            "port": svc.port,
            "running": alive,
            "url": f"http://127.0.0.1:{svc.port}" if svc.port else None,
            "log": svc.log,
        })
    if changed:
        if state:
            _write_state(state)
        else:
            _clear_state()
    return out


__all__ = [
    "start",
    "stop",
    "status",
    "SupervisorError",
    "BACKEND",
    "FRONTEND",
    "DEFAULT_BACKEND_PORT",
    "DEFAULT_FRONTEND_PORT",
]
