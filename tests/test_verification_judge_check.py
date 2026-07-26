"""LLM-judge check execution (`verification/judge_check.py`) — strict
JSON verdict, tolerant parsing, and the `inputs` allow-list that scopes
what the judge prompt actually sees."""

from __future__ import annotations

import asyncio

from tests.fakes import ScriptedProvider, text_response

from botcircuits.agent.workflow.verification import judge_check


def _check(**overrides) -> dict:
    check = {
        "id": "tone",
        "type": "llm_judge",
        "rubric": "Must be professional.",
        "inputs": ["slots.summary"],
        "severity": "advisory",
    }
    check.update(overrides)
    return check


def test_judge_verdict_from_strict_json():
    provider = ScriptedProvider([
        text_response('{"passed": true, "detail": "reads professionally"}'),
    ])
    result = asyncio.run(judge_check.run_one(
        _check(), {"slots": {"summary": "Thank you for your order."}},
        provider=provider,
    ))
    assert result.passed is True
    assert result.detail == "reads professionally"
    assert result.error is None


def test_judge_verdict_from_fenced_markdown_json():
    """Reuses the shared `_json_repair` extractor — fenced code blocks
    around the JSON must still parse."""
    provider = ScriptedProvider([
        text_response('```json\n{"passed": false, "detail": "too casual"}\n```'),
    ])
    result = asyncio.run(judge_check.run_one(
        _check(), {"slots": {"summary": "yo, order's done lol"}},
        provider=provider,
    ))
    assert result.passed is False
    assert result.detail == "too casual"


def test_only_listed_inputs_reach_the_judge_prompt():
    """A slot NOT named in `inputs` must never leak into the judge
    prompt — `inputs` is a scope/privacy boundary, not just a hint."""
    captured: dict = {}

    class RecordingProvider(ScriptedProvider):
        async def complete(self, system, messages, tools, hosted_mcp,
                           skills, max_tokens):
            captured["prompt"] = messages[0].blocks[0]["text"]
            return await super().complete(
                system, messages, tools, hosted_mcp, skills, max_tokens,
            )

    provider = RecordingProvider([text_response('{"passed": true, "detail": "ok"}')])
    run = {"slots": {"summary": "visible value", "ssn": "secret-should-not-leak"}}
    asyncio.run(judge_check.run_one(
        _check(inputs=["slots.summary"]), run, provider=provider,
    ))
    assert "visible value" in captured["prompt"]
    assert "secret-should-not-leak" not in captured["prompt"]


def test_judge_call_failure_never_raises():
    class ExplodingProvider(ScriptedProvider):
        async def complete(self, *a, **kw):
            raise RuntimeError("provider unavailable")

    result = asyncio.run(judge_check.run_one(
        _check(), {"slots": {}}, provider=ExplodingProvider([]),
    ))
    assert result.passed is False
    assert result.error is not None
    assert "provider unavailable" in result.error


def test_judge_malformed_reply_is_an_error_not_a_crash():
    provider = ScriptedProvider([text_response("I cannot comply with this request.")])
    result = asyncio.run(judge_check.run_one(
        _check(), {"slots": {}}, provider=provider,
    ))
    assert result.passed is False
    assert result.error is not None
