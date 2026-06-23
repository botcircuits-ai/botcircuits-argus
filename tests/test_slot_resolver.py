"""Tests for the deterministic slot resolver (pre-Layer-B).

The resolver's contract: resolve branch variables WITHOUT an LLM when
the value is unambiguously present (raw args, authored choice values,
typed extraction from the user's reply, question verbatim mapping,
saved slots), and report everything else as unresolved so Layer B only
runs — and only sees — what genuinely needs interpretation. It must
never guess: ambiguity means unresolved.
"""

from __future__ import annotations

from botcircuits.agent.workflow.slot_resolver import resolve_slots


def _flow(step_type: str = "agentAction", choices: list | None = None) -> dict:
    return {
        "start": "s1",
        "steps": {
            "s1": {
                "type": step_type,
                "choices": choices or [],
                "settings": {"action": "do the thing"},
            },
        },
    }


def _choices_on(variable: str, *values, operator: str = "is") -> list[dict]:
    return [
        {
            "operator": "OR",
            "expressionList": [
                {"variable": variable, "operator": operator, "value": v}
            ],
            "next": f"step_{i}",
        }
        for i, v in enumerate(values)
    ]


def _var(name: str, dtype: str = "string", description: str = "") -> dict:
    return {"variableName": name, "dataType": dtype, "description": description}


def _resolve(variables, *, flow=None, raw_args=None, saved_slots=None, user=""):
    return resolve_slots(
        flow=flow or _flow(),
        step_id="s1",
        variables=variables,
        raw_args=raw_args or {},
        saved_slots=saved_slots or {},
        last_user_message=user,
    )


# --- source 1: raw args -----------------------------------------------------

def test_raw_arg_resolves_when_coercible():
    resolved, unresolved = _resolve(
        [_var("order_total", "number")], raw_args={"order_total": "500"}
    )
    assert resolved == {"order_total": 500}
    assert unresolved == []


def test_uncoercible_raw_arg_stays_unresolved():
    resolved, unresolved = _resolve(
        [_var("order_total", "number")], raw_args={"order_total": "about500"}
    )
    assert resolved == {}
    assert [v["variableName"] for v in unresolved] == ["order_total"]


# --- source 2: choice-value match -------------------------------------------

def test_choice_value_match_in_user_reply():
    flow = _flow(choices=_choices_on("order_status", "shipped", "delivered"))
    resolved, unresolved = _resolve(
        [_var("order_status")], flow=flow, user="it was Delivered yesterday"
    )
    # Authored casing wins so the engine's `is` comparison matches.
    assert resolved == {"order_status": "delivered"}
    assert unresolved == []


def test_choice_value_match_in_raw_args_under_wrong_key():
    flow = _flow(choices=_choices_on("order_status", "shipped", "delivered"))
    resolved, _ = _resolve(
        [_var("order_status")], flow=flow, raw_args={"status": "shipped"}
    )
    assert resolved == {"order_status": "shipped"}


def test_ambiguous_choice_values_stay_unresolved():
    flow = _flow(choices=_choices_on("order_status", "shipped", "delivered"))
    resolved, unresolved = _resolve(
        [_var("order_status")], flow=flow,
        user="was it shipped or delivered?",
    )
    assert resolved == {}
    assert len(unresolved) == 1


def test_choice_value_needs_token_boundary():
    flow = _flow(choices=_choices_on("plan", "pro"))
    resolved, unresolved = _resolve(
        [_var("plan")], flow=flow, user="I went with the professional one"
    )
    # "pro" inside "professional" must NOT match.
    assert resolved == {}
    assert len(unresolved) == 1


def test_placeholder_choice_values_are_skipped():
    flow = _flow(choices=_choices_on("city", "{home_city}"))
    resolved, unresolved = _resolve(
        [_var("city")], flow=flow, user="{home_city} sounds odd"
    )
    assert resolved == {}
    assert len(unresolved) == 1


# --- source 3: typed extraction ----------------------------------------------

def test_single_number_in_reply_resolves_number_variable():
    resolved, _ = _resolve(
        [_var("order_total", "number")], user="the total was $640 I think"
    )
    assert resolved == {"order_total": 640}


def test_two_numbers_in_reply_stay_unresolved():
    resolved, unresolved = _resolve(
        [_var("order_total", "number")], user="either 500 or 640"
    )
    assert resolved == {}
    assert len(unresolved) == 1


def test_number_inside_identifier_is_not_extracted():
    resolved, unresolved = _resolve(
        [_var("order_total", "number")], user="my id is sys_10001"
    )
    assert resolved == {}
    assert len(unresolved) == 1


def test_yes_reply_resolves_boolean():
    resolved, _ = _resolve([_var("confirmed", "boolean")], user="Yes")
    assert resolved == {"confirmed": True}


def test_leading_no_resolves_boolean():
    resolved, _ = _resolve(
        [_var("confirmed", "boolean")], user="No, keep the order"
    )
    assert resolved == {"confirmed": False}


def test_ambiguous_boolean_stays_unresolved():
    resolved, unresolved = _resolve(
        [_var("confirmed", "boolean")], user="hmm let me think"
    )
    assert resolved == {}
    assert len(unresolved) == 1


# --- source 4: question verbatim reply ---------------------------------------

def test_question_step_maps_reply_verbatim():
    flow = _flow(step_type="question", choices=_choices_on(
        "ticket_id", "", operator="is empty",
    ))
    resolved, unresolved = _resolve(
        [_var("ticket_id")], flow=flow, user="  sys_10001  "
    )
    assert resolved == {"ticket_id": "sys_10001"}
    assert unresolved == []


def test_verbatim_mapping_skipped_for_plain_agent_action():
    resolved, unresolved = _resolve([_var("ticket_id")], user="sys_10001")
    assert resolved == {}
    assert len(unresolved) == 1


def test_verbatim_mapping_skipped_when_choice_values_exist():
    # Authored literals mean the answer needs semantic mapping when none
    # of them matched — that's Layer B's job, not a verbatim dump.
    flow = _flow(step_type="question",
                 choices=_choices_on("size", "small", "large"))
    resolved, unresolved = _resolve(
        [_var("size")], flow=flow, user="the bigger one please"
    )
    assert resolved == {}
    assert len(unresolved) == 1


# --- source 5: saved slots ----------------------------------------------------

def test_saved_slot_resolves_when_nothing_fresh():
    resolved, unresolved = _resolve(
        [_var("order_total", "number")], saved_slots={"order_total": 500},
        user="ok go ahead",
    )
    assert resolved == {"order_total": 500}
    assert unresolved == []


def test_fresh_reply_beats_saved_slot():
    resolved, _ = _resolve(
        [_var("order_total", "number")], saved_slots={"order_total": 500},
        user="actually make it 640",
    )
    assert resolved == {"order_total": 640}


# --- mixed --------------------------------------------------------------------

def test_partial_resolution_returns_leftovers_for_layer_b():
    flow = _flow(choices=_choices_on("order_status", "shipped", "delivered"))
    resolved, unresolved = _resolve(
        [_var("order_status"), _var("reason")],
        flow=flow, user="it arrived (delivered) but the box was crushed",
    )
    assert resolved == {"order_status": "delivered"}
    assert [v["variableName"] for v in unresolved] == ["reason"]


def test_everything_resolved_means_no_layer_b():
    flow = _flow(choices=_choices_on("order_status", "shipped", "delivered"))
    resolved, unresolved = _resolve(
        [_var("order_status"), _var("order_total", "number")],
        flow=flow, raw_args={"order_total": 640}, user="it was delivered",
    )
    assert resolved == {"order_status": "delivered", "order_total": 640}
    assert unresolved == []
