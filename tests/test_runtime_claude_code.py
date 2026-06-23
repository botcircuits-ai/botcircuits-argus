"""CLI runtime provider end-to-end against a FAKE `claude` script.

The fake binary echoes a canned JSON object on stdout, so we exercise the
real subprocess path (`cli_exec.run_cli`) + parsing without a live agent.
Async entry points are driven with `asyncio.run`, matching the suite's
convention (see tests/test_workflow_engine_runner.py).
"""

import asyncio
import stat
import textwrap

from botcircuits.runtime.base import RuntimeConfig
from botcircuits.runtime.providers.claude_code import ClaudeCodeRuntime


def _write_fake_cli(tmp_path, stdout_text: str, *, rc: int = 0):
    """Create an executable that prints `stdout_text` and exits with `rc`."""
    script = tmp_path / "fakeclaude"
    script.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys
        sys.stdout.write({stdout_text!r})
        sys.exit({rc})
    """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _runtime(script):
    return ClaudeCodeRuntime(RuntimeConfig(
        name="claude-code",
        command=[str(script), "-p", "{prompt}", "--output-format", "json"],
        timeout=30.0,
    ))


def test_run_segment_captures_slots(tmp_path):
    script = _write_fake_cli(tmp_path, '{"slots": {"approved": true}, "text": "ok"}')
    rt = _runtime(script)
    res = asyncio.run(rt.run_segment(
        actions=["Decide the application"],
        branch_variables=[{"variableName": "approved", "dataType": "boolean"}],
        system_notes=[],
        slots={},
    ))
    assert res.captured_slots == {"approved": True}
    assert res.paused is False


def test_run_segment_pause(tmp_path):
    script = _write_fake_cli(tmp_path, '{"paused": true, "question": "Income?"}')
    rt = _runtime(script)
    res = asyncio.run(rt.run_segment(
        actions=["Ask for income"], branch_variables=[], system_notes=[], slots={},
    ))
    assert res.paused is True
    assert res.question == "Income?"


def test_run_segment_list_items(tmp_path):
    script = _write_fake_cli(
        tmp_path, '{"items": [{"sku": "A", "qty": 2}, {"sku": "B", "qty": 1}]}'
    )
    rt = _runtime(script)
    res = asyncio.run(rt.run_segment(
        actions=["Price each line"], branch_variables=[], system_notes=[],
        slots={}, item_variables=[{"variableName": "sku", "dataType": "string"}],
    ))
    assert res.captured_items == [{"sku": "A", "qty": 2}, {"sku": "B", "qty": 1}]


def test_missing_binary_pauses_not_crashes(tmp_path):
    rt = ClaudeCodeRuntime(RuntimeConfig(
        name="claude-code",
        command=[str(tmp_path / "does-not-exist"), "{prompt}"],
    ))
    res = asyncio.run(rt.run_segment(
        actions=["x"], branch_variables=[], system_notes=[], slots={},
    ))
    assert res.paused is True
    assert "could not be started" in res.question


def _write_prompt_echo_cli(tmp_path):
    """A fake CLI that writes the prompt it received to `prompt.txt` (so the
    test can assert on it) and prints a benign capture, exercising the real
    prompt-building path."""
    out_file = tmp_path / "prompt.txt"
    script = tmp_path / "echocli"
    script.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys
        # The prompt is passed as the argument after `-p` (see _runtime).
        argv = sys.argv[1:]
        prompt = ""
        for i, a in enumerate(argv):
            if a == "-p" and i + 1 < len(argv):
                prompt = argv[i + 1]
                break
        open({str(out_file)!r}, "w").write(prompt)
        sys.stdout.write('{{"slots": {{"again": "yes"}}, "paused": false}}')
    """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script, out_file


def test_resume_prompt_instructs_to_consume_reply_not_reask(tmp_path):
    """On resume (a `__last_user_message__` is present) the segment prompt must
    explicitly tell the agent to treat the reply as the answer and NOT re-ask —
    otherwise a branching question (e.g. a retry loop) re-asks forever."""
    script, out_file = _write_prompt_echo_cli(tmp_path)
    rt = _runtime(script)
    res = asyncio.run(rt.run_segment(
        actions=["Ask: check another order? (yes/no)"],
        branch_variables=[{"variableName": "again", "dataType": "string"}],
        system_notes=[],
        slots={"__last_user_message__": "yes"},
    ))
    prompt = out_file.read_text()
    assert "RESUMING AFTER A PAUSE" in prompt
    assert "do NOT re-ask" in prompt
    assert "yes" in prompt  # the user's reply is surfaced
    # The fake agent honored it: captured the slot instead of pausing.
    assert res.captured_slots == {"again": "yes"}
    assert res.paused is False


def test_no_resume_guidance_without_user_reply(tmp_path):
    """A fresh (non-resume) segment carries no reply, so no resume block — the
    agent should ask the question normally the first time."""
    script, out_file = _write_prompt_echo_cli(tmp_path)
    rt = _runtime(script)
    asyncio.run(rt.run_segment(
        actions=["Ask: check another order? (yes/no)"],
        branch_variables=[{"variableName": "again", "dataType": "string"}],
        system_notes=[],
        slots={},
    ))
    prompt = out_file.read_text()
    assert "RESUMING AFTER A PAUSE" not in prompt


def test_resolve_slots_tier0_deterministic_beats_cli(tmp_path):
    # Tier-0 should resolve a number from the last user message WITHOUT
    # invoking the CLI. The fake CLI would return 999; Tier-0 returns 42, and
    # because nothing is left unresolved the CLI is never consulted.
    script = _write_fake_cli(tmp_path, '{"normalized": {"amount": 999}}')
    rt = _runtime(script)
    flow = {
        "steps": {
            "s1": {
                "type": "question",
                "choices": [{
                    "expressionList": [
                        {"variable": "amount", "operator": "greater than", "value": "100"}
                    ],
                }],
            }
        }
    }
    out = asyncio.run(rt.resolve_slots(
        flow=flow, step_id="s1",
        variables=[{"variableName": "amount", "dataType": "number"}],
        slots={"__last_user_message__": "my amount is 42"},
    ))
    assert out == {"amount": 42}


def test_allowed_tools_appended_to_spawn_argv(tmp_path):
    # A fake CLI that echoes ITS OWN argv back inside the JSON `text`, so we
    # can assert --allowedTools <tool> reached the subprocess.
    script = tmp_path / "echoargs"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "print(json.dumps({'slots': {}, 'text': ' '.join(sys.argv[1:])}))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    rt = ClaudeCodeRuntime(RuntimeConfig(
        name="claude-code",
        command=[str(script), "-p", "{prompt}", "--output-format", "json"],
        timeout=30.0,
        allowed_tools=["WebSearch", "WebFetch"],
    ))
    res = asyncio.run(rt.run_segment(
        actions=["search"], branch_variables=[], system_notes=[], slots={},
    ))
    assert "--allowedTools WebSearch WebFetch" in res.text


def test_no_allowed_tools_means_no_flag(tmp_path):
    script = tmp_path / "echoargs2"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "print(json.dumps({'slots': {}, 'text': ' '.join(sys.argv[1:])}))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    rt = ClaudeCodeRuntime(RuntimeConfig(
        name="claude-code",
        command=[str(script), "-p", "{prompt}", "--output-format", "json"],
        timeout=30.0,
    ))
    res = asyncio.run(rt.run_segment(
        actions=["x"], branch_variables=[], system_notes=[], slots={},
    ))
    assert "--allowedTools" not in res.text


def test_data_variable_reported_slot_is_captured(tmp_path):
    # The CLI contract carries ANY reported slot key, so a non-branch data
    # variable (e.g. scraped_jobs) survives the process hop into slots.
    script = _write_fake_cli(
        tmp_path,
        '{"slots": {"scraped_jobs": "[{\\"t\\":\\"SWE\\"}]", "job_count": 3}, '
        '"text": "scraped"}',
    )
    rt = _runtime(script)
    res = asyncio.run(rt.run_segment(
        actions=["scrape jobs"],
        branch_variables=[{"variableName": "job_count", "dataType": "number"}],
        system_notes=[],
        slots={},
        data_variables=[{"variableName": "scraped_jobs", "dataType": "string"}],
    ))
    assert res.captured_slots["scraped_jobs"] == '[{"t":"SWE"}]'
    assert res.captured_slots["job_count"] == 3


def test_carried_data_value_injected_into_prompt(tmp_path):
    # A fake CLI that echoes the PROMPT it received (the -p arg) back inside
    # the JSON `text`, so we can assert the carried data value reached it.
    script = tmp_path / "echoprompt"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "# argv: -p <prompt> --output-format json ...\n"
        "prompt = sys.argv[2] if len(sys.argv) > 2 else ''\n"
        "print(json.dumps({'slots': {}, 'text': prompt}))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    rt = ClaudeCodeRuntime(RuntimeConfig(
        name="claude-code",
        command=[str(script), "-p", "{prompt}", "--output-format", "json"],
        timeout=30.0,
    ))
    res = asyncio.run(rt.run_segment(
        actions=["Print tasks_data and write it to a file"],
        branch_variables=[],
        system_notes=[],
        slots={"tasks_data": '[{"title":"Test 1","id":"1"}]', "task_count": 1},
        data_variables=[
            {"variableName": "tasks_data", "dataType": "string"},
            {"variableName": "created_task", "dataType": "string"},
        ],
    ))
    # The carried value is surfaced under AVAILABLE DATA so the stateless
    # segment can act on it.
    assert "AVAILABLE DATA" in res.text
    assert "Test 1" in res.text
    # A data var with no value yet (created_task) is NOT listed as available
    # (it may still appear in the report-schema, but not as an available value).
    available_block = res.text.split("AVAILABLE DATA", 1)[1]
    assert "created_task" not in available_block
    assert "tasks_data" in available_block


def test_no_available_data_block_when_data_slots_empty(tmp_path):
    script = tmp_path / "echoprompt2"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "prompt = sys.argv[2] if len(sys.argv) > 2 else ''\n"
        "print(json.dumps({'slots': {}, 'text': prompt}))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    rt = ClaudeCodeRuntime(RuntimeConfig(
        name="claude-code",
        command=[str(script), "-p", "{prompt}", "--output-format", "json"],
        timeout=30.0,
    ))
    res = asyncio.run(rt.run_segment(
        actions=["fetch the tasks"],
        branch_variables=[{"variableName": "task_count", "dataType": "number"}],
        system_notes=[],
        slots={},  # nothing produced yet
        data_variables=[{"variableName": "tasks_data", "dataType": "string"}],
    ))
    assert "AVAILABLE DATA" not in res.text
