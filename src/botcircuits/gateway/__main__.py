"""Run the gateway with `python -m botcircuits.gateway`.

Honors the same env vars as `botcircuits.gateway:app`. For dev, prefer:

  uv run uvicorn botcircuits.gateway:app --reload --port 8000
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "botcircuits.gateway:app",
        host=os.getenv("BOTCIRCUITS_HOST", "127.0.0.1"),
        port=int(os.getenv("BOTCIRCUITS_PORT", "8000")),
        reload=os.getenv("BOTCIRCUITS_RELOAD", "false").lower() in ("1", "true", "yes"),
    )


if __name__ == "__main__":
    main()
