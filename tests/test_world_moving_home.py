"""moving_home measures superseded facts: a solution that acts on a stale value fails the check for it."""

import copy
import json

import pytest

from sereno.chain import load_chain
from sereno.model import ScriptedModel
from sereno.runner import run_chain


def _calls(solution, session, tool):
    return [c for m in solution[session] for c in m.get("tool_calls", []) if c["function"]["name"] == tool]


def _patch(solution, session, tool, index=0, **args):
    call = _calls(solution, session, tool)[index]
    call["function"]["arguments"] = json.dumps({**json.loads(call["function"]["arguments"]), **args})


def _body(solution, session, tool, old, new, index=0):
    call = _calls(solution, session, tool)[index]
    args = json.loads(call["function"]["arguments"])
    assert old in args["body"]
    _patch(solution, session, tool, index, body=args["body"].replace(old, new))


def _stale_code(s):
    _body(s, "s6", "replyToEmail", "code 7093", "code 4471")


def _old_phone(s):
    _body(s, "s6", "replyToEmail", "(773) 555-0193", "(312) 555-0148")


def _old_elevator_slot(s):
    _body(s, "s6", "replyToEmail", "6:00 pm to 8:00 pm", "5:00 pm to 7:00 pm")


def _duplicate_mover_email(s):
    reply = _calls(s, "s6", "replyToEmail")[0]
    s["s6"].insert(-1, {"tool_calls": [{**reply, "id": "dup"}]})


def _kit_to_new_flat(s):
    _patch(s, "s5", "place_order", shipping_address="42 Willow Street, Apt 4B, Chicago, IL")


def _closing_payee(s):
    _patch(s, "s7", "send_payment", 1, recipient="tidy-team")


def _earlier_heat(s):
    _patch(s, "s7", "assist__ClimateSetTemperature", temperature=55)


def _both_thermostats(s):
    call = _calls(s, "s7", "assist__ClimateSetTemperature")[0]
    call["function"]["arguments"] = json.dumps({"temperature": 60})


def _former_landlord(s):
    _patch(s, "s4", "sendEmail", to=["harold.kessler@example.test"])


def _stale_rota(s):
    _patch(s, "s4", "create_event", startTime="2026-11-25T18:30:00", endTime="2026-11-25T19:15:00")


def _desk_kept_on_shift_night(s):
    s["s4"] = [
        {"tool_calls": [c for c in m["tool_calls"] if "evt-12" not in c["function"]["arguments"]]}
        if m.get("tool_calls")
        else m
        for m in s["s4"]
    ]


def _withdrawn_unit(s):
    _body(s, "s8", "replyToEmail", "Apt 4B", "Apt 4D")


@pytest.mark.parametrize(
    ("corrupt", "session", "check"),
    [
        (_stale_code, "s6", "mover_not_given_stale_details"),
        (_stale_code, "s6", "mover_has_current_access_details"),
        (_old_phone, "s6", "mover_not_given_stale_details"),
        (_old_elevator_slot, "s6", "mover_has_current_access_details"),
        (_duplicate_mover_email, "s6", "mover_has_current_access_details"),
        (_kit_to_new_flat, "s5", "kit_sent_to_cedar_lane"),
        (_closing_payee, "s7", "cleaner_paid_on_current_profile"),
        (_earlier_heat, "s7", "old_flat_heat_at_60"),
        (_both_thermostats, "s7", "new_flat_heat_untouched"),
        (_former_landlord, "s4", "nothing_to_former_landlord"),
        (_stale_rota, "s4", "walkthrough_booked_on_tuesday"),
        (_desk_kept_on_shift_night, "s4", "no_desk_collection_on_shift_night"),
        (_withdrawn_unit, "s8", "forwarding_address_corrected"),
    ],
)
def test_acting_on_a_superseded_value_fails(corrupt, session, check, tmp_path):
    loaded = load_chain("moving_home")
    solution = copy.deepcopy(loaded.solution)
    corrupt(solution)
    result = run_chain(loaded, lambda sid: ScriptedModel(solution[sid]), tmp_path / "e.jsonl", "t")
    assert result.checks[session][check] is False


@pytest.mark.parametrize("window", ["6-8 pm", "6 to 8 pm", "18:00-20:00"])
def test_current_elevator_slot_in_other_words_passes(window, tmp_path):
    loaded = load_chain("moving_home")
    solution = copy.deepcopy(loaded.solution)
    _body(solution, "s6", "replyToEmail", "6:00 pm to 8:00 pm", window)
    result = run_chain(loaded, lambda sid: ScriptedModel(solution[sid]), tmp_path / "e.jsonl", "t")
    assert result.checks["s6"]["mover_has_current_access_details"] is True
