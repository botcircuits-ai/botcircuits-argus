"""Hermes runtime provider against a FAKE `hermes` binary.

Hermes oneshot prints the JSON contract as PLAIN final text (no
`--output-format json` envelope) and hides usage from stdout (it lives in the
session store). These tests exercise:

  * the shared claude-code segment/slot contract still parses hermes' bare
    text output (inheritance is wired correctly);
  * the `-z {prompt} --yolo` argv reaches the subprocess;
  * usage is harvested from a fake `state.db` via HERMES_HOME, NOT stdout.

Async entry points use `asyncio.run`, matching the suite convention.
"""

import asyncio
import sqlite3
import stat
import textwrap
import time

from botcircuits.runtime.base import RuntimeConfig
from botcircuits.runtime.providers.hermes import HermesRuntime
from botcircuits.usage.hermes_usage import harvest_usage


def _write_fake_hermes(tmp_path, stdout_text: str, *, rc: int = 0):
    """A fake `hermes` that prints `stdout_text` (plain, no JSON envelope)."""
    script = tmp_path / "fakehermes"
    script.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys
        sys.stdout.write({stdout_text!r})
        sys.exit({rc})
    """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _runtime(script):
    return HermesRuntime(RuntimeConfig(
        name="hermes",
        command=[str(script), "-z", "{prompt}", "--yolo"],
        timeout=30.0,
    ))


def test_run_segment_parses_bare_text_json(tmp_path):
    # Hermes prints the contract object as plain text — no `result` envelope.
    rt = _runtime(_write_fake_hermes(
        tmp_path, '{"slots": {"approved": true}, "text": "ok"}'))
    res = asyncio.run(rt.run_segment(
        actions=["Decide the application"],
        branch_variables=[{"variableName": "approved", "dataType": "boolean"}],
        system_notes=[],
        slots={},
    ))
    assert res.captured_slots == {"approved": True}
    assert res.paused is False


def test_run_segment_parses_fenced_json(tmp_path):
    # A model often wraps its final answer in a ```json fence — still parsed.
    rt = _runtime(_write_fake_hermes(
        tmp_path, '```json\n{"paused": true, "question": "Income?"}\n```'))
    res = asyncio.run(rt.run_segment(
        actions=["Ask for income"], branch_variables=[], system_notes=[], slots={},
    ))
    assert res.paused is True
    assert res.question == "Income?"


def test_yolo_oneshot_argv_reaches_subprocess(tmp_path):
    # Echo argv back inside the contract `text` so we can assert -z/--yolo.
    script = tmp_path / "echoargs"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, json\n"
        "print(json.dumps({'slots': {}, 'text': ' '.join(sys.argv[1:])}))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    rt = _runtime(script)
    res = asyncio.run(rt.run_segment(
        actions=["x"], branch_variables=[], system_notes=[], slots={},
    ))
    assert "-z" in res.text
    assert "--yolo" in res.text


def test_missing_binary_pauses_not_crashes(tmp_path):
    rt = HermesRuntime(RuntimeConfig(
        name="hermes",
        command=[str(tmp_path / "no-such-hermes"), "-z", "{prompt}", "--yolo"],
    ))
    res = asyncio.run(rt.run_segment(
        actions=["x"], branch_variables=[], system_notes=[], slots={},
    ))
    assert res.paused is True
    assert "could not be started" in res.question


def test_resume_block_inherited(tmp_path):
    """The resume-after-pause prompt logic is inherited from the base class."""
    out_file = tmp_path / "prompt.txt"
    script = tmp_path / "echoprompt"
    script.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys
        # argv: -z <prompt> --yolo
        prompt = sys.argv[2] if len(sys.argv) > 2 else ""
        open({str(out_file)!r}, "w").write(prompt)
        sys.stdout.write('{{"slots": {{"again": "yes"}}, "paused": false}}')
    """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
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
    assert res.captured_slots == {"again": "yes"}


# ---- usage harvest from the session store --------------------------------

def _seed_state_db(home, prompt: str, *, started_at: float, sid: str = "s1",
                   input_tokens=1000, output_tokens=200, api_calls=4,
                   cache_read=50, cache_write=10):
    """Create a minimal hermes-shaped state.db under `home`/.hermes."""
    db_dir = home / ".hermes"
    db_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_dir / "state.db")
    con.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, "
        "started_at REAL, input_tokens INT, output_tokens INT, "
        "api_call_count INT, tool_call_count INT, model TEXT, "
        "cache_read_tokens INT, cache_write_tokens INT)"
    )
    con.execute(
        "CREATE TABLE messages (rowid INTEGER PRIMARY KEY, session_id TEXT, "
        "role TEXT, content TEXT, tool_calls TEXT, tool_name TEXT)"
    )
    con.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, "cli", started_at, input_tokens, output_tokens, api_calls,
         2, "anthropic/claude-sonnet-4.6", cache_read, cache_write),
    )
    con.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls, tool_name) "
        "VALUES (?,?,?,?,?)",
        (sid, "user", prompt, None, None),
    )
    con.commit()
    con.close()


def test_harvest_usage_reads_session_store(tmp_path, monkeypatch):
    prompt = "Do the segment thing"
    t0 = time.time()
    _seed_state_db(tmp_path, prompt, started_at=t0)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    usage = harvest_usage(prompt, t0 + 1)  # launched just after session start
    assert usage is not None
    # input_tokens is made TOTAL: stored input + cache_read + cache_write.
    assert usage.input_tokens == 1000 + 50 + 10
    assert usage.output_tokens == 200
    assert usage.cache_read_tokens == 50
    assert usage.cache_write_tokens == 10
    assert usage.calls == 4
    assert usage.runtime == "hermes"


def test_harvest_usage_none_when_no_match(tmp_path, monkeypatch):
    _seed_state_db(tmp_path, "some other prompt", started_at=time.time())
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    assert harvest_usage("a prompt that was never run", time.time()) is None


def test_harvest_usage_none_when_no_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "empty"))
    assert harvest_usage("anything", time.time()) is None


def test_claimed_session_not_harvested_twice(tmp_path, monkeypatch):
    prompt = "identical prompt"
    t0 = time.time()
    _seed_state_db(tmp_path, prompt, started_at=t0)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    claimed: set[str] = set()
    first = harvest_usage(prompt, t0 + 1, claimed=claimed)
    assert first is not None
    assert "s1" in claimed
    # Second segment with the SAME prompt can't re-claim the one session row.
    second = harvest_usage(prompt, t0 + 1, claimed=claimed)
    assert second is None


def test_run_segment_usage_none_when_no_store(tmp_path, monkeypatch):
    """Hermes stdout carries no usage block, so with no session store to
    harvest, run_segment leaves usage None (rather than the bogus stdout
    estimate claude-code would parse) — and never crashes."""
    script = _write_fake_hermes(tmp_path, '{"slots": {"ok": true}}')
    rt = _runtime(script)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "no-store"))
    res = asyncio.run(rt.run_segment(
        actions=["x"], branch_variables=[], system_notes=[], slots={},
    ))
    assert res.captured_slots == {"ok": True}
    assert res.usage is None


def test_run_segment_attaches_harvested_usage(tmp_path, monkeypatch):
    """End to end: with the session store seeded for the exact prompt hermes
    received, run_segment attaches the store's real usage onto the result —
    even though stdout has none. We capture the built prompt from the fake CLI,
    seed the store for it, and re-run."""
    captured = tmp_path / "prompt.txt"
    script = tmp_path / "capture_hermes"
    script.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys
        prompt = sys.argv[2] if len(sys.argv) > 2 else ""
        open({str(captured)!r}, "w").write(prompt)
        sys.stdout.write('{{"slots": {{"ok": true}}}}')
    """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    rt = _runtime(script)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

    # First run: discover the exact prompt the engine builds for this segment.
    asyncio.run(rt.run_segment(
        actions=["x"], branch_variables=[], system_notes=[], slots={},
    ))
    built_prompt = captured.read_text()

    # Seed a session for that prompt, then run again and expect harvested usage.
    _seed_state_db(tmp_path, built_prompt, started_at=time.time())
    res = asyncio.run(rt.run_segment(
        actions=["x"], branch_variables=[], system_notes=[], slots={},
    ))
    assert res.usage is not None
    assert res.usage.runtime == "hermes"
    assert res.usage.input_tokens == 1000 + 50 + 10
    assert res.usage.calls == 4
