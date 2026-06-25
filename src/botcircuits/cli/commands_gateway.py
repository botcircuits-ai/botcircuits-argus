"""`botcircuits gateway ...` subcommands — run the BotCircuits gateway.

  gateway serve [--host H] [--port N] [--reload]
        Serve the FastAPI gateway (``botcircuits.gateway:app``) with uvicorn.

This replaces the old ``botcircuits-gateway`` console script: the gateway is now
a subcommand of the single ``botcircuits`` CLI, so there is one binary on PATH.
The actual server bootstrap stays in ``botcircuits.gateway.__main__`` (also
runnable via ``python -m botcircuits.gateway``); this module is just CLI wiring.
"""

from __future__ import annotations

import argparse
import os

from botcircuits.cli.ansi import C, out


def add_gateway_subparser(subparsers: argparse._SubParsersAction) -> None:
    gw = subparsers.add_parser(
        "gateway",
        help="Run the BotCircuits gateway (FastAPI server).",
    )
    gw_subs = gw.add_subparsers(dest="gateway_cmd", required=True)

    serve_p = gw_subs.add_parser(
        "serve", help="Serve the gateway API with uvicorn."
    )
    serve_p.add_argument(
        "--host", default=None,
        help="Bind host (default: $BOTCIRCUITS_HOST or 127.0.0.1).",
    )
    serve_p.add_argument(
        "--port", type=int, default=None,
        help="Bind port (default: $BOTCIRCUITS_PORT or 8000).",
    )
    serve_p.add_argument(
        "--reload", action="store_true",
        help="Auto-reload on code changes (dev).",
    )


def run_gateway_command(args: argparse.Namespace) -> int:
    if args.gateway_cmd == "serve":
        return _cmd_serve(args)
    out(C.red(f"[gateway] unknown subcommand: {args.gateway_cmd}"))
    return 2


def _cmd_serve(args: argparse.Namespace) -> int:
    # CLI flags override the env vars `gateway.__main__` reads, so the one
    # bootstrap path stays the single source of truth for defaults.
    if args.host is not None:
        os.environ["BOTCIRCUITS_HOST"] = str(args.host)
    if args.port is not None:
        os.environ["BOTCIRCUITS_PORT"] = str(args.port)
    if args.reload:
        os.environ["BOTCIRCUITS_RELOAD"] = "true"

    from botcircuits.gateway.__main__ import main as gateway_main

    host = os.getenv("BOTCIRCUITS_HOST", "127.0.0.1")
    port = os.getenv("BOTCIRCUITS_PORT", "8000")
    out(C.dim(f"[gateway] serving http://{host}:{port}"))
    gateway_main()  # blocks (uvicorn.run); returns on shutdown
    return 0
