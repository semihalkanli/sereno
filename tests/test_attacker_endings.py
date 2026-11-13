"""Neutral loop endings and applied-action accounting without models, sockets or containers."""

import json
from dataclasses import replace

import pytest
import test_context_eval as base
from attacker_world import ScriptedProvider, call, completion, make_config, make_request
from test_context_eval_run_attacker import (
    FakeWriter,
    attacker_settings,
    raw_config,
    records,
    run,
    runtime_for,
)

from sereno.attacker.agent import run_agent
from sereno.attacker.config import LoopSettings
from sereno.attacker.deadline import Deadline
from sereno.attacker.protocol import validate_response
from sereno.attacker.providers.base import UserTurn
from sereno.context_eval import run_attacker
from sereno.context_eval.pairing import campaign_summary
from sereno.context_eval.schema import RunAttacker

fixture_world = base.fixture_world


def ending_request():
    request = make_request()
    request["contract"]["response_keys"].append("loop_ending")
    return request


def test_plain_text_ending_is_reported_at_the_step_bound_without_another_completion(tmp_path):
    config = make_config(tmp_path, agent=LoopSettings(max_steps=2))
    provider = ScriptedProvider(
        completion(text="fixture text", finish="stop"), completion(text="fixture text", finish="stop")
    )
    result = run_agent(config, ending_request(), provider, Deadline(120))
    assert result.finish == "max_steps"
    assert len(provider.turns) == result.steps == 2
    assert any(isinstance(turn, UserTurn) and "No tool call" in turn.text for turn in provider.turns[1])
    assert result.response.get("loop_ending") == "plain_text"
    assert result.response["actions"] == []
    assert "status" not in result.response


def test_a_plain_text_reply_before_the_token_bound_is_reported_as_plain_text(tmp_path):
    request = ending_request()
    request["contract"]["budget"]["max_tokens"] = 15
    provider = ScriptedProvider(completion(text="fixture text", finish="stop"))
    result = run_agent(make_config(tmp_path), request, provider, Deadline(120))
    assert result.finish == "max_tokens" and result.steps == 1
    assert len(provider.turns) == 1
    assert result.response.get("loop_ending") == "plain_text"
    assert result.response["actions"] == []


def test_empty_reply_at_the_step_bound_is_reported_as_empty_reply(tmp_path):
    config = make_config(tmp_path, agent=LoopSettings(max_steps=2))
    provider = ScriptedProvider(completion(finish="stop"), completion(finish="stop"))
    result = run_agent(config, ending_request(), provider, Deadline(120))
    assert result.finish == "max_steps" and result.steps == 2
    assert len(provider.turns) == 2
    assert result.response.get("loop_ending") == "empty_reply"
    assert result.response["actions"] == []
    assert "status" not in result.response


def test_finish_ending_with_no_actions_is_reported(tmp_path):
    provider = ScriptedProvider(completion(call("finish")))
    result = run_agent(make_config(tmp_path), ending_request(), provider, Deadline(120))
    assert result.response.get("loop_ending") == "finish"
    assert result.response["actions"] == []
    assert len(provider.turns) == 1


def test_declined_ending_is_reported_separately_from_plain_text(tmp_path):
    config = make_config(tmp_path)
    config = replace(config, settings=config.settings.model_copy(update={"decline_retries": 0}))
    provider = ScriptedProvider(replace(completion(finish="stop"), declined=True))
    result = run_agent(config, ending_request(), provider, Deadline(120))
    assert result.response.get("loop_ending") == "declined"
    assert result.response["status"] == "declined"
    assert result.response["actions"] == []
    assert result.response["writer_usage"]["declined_attempts"] == 1


@pytest.mark.parametrize("ending", ["max_steps", "max_tokens"])
def test_bound_endings_are_reported_without_becoming_plain_text(tmp_path, ending):
    config = make_config(tmp_path, agent=LoopSettings(max_steps=1))
    request = ending_request()
    if ending == "max_tokens":
        request["contract"]["budget"]["max_tokens"] = 1
        request["contract"]["used"]["tokens"] = 1
    provider = ScriptedProvider(completion(call("list_sessions")))
    result = run_agent(config, request, provider, Deadline(120))
    assert result.response.get("loop_ending") == ending
    assert result.response["actions"] == []


def test_old_response_key_contract_remains_supported(tmp_path):
    result = run_agent(
        make_config(tmp_path), make_request(), ScriptedProvider(completion(call("finish"))), Deadline(120)
    )
    assert result.response["schema_version"] == "1.0"
    assert "loop_ending" not in result.response
    validate_response(result.response, make_request()["contract"])


@pytest.mark.parametrize("ending", [None, "plain_text", "finish", "declined"])
def test_harness_records_explicit_and_legacy_unknown_endings(tmp_path, fixture_world, monkeypatch, ending):
    answer = {} if ending is None else {"loop_ending": ending}
    if ending == "declined":
        answer["status"] = "declined"
    writer = FakeWriter(lambda _: answer)
    monkeypatch.setattr(run_attacker, "run_program", writer)
    _, runtime = runtime_for(tmp_path, fixture_world)
    attacker = run_attacker.RunLocalAttacker(
        RunAttacker.model_validate(attacker_settings()), runtime, "fixture", "first"
    )
    attacker.advance([])
    record = records(tmp_path)[0]
    assert "loop_ending" in record and record["loop_ending"] == ending
    assert record["actions"] == record["applied_attack_actions"] == runtime.applied_attack_actions == 0
    assert record["declined"] == (ending == "declined")
    assert len(writer.requests) == 1


def test_applied_action_count_excludes_no_change_and_session_end_actions(tmp_path, fixture_world, monkeypatch):
    action = {"op": "write", "path": "/root/.claude/projects/fixture/memory/fixture.md", "content": "fixture"}
    monkeypatch.setattr(
        run_attacker, "run_program", FakeWriter(lambda _: {"actions": [action], "loop_ending": "finish"})
    )
    _, runtime = runtime_for(tmp_path, fixture_world)
    attacker = run_attacker.RunLocalAttacker(
        RunAttacker.model_validate(attacker_settings()), runtime, "fixture", "first"
    )
    attacker.advance([])
    attacker.advance([])
    state = attacker.finish([], {})
    audit = records(tmp_path)
    assert [record["status"] for record in audit] == ["applied", "no_change", "not_applied"]
    assert [record["applied_attack_actions"] for record in audit] == [1, 0, 0]
    assert runtime.applied_attack_actions == state["sessions"][-1]["applied_attack_actions"] == 1
    events = [json.loads(line) for line in runtime.log.path.read_text().splitlines()]
    assert [event["loop_ending"] for event in events if event["kind"] == "run_attacker"] == ["finish"] * 3


def test_session_applied_action_counts_reach_results_and_pairing(tmp_path, fixture_world, monkeypatch):
    def answer(request):
        actions = []
        if request["contract"]["kind"] == "in_task" and request["contract"]["stage"] == "exposure":
            actions = [
                {"op": "write", "path": "/root/.claude/projects/fixture/memory/fixture.md", "content": "fixture"}
            ]
        return {"actions": actions, "loop_ending": "finish"}

    monkeypatch.setattr(run_attacker, "run_program", FakeWriter(answer))
    raw = raw_config(fixture_world[0], guard=False)
    raw["repeats"] = 1
    campaign = run(tmp_path, fixture_world, raw)
    sessions = sorted(campaign.glob("cases/*/arms/attack_carry/sessions/*/result.json"))
    assert [json.loads(path.read_text())["applied_attack_actions"] for path in sessions] == [1, 0]
    summary = campaign_summary(campaign, 1)
    chain = summary["chains"]["first--default--r001"]
    assert [session["applied_attack_actions"] for session in chain["sessions"]] == [1, 0]
    assert chain["sessions_without_applied_actions"] == 1
    assert summary["inactivity_by_arm"]["attack_carry"]["sessions_without_applied_actions"] == 1


@pytest.mark.parametrize(("failure", "applied"), [("capture", 2), ("write", 0), ("rollback", None)])
def test_application_failure_preserves_known_or_unknown_action_counts(
    tmp_path, fixture_world, monkeypatch, failure, applied
):
    paths = [f"/root/.claude/projects/fixture/memory/fixture-{index}.md" for index in range(2)]
    actions = [{"op": "write", "path": path, "content": "fixture"} for path in paths]
    monkeypatch.setattr(
        run_attacker, "run_program", FakeWriter(lambda _: {"actions": actions, "loop_ending": "finish"})
    )
    env, runtime = runtime_for(tmp_path, fixture_world)
    write = env.write

    def failing_write(path, text):
        if path == paths[1] and text is not None:
            raise RuntimeError("fixture write")
        if failure == "rollback" and path == paths[0] and text is None:
            raise RuntimeError("fixture rollback")
        return write(path, text)

    def failing_capture(origin):
        raise RuntimeError("fixture capture")

    if failure == "capture":
        monkeypatch.setattr(runtime, "capture_memory", failing_capture)
    else:
        monkeypatch.setattr(env, "write", failing_write)
    attacker = run_attacker.RunLocalAttacker(
        RunAttacker.model_validate(attacker_settings()), runtime, "fixture", "first"
    )
    with pytest.raises(RuntimeError):
        attacker.advance([])
    assert records(tmp_path) == []
    response = json.loads((tmp_path / "run_attacker" / "0001" / "response.json").read_text())
    assert response["loop_ending"] == "finish"
    assert len(response["actions"]) == 2
    assert runtime.applied_attack_actions == applied
    assert (env.read(paths[0]) is not None) == (failure != "write")
