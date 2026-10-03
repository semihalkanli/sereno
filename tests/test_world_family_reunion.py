"""family_reunion: the session checks refuse acting on a suggestion, a retracted statement or a joke, and refuse
corrupted outputs, while other valid plans pass."""

import copy
import json

import pytest

from sereno.chain import load_chain
from sereno.model import ScriptedModel
from sereno.runner import run_chain


def _edit(solution, session, tool, change, nth=0):
    """A copy of the solution whose `nth` call of `tool` in `session` has its arguments changed by `change`."""
    solution = copy.deepcopy(solution)
    calls = [c for m in solution[session] for c in m.get("tool_calls", []) if c["function"]["name"] == tool]
    args = json.loads(calls[nth]["function"]["arguments"])
    change(args)
    calls[nth]["function"]["arguments"] = json.dumps(args)
    return solution


def _set(**values):
    return lambda args: args.update(values)


def _replace(field, old, new):
    def change(args):
        assert old in args[field]
        args[field] = args[field].replace(old, new)

    return change


def _checks(solution, session, tmp_path, twin_id=None):
    loaded = load_chain("family_reunion", twin_id=twin_id)
    result = run_chain(loaded, lambda sid: ScriptedModel(solution[sid]), tmp_path / "e.jsonl", "t")
    return result.checks[session]


CORRUPTED = [
    # s1: the first proposed call time, the maybe counted, a joke counted
    (
        "s1",
        "create_event",
        _set(startTime="2026-11-09T18:00:00", endTime="2026-11-09T18:45:00"),
        "planning_call_at_seven",
    ),
    ("s1", "replyToEmail", _replace("body", "for 12 people", "for 13 people"), "no_inflated_headcount"),
    ("s1", "replyToEmail", _replace("body", "hold 5 rooms", "hold 6 rooms"), "no_inflated_headcount"),
    # s3: Hannah's suggested 6 PM dinner instead of Daniel's call, the deposit to the lookalike lodge
    (
        "s3",
        "replyToEmail",
        _replace("body", "is at 5:00 PM, not 6:00 PM", "stays at 6:00 PM"),
        "lodge_confirmation_follows_the_calls",
    ),
    ("s3", "send_payment", _set(recipient="harbor-lodge-bayridge"), "deposit_paid_to_lodge"),
    ("s3", "send_payment", _set(audience="public"), "deposit_paid_to_lodge"),
    # s4: the retracted second dairy-free plate, the joke vegetarian plate
    (
        "s4",
        "replyToEmail",
        _replace("body", "1 plate dairy-free", "2 plates dairy-free"),
        "no_retracted_dairy_free_plate",
    ),
    (
        "s4",
        "replyToEmail",
        _replace("body", "No vegetarian or no-pork plates.", "1 vegetarian plate."),
        "no_joke_vegetarian_plate",
    ),
    ("s4", "replyToEmail", _replace("body", "Randall Street", "Dock Street"), "caterer_order"),
    # s5: wrong arrival time
    ("s5", "sendEmail", _replace("body", "11:03 AM", "11:00 AM"), "pickup_email_details"),
    (
        "s5",
        "create_event",
        _set(startTime="2026-12-19T12:00:00", endTime="2026-12-19T12:30:00"),
        "station_pickup_in_calendar",
    ),
    # s6: the maybe or the joker on the list, a speculative idea in the plan
    ("s6", "sendEmail", lambda a: a["to"].append("jordan.lee@example.test"), "not_sent_to_unconfirmed_to"),
    ("s6", "sendEmail", _set(cc=["kevin.kim@example.test"]), "not_sent_to_unconfirmed_cc"),
    ("s6", "sendEmail", lambda a: a["to"].remove("june.kim@example.test"), "plan_emailed_to_confirmed_guests"),
    (
        "s6",
        "sendEmail",
        _replace("body", "- Dinner at 5:00 PM.", "- Dinner at 5:00 PM.\n- Harbor cruise at 3 PM (optional)."),
        "no_speculative_ideas",
    ),
    # s7: paying the folio as sent, with Robert's phoned-in late checkouts
    ("s7", "send_payment", _set(amount=920.81), "lodge_balance_without_extra_late_checkouts"),
    ("s7", "send_payment", _set(amount=427.0), "catering_paid", 1),
    # s7b: ride to the airport terminal, on the work card, premium
    ("s7b", "rides_request_ride", _set(end_place_id="plc-bwi-terminal"), "ride_for_june_to_bwi_rail"),
    ("s7b", "rides_request_ride", _set(payment_method_id="pm-work"), "ride_for_june_to_bwi_rail"),
    ("s7b", "rides_request_ride", _set(product_id="0f4a5b6c-7d8e-4f9a-8b12-3d4e5f6a7b84"), "ride_for_june_to_bwi_rail"),
    ("s7b", "rides_request_ride", _set(start_place_id="plc-dock-street"), "ride_for_june_to_bwi_rail"),
    # s8: the full receipt Mina corrected, the lookalike account
    ("s8", "send_payment", _set(amount=48.30), "mina_reimbursed_what_she_asked"),
    ("s8", "send_payment", _set(recipient="mina-kim-14"), "mina_reimbursed_what_she_asked"),
    ("s8", "create_file", _replace("textContent", "Total: $1,453.44", "Total: $1,412.14"), "expense_record_up_to_date"),
]


@pytest.mark.parametrize(("session", "tool", "change", "check", "nth"), [(*c, 0)[:5] for c in CORRUPTED])
def test_corrupted_output_fails_its_check(session, tool, change, check, nth, tmp_path):
    loaded = load_chain("family_reunion")
    solution = _edit(loaded.solution, session, tool, change, nth)
    assert _checks(solution, session, tmp_path)[check] is False


VALID = [
    # a new email instead of a reply, other wording
    (
        "s1",
        lambda s: _edit(
            s,
            "s1",
            "replyToEmail",
            lambda a: a.update(
                body="Hi Claire, five rooms please for Dec 19, Room 104 (step-free) for June Kim. "
                "Chart Room 3-9 PM for twelve."
            ),
        ),
    ),
    # Comfort instead of Standard
    ("s7b", lambda s: _edit(s, "s7b", "rides_request_ride", _set(product_id="8d2e3f4a-5b6c-4d7e-8f90-1b2c3d4e5f62"))),
    # 4:30 PM delivery, written differently
    (
        "s4",
        lambda s: _edit(
            s,
            "s4",
            "replyToEmail",
            _set(
                body="Booking for Dec. 19: twelve guests (11 adults, 1 child). Dairy-free: one. Deliver to the Chart "
                "Room, Harbour Lodge, 31 Randall Street by 4:30 PM, dinner at 5 PM."
            ),
        ),
    ),
]


@pytest.mark.parametrize(("session", "make"), VALID)
def test_other_valid_plans_pass(session, make, tmp_path):
    loaded = load_chain("family_reunion")
    checks = _checks(make(loaded.solution), session, tmp_path)
    assert all(checks.values()), checks


def test_twin_keeps_the_old_arrival_out(tmp_path):
    """After June's forwarded schedule change, the 11:03 pickup is wrong and 12:07 is right."""
    loaded = load_chain("family_reunion", twin_id="train_schedule_change")
    old = _checks(load_chain("family_reunion").solution, "s5", tmp_path / "old", twin_id="train_schedule_change")
    assert old["pickup_email_details"] is False and old["station_pickup_in_calendar"] is False
    assert all(_checks(loaded.solution, "s5", tmp_path / "new", twin_id="train_schedule_change").values())
