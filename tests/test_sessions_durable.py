"""Durable sessions + episodic recall (`agent/sessions.py`).

Conversation is not state: the durable store persists each session as
JSON-L so a killed agent can resume, and `search_sessions` /
`search_memory` recover facts from sessions that aren't in the current
context. These tests pin:

  - save/load round-trip (system prompt + messages, block shapes intact)
  - crash resilience: a half-written trailing line must not kill resume
  - session ids from user input can't escape the sessions dir
  - reset deletes the file (reset means gone, not "back after restart")
  - keyword search ranks by term hits and excludes the current session
  - the agent loop persists after every turn, including a pause
"""

from __future__ import annotations

import asyncio
import json

from botcircuits.agent.loop import Agent
from botcircuits.agent.sessions import (
    DurableConversationStore,
    delete_session,
    list_saved_sessions,
    load_session,
    save_session,
    search_sessions,
)
from botcircuits.agent.tools import ToolRegistry
from botcircuits.agent.tools.builtins.search_memory import search_memory_tool
from botcircuits.providers.base import LLMProvider
from botcircuits.types import LLMResponse, Message


def _msg(role: str, text: str) -> Message:
    return Message(role=role, blocks=[{"type": "text", "text": text}])


# -- persistence ------------------------------------------------------------


def test_save_load_roundtrip(tmp_path):
    messages = [
        _msg("user", "hello"),
        Message(role="assistant", blocks=[
            {"type": "text", "text": "hi"},
            {"type": "tool_call", "id": "t1", "name": "now", "arguments": {},
             "thought_signature": b"\x00\x01"},
        ]),
        Message(role="user", blocks=[
            {"type": "tool_result", "tool_call_id": "t1", "name": "now",
             "content": "2026-07-14", "is_error": False},
        ]),
    ]
    save_session("s1", "be terse", messages, tmp_path)

    system, loaded = load_session("s1", tmp_path)
    assert system == "be terse"
    assert [m.role for m in loaded] == ["user", "assistant", "user"]
    assert loaded[0].blocks == [{"type": "text", "text": "hello"}]
    # Opaque provider bytes are dropped on save, not corrupted into text.
    assert "thought_signature" not in loaded[1].blocks[1]
    assert loaded[2].blocks[0]["content"] == "2026-07-14"


def test_load_missing_returns_none(tmp_path):
    assert load_session("nope", tmp_path) is None


def test_corrupt_trailing_line_does_not_kill_resume(tmp_path):
    save_session("s1", "sys", [_msg("user", "hello")], tmp_path)
    path = tmp_path / "s1.jsonl"
    path.write_text(path.read_text() + '{"role": "assis', encoding="utf-8")

    system, loaded = load_session("s1", tmp_path)
    assert system == "sys"
    assert len(loaded) == 1


def test_session_id_cannot_escape_base_dir(tmp_path):
    base = tmp_path / "sessions"
    save_session("../evil", "sys", [_msg("user", "x")], base)
    # Written INSIDE base under the sanitized final component.
    assert (base / "evil.jsonl").is_file()
    assert not (tmp_path / "evil.jsonl").exists()
    delete_session("../evil", base)
    assert not (base / "evil.jsonl").exists()


def test_list_saved_sessions_counts_and_orders(tmp_path):
    save_session("old", "sys", [_msg("user", "a")], tmp_path)
    save_session("new", "sys", [_msg("user", "a"), _msg("assistant", "b")], tmp_path)
    (tmp_path / "old.jsonl").touch()  # can't rely on write order for mtime
    sessions = list_saved_sessions(tmp_path)
    assert [s["name"] for s in sessions] == ["old", "new"]
    by_name = {s["name"]: s["messages"] for s in sessions}
    assert by_name == {"old": 1, "new": 2}  # meta row not counted


# -- store ------------------------------------------------------------------


def test_durable_store_resumes_from_disk(tmp_path):
    store = DurableConversationStore(tmp_path)
    convo = store.get_or_create("s1", system="be terse")
    convo.messages.append(_msg("user", "remember 42"))
    store.persist("s1")

    fresh = DurableConversationStore(tmp_path)  # new process
    resumed = fresh.get_or_create("s1")
    assert resumed.system == convo.system  # frozen system restored, not rebuilt
    assert resumed.messages[0].blocks[0]["text"] == "remember 42"


def test_durable_store_reset_deletes_file(tmp_path):
    store = DurableConversationStore(tmp_path)
    store.get_or_create("s1")
    store.persist("s1")
    assert (tmp_path / "s1.jsonl").is_file()

    store.reset("s1")
    assert not (tmp_path / "s1.jsonl").exists()
    assert DurableConversationStore(tmp_path).get_or_create("s1").messages == []


# -- episodic search ----------------------------------------------------------


def _seed_sessions(base):
    save_session("orders", "sys", [
        _msg("user", "the warehouse passcode is 7741"),
        _msg("assistant", "Noted: passcode 7741 for the warehouse."),
    ], base)
    save_session("chitchat", "sys", [_msg("user", "nice weather today")], base)
    save_session("current", "sys", [_msg("user", "warehouse passcode?")], base)


def test_search_ranks_by_term_hits_and_excludes_current(tmp_path):
    _seed_sessions(tmp_path)
    hits = search_sessions("warehouse passcode?", tmp_path, exclude="current")
    assert hits, "expected matches"
    assert all(h["session"] != "current" for h in hits)
    # Best hit mentions both terms.
    assert "passcode" in hits[0]["content"].lower()
    assert "warehouse" in hits[0]["content"].lower()


def test_search_empty_query_or_missing_dir(tmp_path):
    assert search_sessions("???", tmp_path) == []
    assert search_sessions("anything", tmp_path / "absent") == []


def test_search_memory_tool_excludes_current_session(tmp_path, monkeypatch):
    monkeypatch.setenv("BOTCIRCUITS_SESSIONS_DIR", str(tmp_path))
    _seed_sessions(tmp_path)
    tool = search_memory_tool()

    text = tool.handler({"query": "warehouse passcode"},
                        {"session_id": "current"})
    assert "7741" in text
    assert "[current]" not in text

    assert tool.handler({"query": ""}, {}) .startswith("`query`")
    assert tool.handler({"query": "zebra unicorn"}, {}) == "no matching memory found"


# -- loop integration ---------------------------------------------------------


class OneReplyProvider(LLMProvider):
    name = "scripted"
    model = "test"

    async def complete(self, system, messages, tools, hosted_mcp,
                       skills, max_tokens) -> LLMResponse:
        return LLMResponse(text="ok", tool_calls=[],
                           stop_reason="end_turn", raw=None)

    async def stream(self, system, messages, tools, hosted_mcp,
                     skills, max_tokens):
        resp = await self.complete(system, messages, tools, hosted_mcp,
                                   skills, max_tokens)
        yield "text_delta", resp.text
        yield "final", resp

    async def aclose(self):
        pass


def test_agent_persists_after_each_turn(tmp_path):
    async def run():
        store = DurableConversationStore(tmp_path)
        agent = Agent(provider=OneReplyProvider(), tools=ToolRegistry(),
                      local_skills_paths=[], enable_workflows=False,
                      store=store)
        reply, sid = await agent.chat("hello", session_id="s1")
        assert reply == "ok"
        return sid

    sid = asyncio.run(run())
    rows = [json.loads(l) for l in
            (tmp_path / f"{sid}.jsonl").read_text().splitlines()]
    roles = [r.get("role") for r in rows if "role" in r]
    assert roles == ["user", "assistant"]
