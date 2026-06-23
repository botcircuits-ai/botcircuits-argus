"""Token-usage accounting: run-time real usage and authoring footprint.

Covers the two mechanisms in `botcircuits.usage`:

  - `usage_from_stdout` parsing CLI agents' `usage` blocks (claude-code /
    codex / openclaw envelopes), `ActionUsage`/`RunUsage` accumulation, and
    the engine folding per-segment usage into a run total.
  - `token_counter` provider-aware counting + `token_footprint` for the
    workflow definition size estimate.
"""

import asyncio
import json
import stat
import textwrap

from botcircuits.runtime.base import RuntimeConfig
from botcircuits.runtime.providers.claude_code import ClaudeCodeRuntime
from botcircuits.agent.workflow.engine.runner import run_workflow_engine
from botcircuits.usage import (
    ActionUsage,
    RunUsage,
    count_json_tokens,
    count_tokens,
    token_footprint,
    usage_from_stdout,
)


# --- usage_from_stdout -----------------------------------------------------

def test_usage_from_claude_code_envelope():
    raw = json.dumps({
        "result": '{"slots": {"approved": true}}',
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 80,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 50,
        },
    })
    u = usage_from_stdout(raw, step="decide", runtime="claude-code")
    assert u is not None
    assert u.input_tokens == 1200
    assert u.output_tokens == 80
    assert u.cache_read_tokens == 1000
    assert u.cache_write_tokens == 50
    assert u.calls == 1
    assert u.total_tokens == 1280
    assert u.step == "decide" and u.runtime == "claude-code"


def test_usage_from_openai_style_aliases():
    # codex/openai vendors use prompt_tokens / completion_tokens / cached_tokens.
    raw = json.dumps({"usage": {
        "prompt_tokens": 500, "completion_tokens": 40, "cached_tokens": 100,
    }})
    u = usage_from_stdout(raw, runtime="codex")
    assert u.input_tokens == 500
    assert u.output_tokens == 40
    assert u.cache_read_tokens == 100


def test_usage_from_stdout_none_when_absent():
    assert usage_from_stdout('{"slots": {}}') is None
    assert usage_from_stdout("") is None
    assert usage_from_stdout("not json at all") is None


def test_usage_from_stdout_span_fallback():
    # Prose around the JSON still yields the usage via the {...} span match.
    raw = "done!\n{\"usage\": {\"input_tokens\": 10, \"output_tokens\": 2}}\nbye"
    u = usage_from_stdout(raw)
    assert u.input_tokens == 10 and u.output_tokens == 2


# --- RunUsage --------------------------------------------------------------

def test_run_usage_accumulates_and_skips_empty():
    r = RunUsage()
    r.add(ActionUsage(step="a", input_tokens=100, output_tokens=10, calls=1))
    r.add(ActionUsage(step="b", input_tokens=200, output_tokens=20,
                      cache_read_tokens=50, calls=1))
    r.add(None)  # deterministic step, no LLM
    r.add(ActionUsage(step="c"))  # all-zero, ignored
    assert len(r.steps) == 2
    assert r.input_tokens == 300
    assert r.output_tokens == 30
    assert r.cache_read_tokens == 50
    assert r.calls == 2
    assert r.total_tokens == 330
    d = r.to_dict()
    assert d["total_tokens"] == 330
    assert [s["step"] for s in d["steps"]] == ["a", "b"]


# --- token_counter ---------------------------------------------------------

def test_count_tokens_heuristic_for_unknown_provider():
    # Unknown runtimes (hermes/openclaw) use the offline ~chars/4 heuristic.
    n = count_tokens("hello world this is a test", "openclaw")
    assert n == 7  # 26 chars -> ceil(26/4)
    assert count_tokens("", "openclaw") == 0


def test_count_json_tokens_nonempty():
    assert count_json_tokens({"a": 1, "steps": {"s": {"type": "start"}}}) > 0


def test_token_footprint_families_and_totals():
    fp = token_footprint(
        raw={"name": "wf", "flow": {"steps": {}}},
        built={"name": "wf", "flow": {"steps": {}, "segments": []}},
        provider="claude-code",
    )
    assert fp["provider"] == "anthropic"
    assert fp["raw_tokens"] > 0
    assert fp["built_tokens"] > 0
    assert fp["total_tokens"] == fp["raw_tokens"] + fp["built_tokens"]

    assert token_footprint(raw={"a": 1}, provider="codex")["provider"] == "openai"
    assert token_footprint(raw={"a": 1}, provider="hermes")["provider"] == "hermes"
    # built omitted -> 0.
    fp2 = token_footprint(raw={"a": 1})
    assert fp2["built_tokens"] == 0


# --- end-to-end: engine folds per-segment usage into the run ---------------

def _fake_cli(tmp_path, json_text: str):
    script = tmp_path / "fakeclaude"
    script.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import sys
        sys.stdout.write({json_text!r})
    """))
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_engine_collects_per_step_usage_via_cli(tmp_path):
    # The fake CLI reports a usage block alongside its slots; the engine must
    # attach it per segment and total it on the EngineResult.
    payload = json.dumps({
        "slots": {"approved": True},
        "usage": {"input_tokens": 300, "output_tokens": 25},
    })
    rt = ClaudeCodeRuntime(RuntimeConfig(
        name="claude-code",
        command=[str(_fake_cli(tmp_path, payload)), "{prompt}"],
        timeout=30.0,
    ))

    flow = {
        "start": "decide",
        "steps": {
            "decide": {
                "id": "decide",
                "type": "agentAction",
                "settings": {"action": "Decide"},
                "next": "done",
            },
            "done": {
                "id": "done",
                "type": "agentAction",
                "settings": {"action": "Finish"},
            },
        },
    }

    result = asyncio.run(run_workflow_engine(
        flow,
        workflow_name="wf",
        run_segment=lambda **kw: rt.run_segment(**kw),
    ))
    assert result.usage is not None
    # Two action segments, each billing 325 total tokens.
    assert result.usage.calls == 2
    assert result.usage.total_tokens == 2 * 325
    assert {s.step for s in result.usage.steps} == {"decide", "done"}
