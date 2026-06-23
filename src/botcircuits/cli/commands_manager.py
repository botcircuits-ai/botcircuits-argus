"""`botcircuits manager ...` subcommands — control the Manager services.

  manager start [--backend-only|--frontend-only]
        Launch the backend (FastAPI) and frontend (Next.js) in the background,
        tracking their PIDs so `manager stop` can find them.
  manager stop
        Stop the background services started by `manager start`.
  manager restart [--backend-only|--frontend-only]
        Stop the running services, then start them again.
  manager status
        Show which services are running, their ports/URLs, and log paths.

The heavy lifting (spawning, PID tracking, process-group teardown) lives in
``botcircuits.manager.supervisor``; this module is just CLI wiring + output.
"""

from __future__ import annotations

import argparse

from botcircuits.cli.ansi import C, out
from botcircuits.manager import supervisor


def add_manager_subparser(subparsers: argparse._SubParsersAction) -> None:
    mgr = subparsers.add_parser(
        "manager",
        help="Start/stop the BotCircuits Manager (backend + web).",
    )
    mgr_subs = mgr.add_subparsers(dest="manager_cmd", required=True)

    start_p = mgr_subs.add_parser(
        "start", help="Start the manager backend + frontend in the background."
    )
    grp = start_p.add_mutually_exclusive_group()
    grp.add_argument(
        "--backend-only", dest="backend_only", action="store_true",
        help="Start only the FastAPI backend.",
    )
    grp.add_argument(
        "--frontend-only", dest="frontend_only", action="store_true",
        help="Start only the Next.js frontend.",
    )

    mgr_subs.add_parser("stop", help="Stop the background manager services.")

    restart_p = mgr_subs.add_parser(
        "restart", help="Stop, then start the manager backend + frontend."
    )
    rgrp = restart_p.add_mutually_exclusive_group()
    rgrp.add_argument(
        "--backend-only", dest="backend_only", action="store_true",
        help="Restart only the FastAPI backend.",
    )
    rgrp.add_argument(
        "--frontend-only", dest="frontend_only", action="store_true",
        help="Restart only the Next.js frontend.",
    )

    mgr_subs.add_parser("status", help="Show manager service status.")


def run_manager_command(args: argparse.Namespace) -> int:
    if args.manager_cmd == "start":
        return _cmd_start(args)
    if args.manager_cmd == "stop":
        return _cmd_stop(args)
    if args.manager_cmd == "restart":
        return _cmd_restart(args)
    if args.manager_cmd == "status":
        return _cmd_status(args)
    out(C.red(f"[manager] unknown subcommand: {args.manager_cmd}"))
    return 2


def _cmd_start(args: argparse.Namespace) -> int:
    try:
        state = supervisor.start(
            backend_only=getattr(args, "backend_only", False),
            frontend_only=getattr(args, "frontend_only", False),
        )
    except supervisor.SupervisorError as e:
        out(C.red(f"[manager] {e}"))
        return 1

    fe = state.get(supervisor.FRONTEND)
    if isinstance(fe, dict) and fe.get("port"):
        url = f"http://127.0.0.1:{fe.get('port')}"
        out(C.green(f"  Manager web runs on {url}"))
        _open_browser(url)
    else:
        # No frontend (e.g. --backend-only). Surface the backend URL instead.
        be = state.get(supervisor.BACKEND)
        if isinstance(be, dict) and be.get("port"):
            out(C.green(
                f"  Manager backend runs on http://127.0.0.1:{be.get('port')}"
            ))

    out(C.dim(
        "  logs: .botcircuits/manager/logs/  ·  stop with: "
        "botcircuits manager stop"
    ))
    return 0


def _open_browser(url: str) -> None:
    """Best-effort open `url` in the default browser. Never raises; on failure
    prints a plain (non-error) note so the user can open it manually."""
    import webbrowser

    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    if not opened:
        out(C.dim(f"  unable to open browser automatically — open {url}"))


def _cmd_restart(args: argparse.Namespace) -> int:
    _cmd_stop(args)
    return _cmd_start(args)


def _cmd_stop(_args: argparse.Namespace) -> int:
    results = supervisor.stop()
    if not results:
        out(C.dim("[manager] nothing to stop."))
        return 0
    for key, stopped in results:
        if stopped:
            out(C.green(f"  ✓ {key} stopped"))
        else:
            out(C.red(f"  ✗ {key} could not be stopped (still running?)"))
    return 0 if all(s for _, s in results) else 1


def _cmd_status(_args: argparse.Namespace) -> int:
    rows = supervisor.status()
    if not rows:
        out(C.dim("[manager] no services tracked. Start with: "
                  "botcircuits manager start"))
        return 0
    for r in rows:
        if r["running"]:
            out(C.green(
                f"  ● {r['service']:<8} running  pid={r['pid']}  {r['url']}"
            ))
        else:
            out(C.red(f"  ○ {r['service']:<8} not running"))
        if r.get("log"):
            out(C.dim(f"      log: {r['log']}"))
    return 0


__all__ = ["add_manager_subparser", "run_manager_command"]
