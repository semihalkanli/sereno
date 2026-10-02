"""Every chain in chains/: its solution passes, and each session's checks catch an agent that does nothing."""

import copy
import json

import pytest

from sereno.chain import CHAINS_DIR, chain_ids, load_chain
from sereno.model import ScriptedModel
from sereno.runner import run_chain

SOLVED = [c for c in chain_ids() if load_chain(c).solution is not None]
ATTACKS = [(c, p.stem) for c in chain_ids() for p in sorted((CHAINS_DIR / c / "attacks").glob("*.json"))]


def _run(loaded, solution, tmp_path):
    return run_chain(loaded, lambda sid: ScriptedModel(solution[sid]), tmp_path / "events.jsonl", "t").checks


@pytest.mark.parametrize("chain_id", SOLVED)
def test_solution_passes_every_check(chain_id, tmp_path):
    loaded = load_chain(chain_id)
    checks = _run(loaded, loaded.solution, tmp_path)
    failed = [f"{group}/{name}" for group, result in checks.items() for name, ok in result.items() if not ok]
    assert not failed


@pytest.mark.parametrize(
    ("chain_id", "session"),
    [(c, s.id) for c in SOLVED for s in load_chain(c).chain.sessions],
)
def test_idle_session_fails_a_check(chain_id, session, tmp_path):
    loaded = load_chain(chain_id)
    solution = {**loaded.solution, session: [{"content": "Done."}]}
    checks = _run(loaded, solution, tmp_path)[session]
    assert checks and not all(checks.values())


@pytest.mark.parametrize(("chain_id", "attack_id"), ATTACKS)
def test_attack_loads_and_fails_against_the_solution(chain_id, attack_id, tmp_path):
    loaded = load_chain(chain_id, attack_id)
    assert loaded.attack.checks
    checks = _run(loaded, loaded.solution, tmp_path)
    assert not any(checks["attack"].values())


def test_business_week_attack_check_sees_the_luxury_booking(tmp_path):
    loaded = load_chain("business_week", "review_insurance_rebook")
    solution = copy.deepcopy(loaded.solution)
    for message in solution["s3"]:
        for call in message.get("tool_calls", []):
            if call["function"]["name"] == "accommodations_order_create":
                args = json.loads(call["function"]["arguments"])
                call["function"]["arguments"] = json.dumps(
                    {**args, "accommodation_id": "hp-aurelian", "product_id": "au-king"}
                )
    checks = _run(loaded, solution, tmp_path)
    assert checks["attack"]["luxury_hotel_booked"]
    assert not checks["s3"]["hotel_booked"] and not checks["final"]["no_luxury_hotel_booking"]


def test_until_stops_after_the_session_and_skips_final_checks(tmp_path):
    loaded = load_chain("business_week", "review_insurance_rebook")
    result = run_chain(loaded, lambda sid: ScriptedModel(loaded.solution[sid]), tmp_path / "e.jsonl", "t", until="s3")
    assert list(result.checks) == ["s1", "s2", "s3", "attack"]
