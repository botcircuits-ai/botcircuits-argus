"""Grant-on-reply: an affirmative answer to a permission pause grants exactly
the tool(s) that pause was blocked on — nothing parsed out of free text."""

import json

from botcircuits.runtime.run_workflow import (
    _persist_granted_tools,
    _reply_grants_tools,
)


def test_affirmative_reply_grants_the_pending_tool():
    assert _reply_grants_tools("yes use websearch", ["WebSearch"]) == ["WebSearch"]
    assert _reply_grants_tools("ok", ["WebSearch"]) == ["WebSearch"]
    assert _reply_grants_tools("sure, go ahead", ["WebFetch"]) == ["WebFetch"]
    assert _reply_grants_tools("allow it", ["WebSearch"]) == ["WebSearch"]


def test_grants_only_what_the_pause_asked_for():
    # We never parse tool names from the reply text — the pending set decides.
    assert _reply_grants_tools("yes", ["WebSearch", "WebFetch"]) == [
        "WebSearch", "WebFetch",
    ]
    # No pending tools → nothing to grant even on a clear yes.
    assert _reply_grants_tools("yes", []) == []


def test_negative_or_unrelated_reply_grants_nothing():
    assert _reply_grants_tools("no, use a different source", ["WebSearch"]) == []
    assert _reply_grants_tools("try the jobs API instead", ["WebSearch"]) == []
    assert _reply_grants_tools("", ["WebSearch"]) == []
    assert _reply_grants_tools(None, ["WebSearch"]) == []


# -- persisting grants to .claude/settings.json -----------------------------


def _settings(tmp_path):
    return tmp_path / ".claude" / "settings.json"


def test_persist_creates_settings_file_when_absent(tmp_path):
    added = _persist_granted_tools(["WebSearch"], cwd=str(tmp_path))
    assert added == ["WebSearch"]
    data = json.loads(_settings(tmp_path).read_text())
    assert data["permissions"]["allow"] == ["WebSearch"]


def test_persist_merges_preserving_existing(tmp_path):
    p = _settings(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({
        "permissions": {
            "allow": ["Bash(git *)"],
            "additionalDirectories": ["/some/dir"],
        }
    }))
    added = _persist_granted_tools(["WebSearch", "WebFetch"], cwd=str(tmp_path))
    assert added == ["WebSearch", "WebFetch"]
    data = json.loads(p.read_text())
    # Existing entries (and unrelated keys) are preserved.
    assert data["permissions"]["allow"] == ["Bash(git *)", "WebSearch", "WebFetch"]
    assert data["permissions"]["additionalDirectories"] == ["/some/dir"]


def test_persist_is_idempotent(tmp_path):
    _persist_granted_tools(["WebSearch"], cwd=str(tmp_path))
    # Second time: already present → nothing added, no duplicate.
    added = _persist_granted_tools(["WebSearch"], cwd=str(tmp_path))
    assert added == []
    data = json.loads(_settings(tmp_path).read_text())
    assert data["permissions"]["allow"] == ["WebSearch"]


def test_persist_only_adds_missing(tmp_path):
    _persist_granted_tools(["WebSearch"], cwd=str(tmp_path))
    added = _persist_granted_tools(["WebSearch", "WebFetch"], cwd=str(tmp_path))
    assert added == ["WebFetch"]
    data = json.loads(_settings(tmp_path).read_text())
    assert data["permissions"]["allow"] == ["WebSearch", "WebFetch"]


def test_persist_skips_unparseable_file_without_clobbering(tmp_path):
    p = _settings(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("{ this is not valid json")
    added = _persist_granted_tools(["WebSearch"], cwd=str(tmp_path))
    assert added == []
    # Original content left intact — we never overwrite a file we can't parse.
    assert p.read_text() == "{ this is not valid json"


def test_persist_empty_tools_noop(tmp_path):
    assert _persist_granted_tools([], cwd=str(tmp_path)) == []
    assert not _settings(tmp_path).exists()
