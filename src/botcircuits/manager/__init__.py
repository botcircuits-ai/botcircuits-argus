"""BotCircuits Manager — backend for the manager web.

Serves the workflow execution traces (and their memory graphs) written by the
tracing layer to ``.botcircuits/sessions/`` so the manager web can list runs
and render a per-session trace + memory-flow view.

Run:
    uv run uvicorn botcircuits.manager:app --reload --port 8700
    # or:
    botcircuits-manager
"""

from botcircuits.manager.app import app, create_app

__all__ = ["app", "create_app"]
