"""Server-Sent Events serialization for the streaming chat endpoint."""

from __future__ import annotations

import json
from typing import AsyncIterator, TYPE_CHECKING

from botcircuits.gateway.schemas import ChatRequest

if TYPE_CHECKING:
    from ..agent import Agent


def sse(event: str, data: dict) -> bytes:
    """Format one Server-Sent Event message."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8")


async def event_stream(agent: "Agent", req: ChatRequest,
                       default_system: str | None = None) -> AsyncIterator[bytes]:
    # Open with a comment to flush headers immediately on some proxies.
    yield b": ready\n\n"
    system = req.system or default_system
    try:
        async for ev in agent.chat_stream(req.message, session_id=req.session_id,
                                          system=system):
            payload: dict = {"session_id": ev.session_id}
            if ev.type == "text_delta":
                payload["text"] = ev.text
                yield sse("text", payload)
            elif ev.type == "tool_call":
                tc = ev.tool_call
                payload.update({"id": tc.id, "name": tc.name, "arguments": tc.arguments})
                yield sse("tool_call", payload)
            elif ev.type == "tool_result":
                payload.update({"id": ev.tool_call_id, "result": ev.text,
                                "is_error": ev.is_error})
                yield sse("tool_result", payload)
            elif ev.type == "turn_end":
                yield sse("turn_end", payload)
            elif ev.type == "done":
                payload["text"] = ev.text
                yield sse("done", payload)
            elif ev.type == "error":
                payload["error"] = ev.text
                yield sse("error", payload)
    except Exception as e:
        yield sse("error", {"error": f"{type(e).__name__}: {e}"})
