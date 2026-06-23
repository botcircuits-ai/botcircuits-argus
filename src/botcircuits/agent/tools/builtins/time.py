"""Time builtins."""

from __future__ import annotations

from datetime import datetime, timezone

from botcircuits.agent.tools.registry import LocalTool, ToolRegistry


def now_tool() -> LocalTool:
    def _now(_: dict) -> dict:
        return {"utc": datetime.now(timezone.utc).isoformat()}

    return LocalTool(
        name="now",
        description="Return the current UTC time in ISO 8601.",
        input_schema={"type": "object", "properties": {}},
        handler=_now,
    )


def register(reg: ToolRegistry, **config) -> None:
    if config:
        raise ValueError(
            f"`now` tool takes no config; got: {sorted(config)}"
        )
    reg.register(now_tool())
