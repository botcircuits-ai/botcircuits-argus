"""Parsing CLI-agent stdout into SegmentResult / normalized-slot dicts."""

import json

from botcircuits.runtime.result import (
    extract_json_object,
    normalized_slots_from_stdout,
    segment_result_from_stdout,
)


def test_bare_contract_object():
    r = segment_result_from_stdout(
        json.dumps({"slots": {"x": 5, "y": ""}, "text": "done"})
    )
    # Empty-string slot dropped; non-empty kept.
    assert r.captured_slots == {"x": 5}
    assert r.text == "done"
    assert r.paused is False


def test_claude_code_envelope_with_fenced_inner_json():
    inner = "```json\n" + json.dumps({"slots": {"approved": True}}) + "\n```"
    envelope = json.dumps({"type": "result", "result": inner})
    r = segment_result_from_stdout(envelope)
    assert r.captured_slots == {"approved": True}


def test_envelope_with_parsed_inner_object():
    # Some CLIs put an already-parsed object under `result`.
    envelope = json.dumps({"result": {"slots": {"n": 3}}})
    r = segment_result_from_stdout(envelope)
    assert r.captured_slots == {"n": 3}


def test_paused_question():
    r = segment_result_from_stdout(
        json.dumps({"paused": True, "question": "What is your income?"})
    )
    assert r.paused is True
    assert r.question == "What is your income?"


def test_items_native_array():
    r = segment_result_from_stdout(
        json.dumps({"items": [{"sku": "A", "qty": 2}, {"sku": "B", "qty": 1}]})
    )
    assert r.captured_items == [{"sku": "A", "qty": 2}, {"sku": "B", "qty": 1}]


def test_items_serialized_as_string():
    # Providers sometimes stringify the array.
    r = segment_result_from_stdout(
        json.dumps({"items": json.dumps([{"sku": "A", "qty": 2}])})
    )
    assert r.captured_items == [{"sku": "A", "qty": 2}]


def test_prose_around_json():
    out = "Here is the result:\n" + json.dumps({"slots": {"k": "v"}}) + "\nThanks!"
    r = segment_result_from_stdout(out)
    assert r.captured_slots == {"k": "v"}


def test_garbage_yields_empty_result_with_text():
    r = segment_result_from_stdout("I could not produce JSON, sorry.")
    assert r.captured_slots == {}
    assert "could not" in r.text


def test_normalized_wrapped_and_bare():
    assert normalized_slots_from_stdout(json.dumps({"normalized": {"a": 1}})) == {"a": 1}
    # Bare object: contract/envelope keys stripped, slot values kept.
    assert normalized_slots_from_stdout(
        json.dumps({"a": 1, "paused": False, "question": ""})
    ) == {"a": 1}


def test_extract_json_object_none_on_empty():
    assert extract_json_object("") is None
    assert extract_json_object("no json here at all") is None


def test_needs_tool_parsed_as_list():
    r = segment_result_from_stdout(json.dumps(
        {"paused": True, "question": "grant web?", "needs_tool": ["WebSearch"]}
    ))
    assert r.paused is True
    assert r.needs_tool == ["WebSearch"]


def test_needs_tool_accepts_single_string():
    r = segment_result_from_stdout(json.dumps(
        {"paused": True, "needs_tool": "WebFetch"}
    ))
    assert r.needs_tool == ["WebFetch"]


def test_needs_tool_absent_is_empty():
    r = segment_result_from_stdout(json.dumps({"slots": {"x": 1}}))
    assert r.needs_tool == []


def test_needs_tool_not_treated_as_slot_in_normalized():
    # `needs_tool` is a contract key, never a normalized slot value.
    assert normalized_slots_from_stdout(
        json.dumps({"a": 1, "needs_tool": ["WebSearch"]})
    ) == {"a": 1}
