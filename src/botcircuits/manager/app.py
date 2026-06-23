"""FastAPI app for the BotCircuits Manager backend.

Endpoints (all JSON):

    POST /api/auth/login        {username, password} -> {token, expires_in}
    GET  /api/sessions          -> [session summary, ...]      (auth)
    GET  /api/sessions/{id}     -> full session document       (auth)
    GET  /api/health            -> {status, auth_configured}

Auth is a bearer token from /api/auth/login (see ``auth``). CORS is open by
default for local dev so the Next.js manager web (a different port) can call
it; restrict via ``BOTCIRCUITS_MANAGER_CORS_ORIGINS`` (comma-separated) in
production.
"""

from __future__ import annotations

import os

from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from botcircuits.manager import auth, store
from botcircuits.manager import authoring, workflows as wf_store


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    token: str
    expires_in: int


class WorkflowSaveRequest(BaseModel):
    workflow: dict[str, Any]


class AuthorRequest(BaseModel):
    name: str
    instruction: str


def _require_user(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency: extract + verify the bearer token, return the user."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        return auth.verify(token)
    except auth.AuthError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e


def _cors_origins() -> list[str]:
    raw = os.getenv("BOTCIRCUITS_MANAGER_CORS_ORIGINS")
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return ["*"]


def create_app() -> FastAPI:
    app = FastAPI(title="BotCircuits Manager", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "auth_configured": auth.is_configured()}

    @app.post("/api/auth/login", response_model=LoginResponse)
    def login(body: LoginRequest) -> LoginResponse:
        try:
            token = auth.login(body.username, body.password)
        except auth.AuthError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e
        return LoginResponse(token=token, expires_in=auth.TOKEN_TTL)

    @app.get("/api/sessions")
    def list_sessions(_user: str = Depends(_require_user)) -> list[dict]:
        return store.list_sessions()

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str, _user: str = Depends(_require_user)) -> dict:
        doc = store.get_session(session_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="session not found")
        return doc

    # --- Workflow management -------------------------------------------------

    @app.get("/api/workflows")
    def list_workflows(_user: str = Depends(_require_user)) -> list[dict]:
        return wf_store.list_workflows()

    @app.get("/api/workflows/{name}")
    def get_workflow(name: str, _user: str = Depends(_require_user)) -> dict:
        doc = wf_store.get_workflow(name)
        if doc is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        return doc

    @app.put("/api/workflows/{name}")
    def save_workflow(
        name: str,
        body: WorkflowSaveRequest,
        _user: str = Depends(_require_user),
    ) -> dict:
        try:
            return wf_store.save_workflow(name, body.workflow)
        except wf_store.WorkflowStoreError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.delete("/api/workflows/{name}")
    def delete_workflow(name: str, _user: str = Depends(_require_user)) -> dict:
        try:
            deleted = wf_store.delete_workflow(name)
        except wf_store.WorkflowStoreError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if not deleted:
            raise HTTPException(status_code=404, detail="workflow not found")
        return {"deleted": True, "name": name}

    @app.post("/api/workflows/{name}/build")
    def build_workflow(name: str, _user: str = Depends(_require_user)) -> dict:
        try:
            return wf_store.build(name)
        except wf_store.WorkflowStoreError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/api/workflows/author/stream")
    def author_workflow(
        name: str,
        instruction: str,
        token: str,
    ) -> StreamingResponse:
        # EventSource can't set an Authorization header, so this endpoint
        # takes the bearer token as a query param and verifies it manually.
        try:
            auth.verify(token)
        except auth.AuthError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e
        return StreamingResponse(
            authoring.author_stream(instruction, name),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/workflows/run/stream")
    def run_workflow_stream(
        name: str,
        token: str,
        reply: str | None = None,
    ) -> StreamingResponse:
        # EventSource can't set an Authorization header, so this endpoint
        # takes the bearer token as a query param and verifies it manually.
        try:
            auth.verify(token)
        except auth.AuthError as e:
            raise HTTPException(status_code=401, detail=str(e)) from e
        return StreamingResponse(
            authoring.run_stream(name, reply),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


app = create_app()

__all__ = ["app", "create_app"]
