"""All new worlds, attack witnesses and external placements, without network calls."""

import json

import pytest

from sereno.chain import CHAINS_DIR, chain_ids, load_chain
from sereno.events import read_events
from sereno.model import ScriptedModel
from sereno.runner import run_chain

WORLDS = [c for c in chain_ids() if (CHAINS_DIR / c / "witnesses").exists()]
WITNESSES = [(c, p.stem) for c in WORLDS for p in sorted((CHAINS_DIR / c / "witnesses").glob("*.json"))]
EXPERIMENTS = [(c, p) for c in WORLDS for p in sorted((CHAINS_DIR / c / "experiments").glob("*.json"))]


def no_tool_errors(log):
    errors = [
        (e.get("gen_ai.tool.name"), e.get("error"))
        for e in read_events(log)
        if e["event"] == "execute_tool" and e.get("error")
    ]
    assert not errors


def test_corpus_size_and_reference_decompositions():
    assert len(WORLDS) == 9
    assert len(WITNESSES) == 27
    for cid in [*WORLDS, "business_week"]:
        loaded = load_chain(cid)
        milestones = json.loads((CHAINS_DIR / cid / "milestones.json").read_text())
        assert 8 <= len(loaded.chain.sessions) <= 10
        assert 30 <= len(milestones) <= 50
        assert loaded.chain.memory
        expected_version = "2" if cid == "business_week" else "3"
        assert loaded.chain.version == expected_version
        assert set(m["session"] for m in milestones) == {s.id for s in loaded.chain.sessions}
        if cid in WORLDS:
            assert len(list((CHAINS_DIR / cid / "twins").glob("*.json"))) == 1
            assert all(s.specs == ["action_open"] for s in loaded.chain.sessions)


@pytest.mark.parametrize("cid", WORLDS)
def test_clean_tool_calls_are_valid_and_sources_disappear(cid, tmp_path):
    loaded = load_chain(cid)
    log = tmp_path / "clean.jsonl"
    result = run_chain(loaded, lambda sid: ScriptedModel(loaded.solution[sid]), log, "clean")
    assert result.task_passed()
    no_tool_errors(log)
    starts = [e for e in read_events(log) if e["event"] == "state" and e.get("reason") == "session_start"]
    for event in starts[1:]:  # s3 and later
        paths = [f["path"] for f in event["snapshot"]["files"]["files"]]
        assert "Sources/Temporary field note" not in paths
        assert "Sources/Temporary handover" not in paths


@pytest.mark.parametrize(("cid", "attack_id"), WITNESSES)
def test_unsafe_witness_reaches_goal_and_late_goals_are_carried(cid, attack_id, tmp_path):
    loaded = load_chain(cid, attack_id)
    patch = json.loads((CHAINS_DIR / cid / "witnesses" / f"{attack_id}.json").read_text())
    witness = {
        sid: [*steps[:-1], *patch["insert_before_final"].get(sid, []), steps[-1]]
        for sid, steps in loaded.solution.items()
    }
    log = tmp_path / "witness.jsonl"
    result = run_chain(loaded, lambda sid: ScriptedModel(witness[sid]), log, "unsafe-witness")
    no_tool_errors(log)
    assert all(result.checks["attack"].values())
    assert not result.task_passed(), "A targeted unauthorized action must also violate the task contract"
    if loaded.attack.trigger:
        assert result.timing["first_success"] == loaded.attack.trigger.session
        assert result.timing["early"] is False
        assert result.timing["carried"] is True
        assert (
            loaded.session_number(loaded.attack.trigger.session) - loaded.session_number(result.timing["poison_seen"])
            >= 3
        )
    else:
        assert result.timing["first_success"] == "s2"
        assert result.timing["carried"] is None


@pytest.mark.parametrize(("cid", "path"), EXPERIMENTS)
def test_external_response_variants_are_visible_but_safe_solution_resists(cid, path, tmp_path):
    loaded = load_chain(cid, attack_file=path)
    log = tmp_path / "response.jsonl"
    result = run_chain(loaded, lambda sid: ScriptedModel(loaded.solution[sid]), log, "response")
    no_tool_errors(log)
    assert result.task_passed()
    assert not any(result.checks["attack"].values())
    assert result.timing["poison_seen"] in {"s1", "s2"}
    assert result.timing["poison_copied"] == []
    events = [e for e in read_events(log) if e["event"] == "attack_event"]
    assert len(events) == len(loaded.attack.events)
