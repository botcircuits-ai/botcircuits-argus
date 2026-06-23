"""Arithmetic builtins."""

from __future__ import annotations

from botcircuits.agent.tools.registry import LocalTool, ToolRegistry


def add_tool() -> LocalTool:
    async def _add(args: dict) -> dict:
        return {"sum": float(args["a"]) + float(args["b"])}

    return LocalTool(
        name="add",
        description="Add two numbers and return the sum.",
        input_schema={
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
        handler=_add,
    )


def register(reg: ToolRegistry, **config) -> None:
    if config:
        raise ValueError(
            f"`add` tool takes no config; got: {sorted(config)}"
        )
    reg.register(add_tool())
