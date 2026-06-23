"""Pydantic request/response models for the FastAPI gateway."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    session_id: str | None = None
    system: str | None = None


class ChatResponse(BaseModel):
    reply: str
    session_id: str
