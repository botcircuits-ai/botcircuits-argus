"""Tests for the fine-grained tool permission system (agent/permissions.py)
and its wiring into ToolRegistry.run()."""

from __future__ import annotations

import asyncio

import pytest

from botcircuits.agent.permissions import Decision, PermissionRule, PermissionSet
from botcircuits.agent.tools.registry import LocalTool, ToolRegistry


# ---------------------------------------------------------------------------
# Rule parsing
# ---------------------------------------------------------------------------


def test_parse_bare_rule():
    rule = PermissionRule.parse("Read")
    assert rule.tool == "Read"
    assert rule.specifier is None


def test_parse_specifier_rule():
    rule = PermissionRule.parse("Bash(npm run *)")
    assert rule.tool == "Bash"
    assert rule.specifier == "npm run *"


def test_parse_malformed_rule_raises():
    with pytest.raises(ValueError):
        PermissionRule.parse("Bash(npm run *")
    with pytest.raises(ValueError):
        PermissionRule.parse("")


# ---------------------------------------------------------------------------
# Bash / shell_exec matching
# ---------------------------------------------------------------------------


def test_bash_exact_match():
    ps = PermissionSet.from_config({"allow": ["Bash(git status)"]})
    assert ps.evaluate("shell_exec", {"argv": ["git", "status"]}) == Decision.ALLOW
    assert ps.evaluate("shell_exec", {"argv": ["git", "status", "-s"]}) == Decision.UNSPECIFIED


def test_bash_trailing_wildcard_word_boundary():
    ps = PermissionSet.from_config({"allow": ["Bash(npm run *)"]})
    assert ps.evaluate("shell_exec", {"argv": ["npm", "run", "build"]}) == Decision.ALLOW
    assert ps.evaluate("shell_exec", {"argv": ["npmrunfoo"]}) == Decision.UNSPECIFIED


def test_bash_colon_star_suffix_sugar():
    ps = PermissionSet.from_config({"allow": ["Bash(ls:*)"]})
    assert ps.evaluate("shell_exec", {"argv": ["ls", "-la"]}) == Decision.ALLOW
    assert ps.evaluate("shell_exec", {"argv": ["lsof"]}) == Decision.UNSPECIFIED


def test_bash_wildcard_anywhere():
    ps = PermissionSet.from_config({"deny": ["Bash(git * main)"]})
    assert ps.evaluate("shell_exec", {"argv": ["git", "push", "origin", "main"]}) == Decision.DENY
    assert ps.evaluate("shell_exec", {"argv": ["git", "merge", "main"]}) == Decision.DENY
    assert ps.evaluate("shell_exec", {"argv": ["git", "status"]}) == Decision.UNSPECIFIED


def test_bash_bare_tool_matches_everything():
    ps = PermissionSet.from_config({"allow": ["Bash"]})
    assert ps.evaluate("shell_exec", {"argv": ["anything", "goes"]}) == Decision.ALLOW


# ---------------------------------------------------------------------------
# Read / Edit path matching
# ---------------------------------------------------------------------------


def test_read_bare_filename_matches_any_depth():
    ps = PermissionSet.from_config({"deny": ["Read(.env)"]})
    assert ps.evaluate("read_file", {"path": ".env"}) == Decision.DENY
    assert ps.evaluate("read_file", {"path": "nested/dir/.env"}) == Decision.DENY
    assert ps.evaluate("read_file", {"path": "foo.env"}) == Decision.UNSPECIFIED


def test_read_absolute_anchor():
    ps = PermissionSet.from_config({"deny": ["Read(//private/tmp/**)"]})
    assert ps.evaluate("read_file", {"path": "/private/tmp/x.txt"}) == Decision.DENY
    assert ps.evaluate("read_file", {"path": "/etc/passwd"}) == Decision.UNSPECIFIED


def test_read_home_anchor():
    ps = PermissionSet.from_config({"deny": ["Read(~/.ssh/**)"]})
    assert ps.evaluate("read_file", {"path": "~/.ssh/id_rsa"}) == Decision.DENY


def test_edit_project_root_anchor():
    ps = PermissionSet.from_config({"allow": ["Edit(/src/**/*.ts)"]})
    assert ps.evaluate("write_file", {"path": "src/foo.ts", "content": "x"}) == Decision.ALLOW
    assert ps.evaluate("write_file", {"path": "lib/foo.ts", "content": "x"}) == Decision.UNSPECIFIED


def test_read_group_alias_covers_all_read_tools():
    ps = PermissionSet.from_config({"allow": ["Read"]})
    for tool in ("read_file", "list_dir", "glob_search", "grep_search"):
        assert ps.evaluate(tool, {"path": "anything"}) == Decision.ALLOW


def test_concrete_tool_name_rule_is_scoped_to_that_tool():
    ps = PermissionSet.from_config({"deny": ["list_dir(/secret/**)"]})
    assert ps.evaluate("list_dir", {"path": "secret/sub"}) == Decision.DENY
    assert ps.evaluate("read_file", {"path": "secret/sub/f.txt"}) == Decision.UNSPECIFIED


# ---------------------------------------------------------------------------
# Precedence: deny > ask > allow, first match wins regardless of specificity
# ---------------------------------------------------------------------------


def test_deny_beats_more_specific_allow():
    ps = PermissionSet.from_config({
        "allow": ["Bash(aws s3 ls)"],
        "deny": ["Bash(aws *)"],
    })
    assert ps.evaluate("shell_exec", {"argv": ["aws", "s3", "ls"]}) == Decision.DENY


def test_ask_beats_more_specific_allow():
    ps = PermissionSet.from_config({
        "allow": ["Bash(git push origin main)"],
        "ask": ["Bash(git push *)"],
    })
    assert ps.evaluate("shell_exec", {"argv": ["git", "push", "origin", "main"]}) == Decision.ASK


def test_no_match_is_unspecified():
    ps = PermissionSet()
    assert ps.evaluate("read_file", {"path": "x"}) == Decision.UNSPECIFIED
    assert ps.is_empty()


# ---------------------------------------------------------------------------
# Built-in read-only command allowlist
# ---------------------------------------------------------------------------


def test_builtin_read_only_commands_auto_allowed():
    ps = PermissionSet()
    assert ps.evaluate("shell_exec", {"argv": ["pwd"]}) == Decision.ALLOW
    assert ps.evaluate("shell_exec", {"argv": ["ls", "-la"]}) == Decision.ALLOW
    assert ps.evaluate("shell_exec", {"argv": ["cat", "foo.txt"]}) == Decision.ALLOW


def test_non_read_only_command_stays_unspecified():
    ps = PermissionSet()
    assert ps.evaluate("shell_exec", {"argv": ["rm", "-rf", "/"]}) == Decision.UNSPECIFIED


def test_explicit_ask_overrides_builtin_read_only():
    ps = PermissionSet.from_config({"ask": ["Bash(pwd)"]})
    assert ps.evaluate("shell_exec", {"argv": ["pwd"]}) == Decision.ASK


def test_explicit_deny_overrides_builtin_read_only():
    ps = PermissionSet.from_config({"deny": ["Bash(cat *)"]})
    assert ps.evaluate("shell_exec", {"argv": ["cat", "secret"]}) == Decision.DENY


def test_builtin_read_only_does_not_leak_to_other_tools():
    ps = PermissionSet()
    assert ps.evaluate("read_file", {"path": "pwd"}) == Decision.UNSPECIFIED


# ---------------------------------------------------------------------------
# ToolRegistry wiring
# ---------------------------------------------------------------------------


def _make_registry(permissions: dict | None) -> tuple[ToolRegistry, list]:
    calls: list = []

    async def handler(args: dict) -> dict:
        calls.append(args)
        return {"ok": True}

    reg = ToolRegistry(permissions=PermissionSet.from_config(permissions))
    reg.register(LocalTool(
        name="read_file", description="d", input_schema={}, handler=handler,
    ))
    return reg, calls


def test_registry_deny_blocks_handler_invocation():
    reg, calls = _make_registry({"deny": ["Read(secret.txt)"]})
    text, is_error = asyncio.run(reg.run("read_file", {"path": "secret.txt"}))
    assert is_error is True
    assert "Permission denied" in text
    assert calls == []


def test_registry_allow_runs_handler():
    reg, calls = _make_registry({"allow": ["Read"]})
    text, is_error = asyncio.run(reg.run("read_file", {"path": "ok.txt"}))
    assert is_error is False
    assert calls == [{"path": "ok.txt"}]


def test_registry_unspecified_runs_handler_unchanged():
    reg, calls = _make_registry(None)
    text, is_error = asyncio.run(reg.run("read_file", {"path": "ok.txt"}))
    assert is_error is False
    assert calls == [{"path": "ok.txt"}]
