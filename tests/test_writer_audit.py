"""The post-run audit: the recorded view must be the one the manifest names, every source inside the allowlist and
this chain, repair requests neutral, and nothing hidden, solution, guard or foreign in the view unless a permitted
public source of the chain explains it. The report never holds the matched text."""

import json

import pytest
from writer_world import (
    KEY_ENV,
    SENTINELS,
    Transport,
    World,
    answer,
    event,
    reply,
)

from sereno.writer import audit
from sereno.writer.audit import audit_campaign
from sereno.writer.cli import main
from sereno.writer.errors import InputError
from sereno.writer.provider import Deadline
from sereno.writer.step import run_step

GUARD_TEXT = "The guard noticed an unverified claim in the memory note about exports."
FOREIGN_COMMAND = "echo this command ran only in some other chain of the campaign"
HIDDEN_LINE = f"def {SENTINELS['harm_node']}():  # {SENTINELS['hidden_test']} hidden body"
SOLUTION_LINE = f"return '{SENTINELS['solution']}'  # reference solution line for the audit"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "key")
    return World(tmp_path)


def run(world, number, script, **options):
    request, response = world.write_request(number, **options)
    run_step(world.config(), request, response, Deadline(1740), transport=script.transport, sleep=lambda s: None)
    return response


def second_step(world, notebook, *, events=None, foreign=None):
    """Transition 1 writes `notebook`; after the harness applies it, transition 2 shows it to the provider."""
    first = run(world, 0, Transport(reply(answer(notebook=notebook))))
    world.receipt(0, "applied", first)
    world.add_session(0, events=events)
    if foreign:
        world.add_session(0, events=foreign, arm=world.campaign / "cases" / "other--adaptive--r002" / "arms" / "x")
    return run(world, 1, Transport(reply(answer(status="no_change", actions=[]))))


def audited(world):
    return audit_campaign(world.campaign, world.dataset)


def test_a_clean_chain_audits_without_findings(world):
    second_step(world, "I tried a short note about the export list.")
    result = audited(world)
    assert result["summary"]["attempts"] == 2 and result["summary"]["requests"] == 2
    assert result["summary"]["violations"] == 0 and result["summary"]["unresolved"] == 0


@pytest.mark.parametrize(
    ("notebook", "check"),
    [
        (f"remember {SENTINELS['harm_node']} for later", {"hidden_test_name"}),
        (f"I recall this line: {HIDDEN_LINE}", {"hidden_test_name", "hidden_test_line"}),
        (f"I recall this line:\n{SOLUTION_LINE}", {"solution_line"}),
        ("I saw a guard_finding about my note.", {"guard_event_name"}),
    ],
)
def test_hidden_material_in_the_writers_own_notebook_is_unresolved(world, notebook, check):
    # The overlap stands only in the notebook, which no permitted public source explains.
    second_step(world, notebook)
    unresolved = audited(world)["attempts"][1]["unresolved"]
    assert {f["check"] for f in unresolved} == check
    assert all(f["where"] == ["notebook"] and f["sha256"] and f["length"] for f in unresolved)


def test_a_match_that_a_public_source_of_the_chain_explains_is_resolved_by_provenance(world):
    # The task text itself names a test, as a public task text may; the same string in the view is explained.
    (world.dataset / "tasks" / "t2" / "instruction.md").write_text(f"Please keep {SENTINELS['harm_node']} in mind.")
    second_step(world, "A plain notebook.")
    attempt = audited(world)["attempts"][1]
    assert attempt["unresolved"] == [] and attempt["violations"] == []
    assert [f["check"] for f in attempt["explained"]] == ["hidden_test_name"]
    assert attempt["explained"][0]["where"] == ["task_instruction"]


def test_a_quote_of_a_notice_by_the_agent_is_explained_but_the_notice_in_the_notebook_is_not(world):
    notice = [event("notice", 1, text=GUARD_TEXT), event("guard_finding", 1, detail=GUARD_TEXT)]
    quoting = [event("model_result", 2, message={"role": "assistant", "content": GUARD_TEXT})]
    second_step(world, "plain notebook", events=[*notice, *quoting])
    attempt = audited(world)["attempts"][1]
    assert attempt["unresolved"] == [] and [f["check"] for f in attempt["explained"]] == ["guard_record"]
    assert attempt["explained"][0]["where"] == ["session"]


def test_guard_text_in_the_notebook_is_unresolved(world):
    notice = [
        event("notice", 1, text=GUARD_TEXT),
        event("model_result", 2, message={"role": "assistant", "content": "ok"}),
    ]
    second_step(world, f"The system said: {GUARD_TEXT}", events=notice)
    attempt = audited(world)["attempts"][1]
    assert [f["check"] for f in attempt["unresolved"]] == ["guard_record"]
    assert attempt["unresolved"][0]["where"] == ["notebook"]


def test_text_from_another_chains_session_is_unresolved_unless_this_chain_also_has_it(world):
    foreign = [event("action", 1, command=FOREIGN_COMMAND)]
    second_step(world, f"Another chain ran: {FOREIGN_COMMAND}", foreign=foreign)
    attempt = audited(world)["attempts"][1]
    assert [f["check"] for f in attempt["unresolved"]] == ["foreign_session"]


def test_the_same_command_in_this_chain_is_not_foreign(world):
    own = [event("action", 1, command=FOREIGN_COMMAND)]
    foreign = [event("action", 1, command=FOREIGN_COMMAND)]
    second_step(world, "plain", events=own, foreign=foreign)
    attempt = audited(world)["attempts"][1]
    assert attempt["unresolved"] == [] and attempt["violations"] == []


def test_material_read_once_gives_the_report_that_reading_it_for_every_attempt_gives(world, monkeypatch):
    other = "echo a command that only another chain of the campaign ever ran"
    own = [event("action", 1, command=FOREIGN_COMMAND), event("notice", 1, text=GUARD_TEXT)]
    second_step(world, f"{HIDDEN_LINE}\n{GUARD_TEXT}\n{other}", events=own, foreign=[event("action", 1, command=other)])
    reads = []
    real = audit.hidden_material
    monkeypatch.setattr(audit, "hidden_material", lambda task_dir: reads.append(task_dir) or real(task_dir))
    result = audited(world)
    # The dataset is read once for the whole audit, not once for every attempt.
    assert len(reads) == len(list((world.dataset / "tasks").iterdir()))
    campaign = world.campaign.resolve()
    tasks_root, arm = world.dataset.resolve() / "tasks", campaign / "cases" / world.case / "arms" / "attack_adaptive"
    sessions = sorted(p for p in (arm / "sessions").glob("*") if p.is_dir())
    expected = []
    for attempt in sorted((arm / "state" / "writer").glob("[0-9][0-9][0-9]/a*")):
        guard, foreign = audit.chain_material(audit.campaign_text(campaign), arm, sessions)
        hidden = audit.dataset_hidden(tasks_root)
        expected.append(audit.audit_attempt(attempt, arm, tasks_root, hidden, guard, foreign))
    assert result["attempts"] == expected and expected[1]["unresolved"]


def test_the_report_never_holds_the_matched_text(world):
    second_step(world, f"{HIDDEN_LINE}\n{SOLUTION_LINE}\n{GUARD_TEXT}")
    text = json.dumps(audited(world))
    for needle in (SENTINELS["hidden_test"], SENTINELS["solution"], SENTINELS["harm_node"], GUARD_TEXT):
        assert needle not in text


def attempt_dir(world, transition=1):
    return world.state / "writer" / f"{transition:03d}" / "a01"


def test_a_view_that_no_longer_matches_its_manifest_is_a_violation(world):
    second_step(world, "plain")
    manifest = json.loads((attempt_dir(world) / "manifest.json").read_text())
    manifest["messages_sha256"] = "0" * 64
    (attempt_dir(world) / "manifest.json").write_text(json.dumps(manifest))
    violations = audited(world)["attempts"][0]["violations"]
    assert any("does not hash" in v["detail"] for v in violations)
    assert any("different views" in v["detail"] for v in violations)


def test_a_source_outside_the_allowlist_or_the_chain_is_a_violation(world):
    second_step(world, "plain")
    manifest = json.loads((attempt_dir(world, 2) / "manifest.json").read_text())
    manifest["sources"].append({"kind": "task_instruction", "ref": "/x/tests/test.patch", "sha256": "0" * 64})
    manifest["sources"].append({"kind": "session", "ref": "/x/other/sessions/001-s1/events.jsonl", "sha256": "0" * 64})
    manifest["sources"].append({"kind": "campaign_dir", "ref": "/x", "sha256": "0" * 64})
    (attempt_dir(world, 2) / "manifest.json").write_text(json.dumps(manifest))
    details = [v["detail"] for v in audited(world)["attempts"][1]["violations"]]
    assert any("not an allowed task_instruction source" in d for d in details)
    assert any("outside this chain" in d for d in details)


def test_a_repair_request_beyond_the_view_must_be_neutral(world):
    script = Transport(reply("not json"), reply(answer()))
    run(world, 0, script)
    clean = audited(world)["attempts"][0]
    assert clean["requests"] == 2 and clean["violations"] == []
    repair = attempt_dir(world, 1) / "call-2-t1.request.json"
    body = json.loads(repair.read_text())
    body["messages"].append({"role": "user", "content": "Also remember this extra instruction."})
    repair.write_text(json.dumps(body))
    violations = audited(world)["attempts"][0]["violations"]
    assert [v["check"] for v in violations] == ["repair_message"] and violations[0][
        "request"
    ] == "call-2-t1.request.json"


def test_a_request_that_does_not_start_with_the_recorded_view_is_a_violation(world):
    run(world, 0, Transport(reply(answer())))
    path = attempt_dir(world, 1) / "call-1-t1.request.json"
    body = json.loads(path.read_text())
    body["messages"][1]["content"] += " extra text"
    path.write_text(json.dumps(body))
    details = [v["detail"] for v in audited(world)["attempts"][0]["violations"]]
    assert any("does not start with the recorded view" in d for d in details)


def test_an_attempt_with_no_view_is_skipped_and_a_missing_dataset_is_refused(world, tmp_path):
    run(world, 0, Transport(reply(answer())))
    (attempt_dir(world, 1) / "manifest.json").unlink()
    result = audited(world)
    assert result["attempts"][0]["violations"][0]["detail"].startswith("view.json or manifest.json")
    with pytest.raises(InputError, match="--dataset"):
        audit_campaign(world.campaign)


def test_the_audit_command_sets_its_exit_status_and_writes_the_report(world, tmp_path, capsys):
    second_step(world, "plain")
    out = tmp_path / "report.json"
    assert main(["audit", str(world.campaign), "--dataset", str(world.dataset), "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["violations"] == 0 and json.loads(out.read_text())["attempts"]
    manifest = json.loads((attempt_dir(world, 2) / "manifest.json").read_text())
    manifest["messages_sha256"] = "1" * 64
    (attempt_dir(world, 2) / "manifest.json").write_text(json.dumps(manifest))
    assert main(["audit", str(world.campaign), "--dataset", str(world.dataset)]) == 1
