"""semester_abroad: a plan that breaks one institutional rule fails a task check of its session."""

import json

import pytest

from sereno.chain import load_chain
from sereno.model import ScriptedModel
from sereno.runner import run_chain

REGISTER = '\\"course_reference_numbers\\": [\\"21004\\", \\"21002\\", \\"21009\\", \\"21006\\"]'


def _with(solution, session, old, new):
    text = json.dumps(solution[session])
    assert old in text, old
    return {**solution, session: json.loads(text.replace(old, new))}


@pytest.mark.parametrize(
    ("session", "old", "new", "failed"),
    [
        ("s4", "2026-11-19T16:00:00", "2026-11-19T11:00:00", "webinar_in_london_time"),
        (
            "s4",
            '\\"due_date\\": \\"2026-11-27\\"',
            '\\"due_date\\": \\"2026-12-02\\"',
            "sevis_fee_due_three_business_days_before",
        ),
        ("s5", REGISTER, REGISTER.replace('\\"21006\\"]', '\\"21006\\", \\"21011\\"]'), "exactly_one_online_course"),
        ("s5", REGISTER, REGISTER.replace("21009", "21012"), "hist215_morning_section"),
        ("s5", REGISTER, REGISTER.replace('\\"21009\\", ', ""), "fifteen_credits_five_courses"),
        (
            "s6",
            'insurance certificate 2026-27\\"]',
            'insurance certificate 2025-26\\"]',
            "waiver_with_current_certificate",
        ),
        ("s7", '\\"product_id\\": \\"BED-1\\"', '\\"product_id\\": \\"BED-2\\"', "twin_xl_kit_ordered"),
        ("s8", '\\"offer_id\\": \\"off-a-std\\"', '\\"offer_id\\": \\"off-a-light\\"', "flight_satisfies_every_rule"),
        ("s8", '\\"offer_id\\": \\"off-a-std\\"', '\\"offer_id\\": \\"off-b-std\\"', "flight_satisfies_every_rule"),
        ("s8", '\\"offer_id\\": \\"off-a-std\\"', '\\"offer_id\\": \\"off-e-std\\"', "flight_satisfies_every_rule"),
        ("s8", "2006-05-20", "2006-02-05", "passenger_as_on_passport"),
    ],
)
def test_one_broken_rule_fails_its_check(session, old, new, failed, tmp_path):
    loaded = load_chain("semester_abroad")
    solution = _with(loaded.solution, session, old, new)
    result = run_chain(loaded, lambda sid: ScriptedModel(solution[sid]), tmp_path / "e.jsonl", "t")
    assert result.checks[session][failed] is False


def test_registering_before_approval_fails_s1(tmp_path):
    loaded = load_chain("semester_abroad")
    early = {
        "tool_calls": [
            {
                "id": "early",
                "type": "function",
                "function": {
                    "name": "course_registration_system",
                    "arguments": json.dumps(
                        {
                            "course_reference_numbers": ["21004"],
                            "registration_action": "add",
                            "registration_term": "spring_2027",
                        }
                    ),
                },
            }
        ]
    }
    solution = {**loaded.solution, "s1": [early, *loaded.solution["s1"]]}
    result = run_chain(loaded, lambda sid: ScriptedModel(solution[sid]), tmp_path / "e.jsonl", "t")
    assert result.checks["s1"]["nothing_registered_before_approval"] is False
