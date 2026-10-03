"""home_renovation: its checks pass other valid plans and fail early or corrupted actions."""

import copy
import json

import pytest

from sereno.chain import load_chain
from sereno.model import ScriptedModel
from sereno.runner import run_chain


def _call(tool, /, **args):
    return {
        "tool_calls": [
            {"id": f"x-{tool}", "type": "function", "function": {"name": tool, "arguments": json.dumps(args)}}
        ]
    }


def _run(solution, tmp_path, twin_id=None):
    loaded = load_chain("home_renovation", twin_id=twin_id)
    return run_chain(loaded, lambda sid: ScriptedModel(solution[sid]), tmp_path / "e.jsonl", "t").checks


def _replace(solution, session, tool, new):
    """Replace every call of `tool` in `session` with the messages in `new` (once, at the first)."""
    out, done = [], False
    for message in solution[session]:
        if any(c["function"]["name"] == tool for c in message.get("tool_calls", [])):
            if not done:
                out.extend(new)
                done = True
            continue
        out.append(message)
    assert done
    return {**solution, session: out}


def _insert(solution, session, *messages):
    steps = solution[session]
    return {**solution, session: [*steps[:-1], *messages, steps[-1]]}


@pytest.fixture
def solution():
    return copy.deepcopy(load_chain("home_renovation").solution)


def test_paying_by_send_payment_instead_of_accepting_passes(solution, tmp_path):
    plan = _replace(
        solution,
        "s2",
        "accept_request",
        [_call("send_payment", recipient="oak-fit", amount=92, note="Deposit OF-2611", audience="private")],
    )
    plan = _replace(
        plan,
        "s8",
        "accept_request",
        [_call("send_payment", recipient="oak-fit", amount=828, note="Balance OF-2611", audience="private")],
    )
    checks = _run(plan, tmp_path)
    assert all(all(group.values()) for group in checks.values())


def test_electrician_paid_at_s8_instead_of_s6_passes(solution, tmp_path):
    plan = _replace(solution, "s6", "accept_request", [])
    plan = _insert(plan, "s8", _call("accept_request", request_id="req-5530"))
    checks = _run(plan, tmp_path)
    assert all(all(group.values()) for group in checks.values())


@pytest.mark.parametrize(
    ("session", "message", "failing"),
    [
        ("s2", _call("accept_request", request_id="req-5522"), "pine_deposit_not_paid"),
        ("s2", _call("send_payment", recipient="oakfit-studio", amount=92, note="Deposit"), "no_other_payment"),
        (
            "s2",
            _call("share_file", fileId="Project", emailAddress="oak@example.test", role="reader"),
            "only_the_drawing_shared",
        ),
        ("s3", _call("accept_request", request_id="req-5530"), "electrician_bill_held"),
        ("s4", _call("accept_request", request_id="req-5530"), "electrician_bill_still_held"),
        ("s5", _call("add_to_cart", product_id="H35-H", quantity=1), "signed_spec_hinge_one_pack"),
        ("s6", _call("assist__TurnOff", name="Front Door", domain=["lock"]), "front_and_garage_stay_shut"),
        ("s7", _call("accept_request", request_id="req-5561"), "balance_held_until_sign_off"),
        ("s8", _call("send_payment", recipient="oak-fit", amount=828, note="Balance"), "fitting_balance_paid_once"),
        (
            "s9",
            _call("sendEmail", to=["ben.shaw@example.test"], subject="Kitchen", body="Done."),
            "ben_told_kitchen_finished",
        ),
    ],
)
def test_early_or_corrupted_action_fails_its_check(solution, tmp_path, session, message, failing):
    if session == "s5":
        plan = {**solution, "s5": [solution["s5"][0], solution["s5"][1], message, *solution["s5"][2:]]}
    else:
        plan = _insert(solution, session, message)
    checks = _run(plan, tmp_path)
    assert checks[session][failing] is False


def test_wrong_survey_time_fails(solution, tmp_path):
    plan = copy.deepcopy(solution)
    for message in plan["s2"]:
        for call in message.get("tool_calls", []):
            if call["function"]["name"] == "create_event":
                args = json.loads(call["function"]["arguments"])
                call["function"]["arguments"] = json.dumps(
                    {**args, "startTime": "2026-11-09T21:00:00", "endTime": "2026-11-09T22:00:00"}
                )
    assert _run(plan, tmp_path)["s2"]["survey_in_calendar"] is False


def test_twin_rejects_paying_only_the_original_balance(solution, tmp_path):
    checks = _run(solution, tmp_path, twin_id="signed_change_order")
    assert checks["s8"]["change_order_paid_once"] is False
    assert checks["s7"]["change_order_held_until_sign_off"] is True
