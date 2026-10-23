"""One writer step end to end with a scripted provider: the hook contract, failures that leave no response, the
repair rules, the notebook transaction against the harness's receipts, resume without a second charge, and the
last-write sentence."""

import hashlib
import json

import httpx
import pytest
from writer_world import (
    AGENT_NOTE,
    INDEX,
    KEY_ENV,
    LAST_WRITE,
    LIMITS,
    NOTE,
    PATTERNS,
    ROOT,
    Transport,
    World,
    answer,
    answer_keeping,
    reply,
)

from sereno.context_eval.adaptive import load_response, validate_actions
from sereno.context_eval.schema import AdaptiveLimits
from sereno.writer.errors import ConfigError, InputError, OutputError, ProviderFailure
from sereno.writer.parsers import repair_message
from sereno.writer.provider import Deadline
from sereno.writer.step import run_step

SECRET = "secret-key-value"
MEMORY = {INDEX: "- [Agent](agent.md) - agent note\n", AGENT_NOTE: "Agent note.\n"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, SECRET)
    return World(tmp_path)


def run(world, number, script, *, config=None, **options):
    request, response = world.write_request(number, **options)
    summary = run_step(
        config or world.config(), request, response, Deadline(1740), transport=script.transport, sleep=lambda s: None
    )
    return summary, response


def attempt(world, transition=1, name="a01"):
    return world.state / "writer" / f"{transition:03d}" / name


def read(path):
    return json.loads(path.read_text())


def bad_json():
    return reply("this is not json")


def forbidden_answer():
    return reply(answer(actions=[{"op": "write", "path": NOTE, "content": "a forbiddenword here\n"}]))


def test_a_ready_answer_becomes_the_hook_contract_and_is_staged_and_recorded(world):
    script = Transport(reply(answer(), reasoning={"reasoning": "my reasoning"}))
    summary, response = run(world, 0, script)
    assert summary["outcome"] == "delivered_ready" and summary["calls"] == 1 and summary["cost"] == 0.0123
    body = load_response(response)  # the hook's own reader
    assert body["note"] == "Add a topic note and its index line." and len(body["actions"]) == 2
    assert set(json.loads(response.read_text())) == {"schema_version", "actions", "note"}
    assert validate_actions({}, body["actions"], set(), AdaptiveLimits.model_validate(LIMITS), "repo") == []
    folder = attempt(world)
    candidate = read(folder / "candidate.json")
    assert candidate["response_sha256"] == hashlib.sha256(response.read_bytes()).hexdigest()
    assert candidate["notebook"] == "Attempted a topic note about the export list."
    record = read(folder / "attempt.json")
    assert record["outcome"] == "delivered_ready" and record["calls"] == 1 and record["transport_attempts"] == 1
    inputs = read(folder / "input.json")
    assert inputs["requested_model"] == "deepseek/test-model" and inputs["last_write"] is False
    assert len(inputs["code_sha256"]) == 64 and inputs["assignment_review"] == "proposed_pending_user_review"
    assert read(folder / "view.json")[0]["role"] == "system" and read(folder / "manifest.json")["sources"]
    line = json.loads((folder / "candidates.jsonl").read_text())
    assert line["call"] == 1 and line["kind"] == "initial" and line["reasoning"] == {"reasoning": "my reasoning"}
    assert (
        line["completion"]["provider"] == "DeepSeek" and line["completion"]["returned_model"] == "deepseek/test-model"
    )
    assert (folder / "call-1-t1.request.json").exists() and (folder / "call-1-t1.response.json").exists()
    assert not list(world.arm.rglob("*.partial"))


def test_no_file_the_writer_leaves_holds_the_credential(world):
    run(world, 0, Transport(reply(answer())))
    for path in world.state.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes(), path


def test_a_deliberate_no_change_returns_empty_actions_successfully(world):
    text = answer(status="no_change", actions=[], explanation="Keep the supplied memory unchanged.")
    summary, response = run(world, 0, Transport(reply(text)))
    assert summary["outcome"] == "delivered_no_change"
    assert load_response(response)["actions"] == []
    assert read(response)["note"] == "Keep the supplied memory unchanged."


def test_an_unable_answer_fails_the_step_and_is_never_a_no_change(world):
    script = Transport(reply(answer(status="unable", actions=[], explanation="I cannot do this.")), reply(answer()))
    request, response = world.write_request(0)
    with pytest.raises(OutputError) as caught:
        run_step(world.config(), request, response, Deadline(1740), transport=script.transport)
    assert caught.value.code == "unable" and not response.exists() and len(script.requests) == 1
    record = read(attempt(world) / "attempt.json")
    assert record["outcome"] == "failed" and record["code"] == "unable"


@pytest.mark.parametrize(
    ("items", "code"),
    [
        ([(401, {}, {})], "auth"),
        ([(402, {}, {})], "balance"),
        ([(429, {}, {}), (429, {}, {})], "http_transient"),
        ([httpx.ConnectError("x"), httpx.ConnectError("x")], "transport"),
        ([reply("x", finish="content_filter")], "refusal"),
        ([reply(answer(), finish="length")], "truncated"),
        ([reply("")], "empty_completion"),
        ([{"error": {"message": "upstream"}}], "body_error"),
        ([reply(answer(), provider="Other")], "unexpected_provider"),
    ],
)
def test_provider_trouble_ends_the_step_with_no_response_and_no_candidate(world, items, code):
    script = Transport(*items, reply(answer()))
    request, response = world.write_request(0)
    with pytest.raises(ProviderFailure) as caught:
        run_step(world.config(), request, response, Deadline(1740), transport=script.transport, sleep=lambda s: None)
    assert caught.value.code == code
    assert not response.exists() and not (attempt(world) / "candidate.json").exists()
    record = read(attempt(world) / "attempt.json")
    assert record["outcome"] == "failed" and record["code"] == code
    assert len(script.queue) == 1, "nothing was retried beyond the transient budget or worked around"


def test_a_missing_credential_fails_before_any_call(world, monkeypatch):
    monkeypatch.delenv(KEY_ENV)
    script = Transport(reply(answer()))
    request, response = world.write_request(0)
    with pytest.raises(ConfigError) as caught:
        run_step(world.config(), request, response, Deadline(1740), transport=script.transport)
    assert caught.value.code == "missing_credential" and script.requests == [] and not response.exists()


def test_other_limits_than_the_frozen_design_stop_the_step_before_anything_is_written(world):
    script = Transport(reply(answer()))
    request, response = world.write_request(0, limits=LIMITS | {"forbidden_patterns": PATTERNS[:1]})
    with pytest.raises(ConfigError) as caught:
        run_step(world.config(), request, response, Deadline(1740), transport=script.transport)
    assert caught.value.code == "patterns_differ" and script.requests == [] and not (world.state / "writer").exists()


def test_a_hook_timeout_not_above_the_writer_deadline_stops_the_step(world):
    world.write_manifest(timeout=1740)
    request, response = world.write_request(0)
    with pytest.raises(ConfigError) as caught:
        run_step(world.config(), request, response, Deadline(1740), transport=Transport().transport)
    assert caught.value.code == "deadline"


def test_a_structural_repair_replays_the_reasoning_and_sends_only_neutral_codes(world):
    details = [{"type": "reasoning.text", "text": "first thoughts", "signature": "s1"}]
    first = reply("not json", reasoning={"reasoning_details": details, "reasoning": "first thoughts"})
    script = Transport(first, reply(answer()))
    summary, _ = run(world, 0, script)
    assert summary["outcome"] == "delivered_ready" and summary["calls"] == 2
    initial, repair = script.bodies()
    assert repair["messages"][:2] == initial["messages"]
    assert repair["messages"][2] == {
        "role": "assistant",
        "content": "not json",
        "reasoning_details": details,
        "reasoning": "first thoughts",
    }
    assert repair["messages"][3] == {"role": "user", "content": repair_message(["invalid_json"])}
    assert {k: v for k, v in repair.items() if k != "messages"} == {k: v for k, v in initial.items() if k != "messages"}
    lines = (attempt(world) / "candidates.jsonl").read_text().splitlines()
    assert [json.loads(line)["kind"] for line in lines] == ["initial", "repair"]
    assert json.loads(lines[0])["codes"] == ["invalid_json"] and json.loads(lines[1])["codes"] == []


def test_after_two_failed_repairs_the_step_fails_without_a_fourth_call(world):
    script = Transport(bad_json(), bad_json(), bad_json(), reply(answer()))
    request, response = world.write_request(0)
    with pytest.raises(OutputError) as caught:
        run_step(world.config(), request, response, Deadline(1740), transport=script.transport)
    assert caught.value.code == "unusable_answer" and "invalid_json" in str(caught.value)
    assert len(script.requests) == 3 and len(script.queue) == 1 and not response.exists()
    assert len((attempt(world) / "candidates.jsonl").read_text().splitlines()) == 3
    assert read(attempt(world) / "attempt.json")["code"] == "unusable_answer"


def test_a_ready_answer_that_changes_nothing_fails_after_the_repairs_and_is_not_a_no_change(world):
    same = reply(answer(actions=[{"op": "write", "path": AGENT_NOTE, "content": MEMORY[AGENT_NOTE]}]))
    script = Transport(same, same, same)
    request, response = world.write_request(0, memory=MEMORY)
    with pytest.raises(OutputError, match="no_effective_change"):
        run_step(world.config(), request, response, Deadline(1740), transport=script.transport)
    assert len(script.requests) == 3 and not response.exists()
    assert "no_effective_change" in script.bodies()[1]["messages"][-1]["content"]


def test_a_content_violation_is_repaired_when_the_next_answer_is_clean(world):
    script = Transport(forbidden_answer(), reply(answer()))
    summary, _ = run(world, 0, script)
    assert summary["outcome"] == "delivered_ready" and summary["calls"] == 2
    message = script.bodies()[1]["messages"][-1]["content"]
    assert "content_constraint" in message and "forbiddenword" not in message
    assert all(pattern not in message for pattern in PATTERNS) and "forbidden pattern" not in message


def test_a_content_violation_that_survives_the_repairs_goes_to_the_hook_to_be_rejected(world):
    script = Transport(forbidden_answer(), forbidden_answer(), forbidden_answer())
    summary, response = run(world, 0, script)
    assert summary["outcome"] == "delivered_violating" and summary["calls"] == 3
    assert summary["codes"] == ["content_constraint"]
    body = load_response(response)
    violations = validate_actions({}, body["actions"], set(), AdaptiveLimits.model_validate(LIMITS), "repo")
    assert violations and "forbidden pattern" in violations[0]
    assert len(script.requests) == 3 and read(attempt(world) / "attempt.json")["outcome"] == "delivered_violating"
    for body in script.bodies()[1:]:
        assert "forbiddenword" not in body["messages"][-1]["content"]


def test_a_lone_surrogate_in_a_written_file_never_crashes_the_records(world):
    lone = (
        '{"status":"ready","actions":[{"op":"write","path":"%s","content":"\\ud800"}],"notebook":"n","explanation":"e"}'
    )
    item = reply(lone % NOTE)
    summary, response = run(world, 0, Transport(item, item, item))
    assert summary["outcome"] == "delivered_violating" and summary["codes"] == ["invalid_content"]
    assert "\\ud800" in response.read_text()
    assert load_response(response)["actions"][0]["path"] == NOTE


def test_the_previous_write_is_committed_only_after_the_harness_receipt(world):
    run(world, 0, Transport(reply(answer())))
    assert not (world.state / "writer" / "notebook.json").exists()
    world.receipt(0, "applied", world.arm / "transitions" / "001" / "response.json")
    world.add_session(0)
    script = Transport(reply(answer(status="no_change", actions=[], notebook="Second notebook.")))
    run(world, 1, script, memory=MEMORY)
    sent = json.loads(script.bodies()[0]["messages"][1]["content"])
    assert sent["notebook"] == "Attempted a topic note about the export list."
    assert sent["previous_write"] == {"before_session": 1, "status": "applied", "applied": True}
    committed = read(world.state / "writer" / "notebook.json")
    assert committed["transition"] == 1 and committed["candidate"]["attempt"] == "a01"
    assert [s["index"] for s in sent["completed_sessions"]] == [0]


@pytest.mark.parametrize(("status", "applied"), [("applied", True), ("no_change", False), ("rejected", False)])
def test_applied_no_change_and_rejected_receipts_advance_the_notebook_with_a_label(world, status, applied):
    run(world, 0, Transport(reply(answer())))
    world.receipt(0, status, world.arm / "transitions" / "001" / "response.json")
    world.add_session(0)
    script = Transport(reply(answer(status="no_change", actions=[])))
    run(world, 1, script)
    sent = json.loads(script.bodies()[0]["messages"][1]["content"])
    assert sent["notebook"] == "Attempted a topic note about the export list."
    assert sent["previous_write"] == {"before_session": 1, "status": status, "applied": applied}


@pytest.mark.parametrize("status", ["writer_failed", "timeout"])
def test_a_failed_step_discards_its_staged_candidate(world, status):
    run(world, 0, Transport(reply(answer())))
    world.receipt(0, status, world.arm / "transitions" / "001" / "response.json")
    world.add_session(0)
    script = Transport(reply(answer(status="no_change", actions=[])))
    run(world, 1, script)
    sent = json.loads(script.bodies()[0]["messages"][1]["content"])
    assert sent["notebook"] == "" and "previous_write" not in sent
    assert not (world.state / "writer" / "notebook.json").exists()
    assert '"discarded"' in (world.state / "writer" / "log.jsonl").read_text()


def test_a_receipt_for_another_response_is_refused(world):
    run(world, 0, Transport(reply(answer())))
    folder = world.arm / "transitions" / "001"
    (folder / "transition.json").write_text(
        json.dumps({"index": 0, "status": "applied", "response_sha256": "0" * 64, "kind": "before_first_session"})
    )
    world.add_session(0)
    script = Transport(reply(answer()))
    request, response = world.write_request(1)
    with pytest.raises(InputError) as caught:
        run_step(world.config(), request, response, Deadline(1740), transport=script.transport)
    assert caught.value.code == "bad_state" and script.requests == []


def test_a_missing_receipt_is_refused(world):
    run(world, 0, Transport(reply(answer())))
    world.add_session(0)
    request, response = world.write_request(1)
    with pytest.raises(InputError, match="no readable receipt"):
        run_step(world.config(), request, response, Deadline(1740), transport=Transport().transport)


def test_a_chain_never_sees_another_chains_notebook(world, tmp_path):
    run(world, 0, Transport(reply(answer(notebook="CHAIN_A_PRIVATE_NOTEBOOK"))))
    world.receipt(0, "applied", world.arm / "transitions" / "001" / "response.json")
    world.add_session(0)
    (tmp_path / "other").mkdir()
    other = World(tmp_path / "other", case="t1--adaptive--r002")
    run(other, 0, Transport(reply(answer(notebook="CHAIN_B_NOTEBOOK"))))
    other.receipt(0, "applied", other.arm / "transitions" / "001" / "response.json")
    other.add_session(0)
    follow_b = Transport(reply(answer(status="no_change", actions=[])))
    run(other, 1, follow_b)
    sent = follow_b.requests[0].content.decode()
    assert "CHAIN_A_PRIVATE_NOTEBOOK" not in sent and "CHAIN_B_NOTEBOOK" in sent
    follow_a = Transport(reply(answer(status="no_change", actions=[])))
    run(world, 1, follow_a)
    assert "CHAIN_B_NOTEBOOK" not in follow_a.requests[0].content.decode()


def test_a_resume_reuses_the_recorded_candidate_without_a_second_call(world):
    script = Transport(reply(answer()))
    _, response = run(world, 0, script)
    first_bytes = response.read_bytes()
    response.unlink()
    again = Transport(reply(answer(notebook="must not be asked")))
    summary, response = run(world, 0, again)
    assert summary["outcome"] == "reused" and summary["calls"] == 0
    assert again.requests == [] and response.read_bytes() == first_bytes
    assert [p.name for p in (world.state / "writer" / "001").iterdir()] == ["a01"]


def test_a_changed_input_is_a_new_attempt_and_a_new_call(world):
    run(world, 0, Transport(reply(answer())))
    script = Transport(reply(answer_keeping(MEMORY)))
    summary, _ = run(world, 0, script, memory=MEMORY)
    assert summary["attempt"] == "a02" and len(script.requests) == 1


def test_a_response_the_hook_already_ended_as_writer_failed_is_not_replayed(world):
    _, response = run(world, 0, Transport(reply(answer())))
    sha = hashlib.sha256(response.read_bytes()).hexdigest()
    request_sha = hashlib.sha256((world.arm / "transitions" / "001" / "request.json").read_bytes()).hexdigest()
    folder = world.arm / "transitions" / "stopped" / "20261023T000000Z" / "001"
    folder.mkdir(parents=True)
    record = {"status": "writer_failed", "response_sha256": sha, "request_sha256": request_sha}
    (folder / "transition.json").write_text(json.dumps(record))
    script = Transport(reply(answer()))
    summary, _ = run(world, 0, script)
    assert summary["attempt"] == "a02" and len(script.requests) == 1


def test_a_failure_recorded_for_another_request_does_not_block_reuse(world):
    _, response = run(world, 0, Transport(reply(answer())))
    sha = hashlib.sha256(response.read_bytes()).hexdigest()
    folder = world.arm / "transitions" / "stopped" / "20261023T000000Z" / "001"
    folder.mkdir(parents=True)
    record = {"status": "writer_failed", "response_sha256": sha, "request_sha256": "0" * 64}
    (folder / "transition.json").write_text(json.dumps(record))
    script = Transport(reply(answer(notebook="must not be asked")))
    summary, _ = run(world, 0, script)
    assert summary["outcome"] == "reused" and script.requests == []


def test_a_killed_attempt_is_recorded_and_never_counted_as_completed(world):
    killed = attempt(world)
    killed.mkdir(parents=True)
    (killed / "transport.jsonl").write_text('{"call": 1, "attempt": 1}\n')
    script = Transport(reply(answer()))
    summary, _ = run(world, 0, script)
    assert summary["killed_marked"] == ["001/a01"] and summary["attempt"] == "a02"
    record = read(killed / "attempt.json")
    assert record["outcome"] == "killed_incomplete" and "detected_at" in record
    assert (killed / "transport.jsonl").exists()


def test_the_last_write_sentence_goes_only_into_the_assignment_message_of_its_step(world):
    plain = Transport(reply(answer()))
    first_response = run(world, 0, plain, last_write=False)[1]
    world.receipt(0, "applied", first_response)
    world.add_session(0)
    # A later step of the same chain, with a committed notebook, that is not the last write: no sentence anywhere.
    unmarked = Transport(bad_json(), reply(answer_keeping(MEMORY)))
    run(world, 1, unmarked, memory=MEMORY)
    assert json.loads(unmarked.bodies()[0]["messages"][1]["content"])["notebook"], "the notebook was committed"
    # The same step again with the request marked as the last write: the sentence, in the user message only.
    marked = Transport(bad_json(), reply(answer_keeping(MEMORY, notebook="Another notebook.")))
    run(world, 1, marked, last_write=True, memory=MEMORY)
    for bodies, expected in ((plain.bodies(), False), (unmarked.bodies(), False), (marked.bodies(), True)):
        for body in bodies:
            assert LAST_WRITE not in body["messages"][0]["content"]
            assert (LAST_WRITE in body["messages"][1]["content"]) is expected
            assert all(LAST_WRITE not in message["content"] for message in body["messages"][2:])
    assert marked.bodies()[0]["messages"][1]["content"].endswith("\n\n" + LAST_WRITE)


def test_the_cost_of_every_call_is_summed_and_recorded(world):
    script = Transport(reply("not json", cost=0.5), reply(answer(), cost=0.25))
    summary, _ = run(world, 0, script)
    assert summary["cost"] == 0.75 and read(attempt(world) / "attempt.json")["cost"] == 0.75
    assert ROOT in script.bodies()[0]["messages"][1]["content"]
