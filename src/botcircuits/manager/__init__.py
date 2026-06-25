"""BotCircuits Manager — backend for the manager web.

Serves the workflow execution traces (and their memory graphs) written by the
tracing layer to ``.botcircuits/sessions/`` so the manager web can list runs
and render a per-session trace + memory-flow view.

Run:
    botcircuits manager start --backend-only
    # or, for dev with auto-reload:
    uv run uvicorn botcircuits.manager:app --reload --port 8700
    # or the raw bootstrap:
    python -m botcircuits.manager
"""

from botcircuits.manager.app import app, create_app

__all__ = ["app", "create_app"]
