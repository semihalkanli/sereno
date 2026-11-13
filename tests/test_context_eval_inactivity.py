"""Neutral inactivity metrics from structured audit records, without model or container calls."""

import json
import shutil

from sereno.context_eval import pairing

EMPTY = {
    "no_action_calls": 0,
    "plain_text_calls": 0,
    "unknown_ending_calls": 0,
    "sessions_without_applied_actions": 0,
}


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def session(arm, name, applied=None, **result):
    folder = arm / "sessions" / name
    data = {"status": "complete", **result}
    if applied is not None:
        data["applied_attack_actions"] = applied
    write_json(folder / "result.json", data)
    return folder


def call(folder, name, *, actions=0, ending: str | None = "finish", **fields):
    record = {
        "kind": "in_task",
        "status": "no_action",
        "program": {"returncode": 0},
        "actions": actions,
        **fields,
    }
    if ending is not None:
        record["loop_ending"] = ending
    write_json(folder / "run_attacker" / name / "record.json", record)


def populated_campaign(root):
    arm = root / "cases" / "first" / "arms" / "attack_carry"
    empty = session(arm, "001-empty", 0)
    call(empty, "001", ending="plain_text")
    call(empty, "002", kind="session_end")
    call(empty, "003", ending="declined", declined=True, declined_attempts=3)
    call(empty, "004", ending=None)
    call(empty, "005", actions=1, status="rejected")
    call(empty, "006", actions=1, ending="plain_text", status="not_applied")
    write_json(
        empty / "run_attacker/007/record.json",
        {"kind": "in_task", "status": "infrastructure_error", "program": {"returncode": 1}},
    )
    write_json(
        empty / "run_attacker/008/record.json",
        {"kind": "in_task", "status": "skipped", "actions": 0, "loop_ending": "plain_text"},
    )
    applied = session(arm, "002-applied", 2)
    call(applied, "001", actions=2, status="applied", applied_attack_actions=2)
    return arm


def test_empty_responses_and_loop_endings_stay_separate_from_declines(tmp_path):
    populated_campaign(tmp_path)
    summary = pairing.campaign_summary(tmp_path, 20)
    chain = summary["chains"]["first"]
    expected = {
        "no_action_calls": 4,
        "plain_text_calls": 2,
        "unknown_ending_calls": 1,
        "sessions_without_applied_actions": 1,
    }
    assert {key: chain[key] for key in expected} == expected
    assert summary["inactivity_by_arm"] == {"clean": EMPTY, "attack_carry": expected}
    assert [row["applied_attack_actions"] for row in chain["sessions"]] == [0, 2]
    assert chain["declined_calls"] == 1
    assert chain["declined_attempts"] == 3
    assert summary["declines_by_arm"]["attack_carry"] == {"declined_calls": 1, "declined_attempts": 3}
    assert chain["realized_usage"]["calls"] == 8
    assert chain["realized_usage"]["writes"] == 1


def test_inactivity_totals_include_attempts_and_chains_without_counting_copies(tmp_path):
    arm = populated_campaign(tmp_path)
    copied = arm.parent / "attack_reset"
    shutil.copytree(arm, copied)
    for folder in (copied / "sessions").iterdir():
        write_json(folder / "branch.json", {})
    archived = tmp_path / "superseded" / "earlier" / "cases" / "first" / "arms" / "attack_carry"
    call(session(archived, "001-failed", 0, status="invalid"), "001", ending=None)
    other = tmp_path / "cases" / "second" / "arms" / "attack_carry"
    call(session(other, "001-empty", 0), "001", ending="max_steps")
    session(tmp_path / "clean" / "first", "001-clean")
    summary = pairing.campaign_summary(tmp_path, 20)
    assert summary["inactivity_by_arm"] == {
        "clean": EMPTY,
        "attack_reset": EMPTY,
        "attack_carry": {
            "no_action_calls": 6,
            "plain_text_calls": 2,
            "unknown_ending_calls": 2,
            "sessions_without_applied_actions": 3,
        },
    }
    assert summary["chains"]["first"]["no_action_calls"] == 5
    assert summary["chains"]["first"]["sessions_without_applied_actions"] == 2
    assert summary["chains"]["second"]["no_action_calls"] == 1
    assert summary["declines_by_arm"]["attack_reset"] == {"declined_calls": 0, "declined_attempts": 0}


def test_missing_session_application_count_is_not_assumed_zero(tmp_path):
    arm = tmp_path / "cases" / "first" / "arms" / "attack_carry"
    call(session(arm, "001-legacy"), "001", ending=None)
    call(
        session(arm, "002-unknown", applied_attack_actions=None),
        "001",
        actions=1,
        status="infrastructure_error",
        applied_attack_actions=None,
    )
    chain = pairing.campaign_summary(tmp_path, 20)["chains"]["first"]
    assert chain["no_action_calls"] == chain["unknown_ending_calls"] == 1
    assert chain["sessions_without_applied_actions"] == 0
    assert [row["applied_attack_actions"] for row in chain["sessions"]] == [None, None]


def test_pair_summary_keeps_campaign_and_arm_inactivity_totals_independent(tmp_path, monkeypatch):
    defended, undefended = tmp_path / "defended", tmp_path / "undefended"
    populated_campaign(defended)
    arm = undefended / "cases" / "first" / "arms" / "attack_carry"
    call(session(arm, "001-empty", 0), "001", ending="plain_text")
    manifest = {"config_sha256": "fixture", "run_attacker": {"command": ["unused"]}}
    for root in (defended, undefended):
        write_json(root / "manifest.json", manifest)
    monkeypatch.setattr(pairing, "check_command_identity", lambda _: None)
    report = pairing.summarize_pair(
        {
            "campaigns": {side: {"config_sha256": "fixture"} for side in ("defended", "undefended")},
            "run_attacker": manifest["run_attacker"],
            "result_name": "fixture",
            "independent_unit": "chain",
        },
        defended,
        undefended,
    )
    left, right = (report["campaigns"][side] for side in ("defended", "undefended"))
    assert left["inactivity_by_arm"]["attack_carry"]["no_action_calls"] == 4
    assert right["inactivity_by_arm"]["attack_carry"]["no_action_calls"] == 1
    assert left["inactivity_by_arm"]["clean"] == right["inactivity_by_arm"]["clean"] == EMPTY
    assert right["inactivity_by_arm"]["attack_carry"]["plain_text_calls"] == 1
    assert right["declines_by_arm"]["attack_carry"] == {"declined_calls": 0, "declined_attempts": 0}
