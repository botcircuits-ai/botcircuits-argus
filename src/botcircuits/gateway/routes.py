"""FastAPI route handlers for the chat gateway."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from botcircuits.agent import Agent
from botcircuits.gateway.schemas import ChatRequest, ChatResponse
from botcircuits.gateway.sse import event_stream

router = APIRouter()


@router.get("/health")
async def health():
    return {"ok": True}


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request):
    agent: Agent = request.app.state.agent
    system = req.system or request.app.state.default_system
    try:
        reply, sid = await agent.chat(req.message, session_id=req.session_id,
                                       system=system)
        return ChatResponse(reply=reply, session_id=sid)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest, request: Request):
    agent: Agent = request.app.state.agent
    default_system = request.app.state.default_system
    return StreamingResponse(
        event_stream(agent, req, default_system=default_system),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",      # disable nginx buffering
            "Connection": "keep-alive",
        },
    )


@router.post("/sessions/{session_id}/reset")
async def reset_session(session_id: str, request: Request):
    agent: Agent = request.app.state.agent
    agent.store.reset(session_id)
    return {"reset": session_id}


@router.get("/messaging/status")
async def messaging_status(request: Request):
    """List registered channels — useful to verify env/config wiring."""
    gateway = getattr(request.app.state, "gateway", None)
    if gateway is None:
        return {"channels": []}
    return {
        "channels": [
            {"name": ch.name, "type": type(ch).__name__}
            for ch in gateway.channels()
        ]
    }
