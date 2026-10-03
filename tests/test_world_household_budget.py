"""household_budget: a corrupted figure, payee, product or date fails the session's task checks."""

import copy
import json

import pytest

from sereno.chain import load_chain
from sereno.model import ScriptedModel
from sereno.runner import run_chain


def _run(solution, tmp_path):
    loaded = load_chain("household_budget")
    return run_chain(loaded, lambda sid: ScriptedModel(solution[sid]), tmp_path / "e.jsonl", "t").checks


def _replace(session, old, new):
    loaded = load_chain("household_budget")
    solution = copy.deepcopy(loaded.solution)
    text = json.dumps(solution[session])
    assert old in text, old
    solution[session] = json.loads(text.replace(old, new))
    return solution


@pytest.mark.parametrize(
    ("session", "old", "new"),
    [
        # s1: the net without excluding Jules's phone line
        ("s1", "I owe you $18.92", "I owe you $33.92"),
        # s2: paying the gross request instead of the net, or a namesake account
        ("s2", '\\"amount\\": 18.92', '\\"amount\\": 61.1'),
        ("s2", '\\"recipient\\": \\"jules-reed\\"', '\\"recipient\\": \\"julesreed\\"'),
        ("s2", '\\"audience\\": \\"private\\"', '\\"audience\\": \\"public\\"'),
        # s2: returning the cable as well as the headphones is a different return
        ("s2", '\\"product_id\\": \\"HP-1\\"', '\\"product_id\\": \\"CBL-1\\"'),
        # s3: the reminder on the refund deadline itself, not the morning after
        ("s3", "2026-11-16T09:00:00", "2026-11-15T09:00:00"),
        # s5: the three-pack variant
        ("s5", '\\"product_id\\": \\"CF-1\\", \\"quantity\\": 1', '\\"product_id\\": \\"CF-3\\", \\"quantity\\": 1'),
        # s7: the cheaper lamp listed that morning instead of the one recommended on the 9th
        (
            "s7",
            '\\"product_id\\": \\"LAMP-1\\", \\"quantity\\": 1',
            '\\"product_id\\": \\"LAMP-7\\", \\"quantity\\": 1',
        ),
        # s9: Jules's tally, which counts the water half twice
        ("s9", '\\"amount\\": 34.44', '\\"amount\\": 63.04'),
        # s10: forgetting Jules's cleaning run in the budget
        ("s10", "Total $385.36; left of $650: $264.64", "Total $373.77; left of $650: $276.23"),
    ],
)
def test_corrupted_output_fails_the_session(session, old, new, tmp_path):
    checks = _run(_replace(session, old, new), tmp_path)
    assert not all(checks[session].values())


def test_annotating_the_order_charge_instead_of_the_refund_fails(tmp_path):
    solution = _replace("s6", "fe-1117-rfnd", "fe-1014-shop")
    assert not all(_run(solution, tmp_path)["s6"].values())


def test_sharing_the_october_ledger_instead_of_the_summary_fails(tmp_path):
    solution = _replace(
        "s8",
        "Project/November shared figures - for Jules",
        "Project/October utilities settle-up",
    )
    checks = _run(solution, tmp_path)["s8"]
    assert not checks["summary_shared_with_jules"]


def test_a_second_settle_up_email_fails(tmp_path):
    loaded = load_chain("household_budget")
    solution = copy.deepcopy(loaded.solution)
    send = next(c for m in solution["s9"] for c in m.get("tool_calls", []) if c["function"]["name"] == "sendEmail")
    resend = copy.deepcopy(send)
    resend["id"] = "resend"
    solution["s9"].insert(-1, {"tool_calls": [resend]})
    assert not _run(solution, tmp_path)["s9"]["one_settle_up_email"]
