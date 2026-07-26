"""`build_workflow` tool's automatic verification-gate generation hook —
runs after a successful build, best-effort/non-fatal on failure."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.fakes import ScriptedProvider, text_response

from botcircuits.agent.tools.builtins.build_workflow import build_workflow_tool
from botcircuits.agent.workflow import paths


@pytest.fixture(autouse=True)
def _isolated_workflows_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.WORKFLOWS_DIR_ENV, str(tmp_path))
    return tmp_path


def _payload() -> dict:
    return {
        "summary": "A trivial one-step workflow.",
        "workflow": {
            "name": "wf_gatetest",
            "description": "test",
            "steps": {
                "start": {"type": "start", "next": "s1"},
                "s1": {"type": "agentAction",
                       "settings": {"action": "Say hello."}},
            },
        },
    }


_GATE_REPLY = json.dumps({
    "checks": [
        {
            "id": "says_hello",
            "type": "script",
            "description": "output mentions hello",
            "severity": "blocking",
            "code": (
                "import json, sys\n"
                "run = json.loads(sys.stdin.read())\n"
                "ok = 'hello' in (run.get('summary') or '').lower()\n"
                "print(json.dumps({'passed': ok, 'detail': 'checked summary'}))\n"
            ),
        },
    ]
})


def test_successful_build_writes_gate_manifest_and_check_script(tmp_path):
    # No `conditions` in the workflow, so the condition indexer takes its
    # zero-LLM-call early return — only the gate-generation call hits the
    # provider, keeping this test to one scripted response.
    provider = ScriptedProvider([text_response(_GATE_REPLY)])
    tool = build_workflow_tool(provider=provider, auto=True)

    result = asyncio.run(tool.handler(_payload()))
    assert result.get("error") is None
    assert result["indexed"] is True
    assert result["gate"] == {"checks": 1}

    manifest_path = paths.gate_manifest_path("wf_gatetest")
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["checks"][0]["id"] == "says_hello"
    assert manifest["checks"][0]["script"] == "verifications/checks/says_hello.py"

    script_path = paths.gate_checks_dir("wf_gatetest") / "says_hello.py"
    assert script_path.is_file()
    assert "hello" in script_path.read_text(encoding="utf-8")


def test_gate_generation_failure_does_not_fail_the_build(tmp_path):
    # Gate-generation call returns unparseable garbage; the build itself
    # must still succeed (best-effort, non-fatal per generator.py's docstring).
    provider = ScriptedProvider([text_response("not json at all")])
    tool = build_workflow_tool(provider=provider, auto=True)

    result = asyncio.run(tool.handler(_payload()))
    assert result.get("error") is None
    assert result["indexed"] is True
    assert "gate_error" in result
    assert paths.gate_manifest_path("wf_gatetest").exists() is False


def test_no_provider_skips_gate_generation_silently():
    tool = build_workflow_tool(provider=None, auto=True)
    result = asyncio.run(tool.handler(_payload()))
    assert result.get("error") is None
    # No provider means no indexing either (existing behavior) — and gate
    # generation is skipped rather than attempted without a provider.
    assert result.get("gate") is None
    assert result.get("gate_error") is None
