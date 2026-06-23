"""End-to-end: drive the real workflow engine through a CLI runtime provider.

Proves the engine is provider-agnostic — the same `run_workflow_engine` that
the native agent drives also walks branches, resolves slots, and ends when
its `run_segment` / `resolve_unfilled` callbacks come from a
`ClaudeCodeRuntime` backed by a fake CLI.
"""

import asyncio
import stat
import textwrap

from botcircuits.runtime.base import RuntimeConfig
from botcircuits.runtime.providers.claude_code import ClaudeCodeRuntime
from botcircuits.agent.workflow.engine.runner import run_workflow_engine


def _fake_cli_emitting(tmp_path, json_text: str):
    script = tmp_path / "fakeclaude"
    script.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys
        sys.stdout.write({json_text!r})
    """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _runtime(script):
    return ClaudeCodeRuntime(RuntimeConfig(
        name="claude-code",
        command=[str(script), "{prompt}"],
        timeout=30.0,
    ))


def test_engine_walks_branch_via_cli_runtime(tmp_path):
    # The fake CLI always reports approved=true, so the branch must route to
    # the "approve" step and end there.
    script = _fake_cli_emitting(tmp_path, '{"slots": {"approved": true}}')
    rt = _runtime(script)

    flow = {
        "start": "decide",
        "steps": {
            "decide": {
                "id": "decide",
                "type": "agentAction",
                "settings": {"action": "Decide the application"},
                "next": "deny",
                "choices": [{
                    "next": "approve",
                    "operator": "AND",
                    "expressionList": [
                        {"variable": "approved", "operator": "is", "value": "true"}
                    ],
                }],
                "conditions": [{"condition": "approved", "next": "approve"}],
            },
            "approve": {
                "id": "approve", "type": "agentAction",
                "settings": {"action": "Approve it"},
            },
            "deny": {
                "id": "deny", "type": "agentAction",
                "settings": {"action": "Deny it"},
            },
        },
        "variables": [
            {"variableName": "approved", "dataType": "boolean",
             "description": "whether the application is approved"}
        ],
        "segments": [
            {"id": "decide", "steps": ["decide"], "branchStep": "decide"},
            {"id": "approve", "steps": ["approve"], "branchStep": None},
            {"id": "deny", "steps": ["deny"], "branchStep": None},
        ],
    }

    res = asyncio.run(run_workflow_engine(
        flow,
        workflow_name="loan",
        run_segment=lambda **kw: rt.run_segment(**kw),
        resolve_unfilled=lambda **kw: rt.resolve_slots(**kw),
    ))

    assert res.done is True
    assert res.slots.get("approved") is True
    # The branch routed to approve (a decision record marks the matched next).
    matched = [d for d in res.decisions if d.get("matched_next")]
    assert matched and matched[-1]["matched_next"] == "approve"
