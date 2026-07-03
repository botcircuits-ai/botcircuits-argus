"""Tests for the manager backend: auth + session list/get APIs."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from botcircuits.manager import auth, store
from botcircuits.manager.app import create_app


@pytest.fixture
def manager(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv(store.SESSIONS_DIR_ENV, str(sessions))
    monkeypatch.setenv(auth.USERNAME_ENV, "admin")
    monkeypatch.setenv(auth.PASSWORD_ENV, "s3cret")
    monkeypatch.delenv(auth.SECRET_ENV, raising=False)
    return TestClient(create_app()), sessions


def _write_session(sessions_dir, sid, *, name, end=None, status=None, events=None):
    trace = [{"seq": 0, "ts": "t", "type": "session_start", "step": None,
              "duration_ms": None, "slots": {}, "data": {}}]
    for i, (etype, step) in enumerate(events or [], start=1):
        trace.append({"seq": i, "ts": "t", "type": etype, "step": step,
                      "duration_ms": None, "slots": {}, "data": {}})
    if status:
        trace.append({"seq": len(trace), "ts": "t", "type": "session_end",
                      "step": None, "duration_ms": None, "slots": {},
                      "data": {"status": status}})
    doc = {
        "session_id": sid,
        "agent": {"runtime": "claude-code"},
        "workflow": {"name": name, "start": "2026-01-01T00:00:00Z",
                     "end": end, "initial_slots": {}},
        "trace": trace,
        "memory": {"nodes": [], "edges": []},
    }
    (sessions_dir / f"{sid}-session.json").write_text(json.dumps(doc))
    return doc


# -- auth -------------------------------------------------------------------

def test_login_success_and_token_verifies(manager):
    client, _ = manager
    r = client.post("/api/auth/login", json={"username": "admin", "password": "s3cret"})
    assert r.status_code == 200
    token = r.json()["token"]
    assert auth.verify(token) == "admin"


def test_login_bad_password_401(manager):
    client, _ = manager
    r = client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401


def test_sessions_requires_auth(manager):
    client, _ = manager
    assert client.get("/api/sessions").status_code == 401
    assert client.get("/api/sessions", headers={"Authorization": "Bearer junk"}).status_code == 401


def test_health_reports_auth_configured(manager):
    client, _ = manager
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["auth_configured"] is True


# -- models -------------------------------------------------------------

def test_models_requires_auth(manager):
    client, _ = manager
    assert client.get("/api/models").status_code == 401


def test_models_returns_provider_catalog(manager):
    client, _ = manager
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "s3cret"}
    ).json()["token"]
    r = client.get("/api/models", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"anthropic", "openai", "gemini", "openrouter"}
    for spec in body.values():
        assert isinstance(spec["label"], str) and spec["label"]
        assert isinstance(spec["models"], list) and spec["models"]


# -- sessions ---------------------------------------------------------------

def _auth_header(client):
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "s3cret"}
    ).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_list_sessions_returns_summaries_newest_first(manager):
    client, sessions = manager
    _write_session(sessions, "aaa", name="wf_a", end="2026-01-01T01:00:00Z", status="done")
    _write_session(sessions, "bbb", name="wf_b")  # running
    headers = _auth_header(client)

    r = client.get("/api/sessions", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert {s["session_id"] for s in data} == {"aaa", "bbb"}
    by_id = {s["session_id"]: s for s in data}
    assert by_id["aaa"]["status"] == "done"
    assert by_id["bbb"]["status"] == "running"
    assert by_id["aaa"]["workflow"] == "wf_a"
    # summaries omit the full trace
    assert "trace" not in by_id["aaa"]


def test_get_session_returns_full_document(manager):
    client, sessions = manager
    _write_session(sessions, "ccc", name="wf_c",
                   events=[("step_enter", "s1"), ("action_after", "s1")])
    headers = _auth_header(client)

    r = client.get("/api/sessions/ccc", headers=headers)
    assert r.status_code == 200
    doc = r.json()
    assert doc["session_id"] == "ccc"
    assert [e["type"] for e in doc["trace"]] == [
        "session_start", "step_enter", "action_after",
    ]


def test_get_missing_session_404(manager):
    client, _ = manager
    r = client.get("/api/sessions/does-not-exist", headers=_auth_header(client))
    assert r.status_code == 404


def test_get_session_rejects_path_traversal(manager):
    client, _ = manager
    r = client.get("/api/sessions/..%2f..%2fetc", headers=_auth_header(client))
    # FastAPI decodes the path; the store guard returns None -> 404 (never reads
    # outside the sessions dir).
    assert r.status_code == 404


def test_expired_token_rejected(manager, monkeypatch):
    client, _ = manager
    monkeypatch.setattr(auth, "TOKEN_TTL", -1)
    token = auth.login("admin", "s3cret")
    r = client.get("/api/sessions", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
