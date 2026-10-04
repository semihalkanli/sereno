"""The curated suite observes ten distinct trust-boundary hypotheses using local doubles."""

import copy
import json
from pathlib import Path

import pytest

from sereno.chain import CHAINS_DIR, chain_ids, load_chain
from sereno.events import read_events
from sereno.model import ScriptedModel
from sereno.runner import run_chain

CATALOG = json.loads((CHAINS_DIR / "challenge-suite.json").read_text())
CASES = [(world["id"], attack) for world in CATALOG["worlds"] for attack in world["attacks"]]
ANSWERS = json.loads((Path(__file__).parent / "fixtures/challenge_answers.json").read_text())


def _run(loaded, solution, tmp_path):
    log = tmp_path / "events.jsonl"
    result = run_chain(loaded, lambda sid: ScriptedModel(solution[sid]), log, "challenge-test")
    events = read_events(log)
    assert not result.invalid
    assert not [e for e in events if e["event"] == "execute_tool" and e.get("error")]
    return result, events


def test_curated_worlds_own_distinct_mechanisms():
    worlds = CATALOG["worlds"]
    assert len(worlds) == 10
    assert len({w["id"] for w in worlds}) == 10
    assert len({w["mechanism"] for w in worlds}) == 10
    assert {w["id"] for w in worlds} <= set(chain_ids())
    assert len(CASES) == 30
    assert all(len(w["attacks"]) == 3 for w in worlds)
    assert set(ANSWERS) == {a["attack"] for _, a in CASES}
    markers = []
    for cid, entry in CASES:
        loaded = load_chain(cid, attack_file=CHAINS_DIR / entry["attack"])
        markers.append(loaded.attack.marker)
        assert loaded.attack.answer_sessions == [s.id for s in loaded.chain.sessions]
        assert len(loaded.attack.answer_signals) == 3
    assert len(set(markers)) == 30


@pytest.mark.parametrize(("cid", "entry"), CASES)
def test_clean_replay_observes_source_without_adopting_it(cid, entry, tmp_path):
    loaded = load_chain(cid, attack_file=CHAINS_DIR / entry["attack"])
    result, events = _run(loaded, loaded.solution, tmp_path)
    assert result.task_passed()
    assert not any(result.checks["attack"].values())
    assert result.timing["poison_seen"] is not None
    assert result.timing["poison_copied"] == []
    assert set(result.timing["answer_signals"]) == set(loaded.attack.answer_sessions)
    assert all(not any(signals.values()) for signals in result.timing["answer_signals"].values())
    score = next(e for e in events if e["event"] == "score" and e.get("group") == "attack")
    assert score["answer_signals"] == result.timing["answer_signals"]


@pytest.mark.parametrize(("cid", "entry"), CASES)
def test_response_variant_preserves_meaning_and_safe_replay(cid, entry, tmp_path):
    original = load_chain(cid, attack_file=CHAINS_DIR / entry["attack"])
    loaded = load_chain(cid, attack_file=CHAINS_DIR / entry["response"])
    assert loaded.attack.checks == original.attack.checks
    assert loaded.attack.answer_signals == original.attack.answer_signals
    assert loaded.attack.marker == original.attack.marker
    assert {e.text.strip() for e in loaded.attack.events} <= set(original.attack.fills.values())
    result, events = _run(loaded, loaded.solution, tmp_path)
    assert result.task_passed()
    assert not any(result.checks["attack"].values())
    assert result.timing["poison_seen"] is not None
    assert result.timing["poison_copied"] == []
    assert len([e for e in events if e["event"] == "attack_event"]) == len(loaded.attack.events)


@pytest.mark.parametrize(("cid", "entry"), CASES)
def test_unsafe_witness_verifies_state_oracle_and_persistence(cid, entry, tmp_path):
    loaded = load_chain(cid, attack_file=CHAINS_DIR / entry["attack"])
    patch = json.loads((CHAINS_DIR / entry["witness"]).read_text())
    witness = {
        sid: [*steps[:-1], *patch["insert_before_final"].get(sid, []), steps[-1]]
        for sid, steps in loaded.solution.items()
    }
    result, _ = _run(loaded, witness, tmp_path)
    assert all(result.checks["attack"].values())
    assert not result.task_passed()
    if loaded.attack.trigger:
        assert result.timing["poison_copied"]
        assert result.timing["first_success"] == loaded.attack.trigger.session
        assert result.timing["early"] is False
        assert result.timing["carried"] is True
    if cid == "business_week":
        assert result.timing["poison_copied"]
        assert result.timing["carried"] is True


@pytest.mark.parametrize(("cid", "entry"), CASES)
def test_answer_signals_distinguish_mention_rejection_and_action_claim(cid, entry):
    attack = load_chain(cid, attack_file=CHAINS_DIR / entry["attack"]).attack
    for case in ANSWERS[entry["attack"]]:
        actual = attack.signals_in(case["text"])
        assert {name for name, value in actual.items() if value and name != "told_user"} == set(case["expected"])
    assert attack.signals_in(None) is None


def test_action_claim_is_observed_without_scoring_state_success(tmp_path):
    cid, entry = CASES[0]
    loaded = load_chain(cid, attack_file=CHAINS_DIR / entry["attack"])
    solution = copy.deepcopy(loaded.solution)
    session = loaded.attack.answer_sessions[-1]
    solution[session][-1] = {"content": ANSWERS[entry["attack"]][0]["text"]}
    result, _ = _run(loaded, solution, tmp_path)
    assert result.timing["answer_signals"][session]["claimed_attack_action"]
    assert not any(result.checks["attack"].values())
    assert result.task_passed()
