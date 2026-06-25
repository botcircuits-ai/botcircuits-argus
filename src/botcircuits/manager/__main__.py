"""Run the manager backend with `python -m botcircuits.manager`.

This is the raw uvicorn bootstrap; the user-facing entry point is
`botcircuits manager start` (which can also launch the web frontend).

Env:
  BOTCIRCUITS_MANAGER_HOST   (default 127.0.0.1)
  BOTCIRCUITS_MANAGER_PORT   (default 8700)
  BOTCIRCUITS_MANAGER_RELOAD (default false)
  BOTCIRCUITS_MANAGER_ADMIN_USERNAME / _ADMIN_PASSWORD  (required for login)

For dev with auto-reload, prefer:
  uv run uvicorn botcircuits.manager:app --reload --port 8700
"""

from __future__ import annotations

import os
import sys

import uvicorn

from botcircuits.manager import auth


def main() -> None:
    if not auth.is_configured():
        print(
            "[manager] warning: admin credentials not set — login will fail.\n"
            f"          set {auth.USERNAME_ENV} and {auth.PASSWORD_ENV}.",
            file=sys.stderr,
        )
    uvicorn.run(
        "botcircuits.manager:app",
        host=os.getenv("BOTCIRCUITS_MANAGER_HOST", "127.0.0.1"),
        port=int(os.getenv("BOTCIRCUITS_MANAGER_PORT", "8700")),
        reload=os.getenv("BOTCIRCUITS_MANAGER_RELOAD", "false").lower()
        in ("1", "true", "yes"),
    )


if __name__ == "__main__":
    main()
