"""Tests for the shared option-question plumbing (`agent.option_select`):
reply normalization, the pending-options store, the LLM classifier, and
the `human_feedback` tool's selector pass-through."""

from __future__ import annotations

import asyncio

import pytest

from botcircuits.agent.option_select import (
    classify_option_reply,
    clear_options,
    is_other_reply,
    map_option_reply,
    offer_options,
    take_options,
)


@pytest.fixture(autouse=True)
def _clean_store():
    clear_options()
    yield
    clear_options()


# -- map_option_reply ---------------------------------------------------------


def test_digit_maps_to_option():
    assert map_option_reply("2", ["yes", "no"]) == "no"


def test_out_of_range_digit_passes_through():
    assert map_option_reply("7", ["yes", "no"]) == "7"


def test_label_match_is_case_insensitive_and_canonical():
    assert map_option_reply("  YES ", ["yes", "no"]) == "yes"


def test_free_form_passes_through():
    assert map_option_reply("make it 5 pages", ["yes", "no"]) == "make it 5 pages"


def test_no_options_is_identity():
    assert map_option_reply("2", None) == "2"
    assert map_option_reply("2", []) == "2"


# -- the synthetic "Other" entry ---------------------------------------------


def test_other_is_the_number_after_the_real_options():
    assert is_other_reply("3", ["yes", "no"])          # 2 options -> #3
    assert not is_other_reply("2", ["yes", "no"])      # a real option
    assert not is_other_reply("4", ["yes", "no"])      # out of range


def test_other_matches_the_word():
    assert is_other_reply("other", ["yes", "no"])
    assert is_other_reply(" Other ", ["yes", "no"])
    assert not is_other_reply("something other than that", ["yes", "no"])


def test_other_without_options_is_never_matched():
    assert not is_other_reply("other", None)
    assert not is_other_reply("1", [])


def test_tui_other_pick_falls_back_to_raw_typing():
    """Picking "Other" keeps the pause open in typing mode; the next line is
    the answer VERBATIM — a typed "2" is the answer "2", not option #2."""
    from botcircuits.cli.tui import TUISession

    async def scenario():
        s = TUISession(interactive=False)

        async def user():
            await asyncio.sleep(0)
            assert s.is_paused()
            assert await s.dispatch_reply("4")   # "Other" = 3 options + 1
            assert s.is_paused()                 # still open, now typing
            assert not s._selector_active()      # arrows/mapping disabled
            await s.dispatch_reply("2")

        t = asyncio.ensure_future(user())
        reply = await s.pause("q", ["yes", "no", "change topic"])
        await t
        return reply

    assert asyncio.run(scenario()) == "2"


def test_tui_selection_wraps_over_other_row():
    from botcircuits.cli.tui import TUISession

    async def scenario():
        s = TUISession(interactive=False)

        async def user():
            await asyncio.sleep(0)
            assert s._selected_option() == "yes"
            s._move_selection(+1)
            assert s._selected_option() == "no"
            s._move_selection(+1)
            assert s._selected_option() is None   # the "Other" row
            s._move_selection(+1)
            assert s._selected_option() == "yes"  # wrapped past Other
            await s.dispatch_reply("1")

        t = asyncio.ensure_future(user())
        reply = await s.pause("q", ["yes", "no"])
        await t
        return reply

    assert asyncio.run(scenario()) == "yes"


# -- pending-options store ----------------------------------------------------


def test_offer_and_take_exact_match():
    offer_options("Reuse them?", ["yes", "no"], default_index=1)
    got = take_options("Reuse them?")
    assert got is not None
    assert got.options == ["yes", "no"]
    assert got.default_index == 1
    assert take_options("Reuse them?") is None  # consumed


def test_take_matches_when_model_trims_or_wraps_the_question():
    offer_options("Line one\nReuse them? (yes / no)", ["yes", "no"])
    # The model relayed only part of the question text.
    assert take_options("Reuse them? (yes / no)") is not None

    offer_options("Reuse them?", ["yes", "no"])
    # The model wrapped the question with extra words.
    assert take_options("The workflow asks: Reuse them?") is not None


def test_take_unrelated_question_returns_none():
    offer_options("Reuse them?", ["yes", "no"])
    assert take_options("What topic should I research?") is None
    assert take_options("Reuse them?") is not None  # still parked


def test_reoffering_same_question_replaces_entry():
    offer_options("Reuse them?", ["yes", "no"])
    offer_options("Reuse them?", ["yes", "no", "change topic"])
    got = take_options("Reuse them?")
    assert got.options == ["yes", "no", "change topic"]
    assert take_options("Reuse them?") is None


# -- classify_option_reply ----------------------------------------------------


class _FakeProvider:
    def __init__(self, text):
        self._text = text
        self.calls = []

    async def complete(self, **kw):
        self.calls.append(kw)

        class R:
            text = self._text

        return R()


def test_classifier_returns_canonical_option():
    p = _FakeProvider('{"choice": "YES"}')
    got = asyncio.run(classify_option_reply(
        p, question="Reuse them?", options=["yes", "no"],
        reply="yes do same,"))
    assert got == "yes"  # canonical casing from the option list


def test_classifier_null_and_garbage_mean_free_form():
    for text in ('{"choice": null}', "not json at all",
                 '{"choice": "maybe"}'):
        got = asyncio.run(classify_option_reply(
            _FakeProvider(text), question="q", options=["yes", "no"],
            reply="Robotics, 2 pages"))
        assert got is None


def test_classifier_provider_error_means_free_form():
    class Boom:
        async def complete(self, **kw):
            raise RuntimeError("provider down")

    got = asyncio.run(classify_option_reply(
        Boom(), question="q", options=["yes", "no"], reply="hmm"))
    assert got is None


def test_classifier_skips_empty_inputs_without_calling_provider():
    p = _FakeProvider('{"choice": "yes"}')
    assert asyncio.run(classify_option_reply(
        p, question="q", options=[], reply="yes")) is None
    assert asyncio.run(classify_option_reply(
        p, question="q", options=["yes"], reply="  ")) is None
    assert asyncio.run(classify_option_reply(
        None, question="q", options=["yes"], reply="yes")) is None
    assert p.calls == []


# -- human_feedback selector pass-through ------------------------------------


class _FakeWorkflowTask:
    def __init__(self, answer="picked"):
        self._answer = answer
        self.pauses = []

    async def pause(self, question, options=None, default_index=0):
        self.pauses.append((question, options, default_index))
        return self._answer


def _feedback(args, context):
    from botcircuits.agent.tools.builtins.human_feedback import (
        human_feedback_tool,
    )
    return asyncio.run(human_feedback_tool().handler(args, context))


def test_human_feedback_passes_model_options_to_pause():
    wt = _FakeWorkflowTask()
    out = _feedback({"question": "Pick one", "options": ["a", "b"]},
                    {"_workflow_bg": wt})
    assert out == {"answer": "picked", "question": "Pick one"}
    assert wt.pauses == [("Pick one", ["a", "b"], 0)]


def test_human_feedback_recovers_parked_engine_options():
    offer_options("Reuse them?", ["yes", "no"], default_index=1)
    wt = _FakeWorkflowTask()
    _feedback({"question": "Reuse them?"}, {"_workflow_bg": wt})
    assert wt.pauses == [("Reuse them?", ["yes", "no"], 1)]


def test_human_feedback_foreground_echoes_options():
    offer_options("Pick one", ["a", "b"])
    out = _feedback({"question": "Pick one"}, None)
    assert out == {"paused": True, "question": "Pick one",
                   "options": ["a", "b"]}
