"""The memory guard's notices: a fixed harness message that reaches the agent as a mid-conversation system message,
in the attack arm of the variants it names, and never counts as exposure.

Requests go to an in-process transport; nothing leaves the machine.
"""

import json

import pytest
import test_context_eval as base
import test_context_eval_engine as engine_tests
import test_context_eval_runtime as runtime_tests
from pydantic import ValidationError
from test_context_eval_anthropic import SUBMIT, Transport, adapter_config, bash, model_file, native
from test_context_eval_engine import read_log

from sereno.context_eval.agents import AnthropicAdapter
from sereno.context_eval.anthropic_model import native_request
from sereno.context_eval.config import default_registry, validate
from sereno.context_eval.engine import EventLog, InterventionEngine, Runtime
from sereno.context_eval.memory import ROOT
from sereno.context_eval.runner import run_campaign
from sereno.context_eval.schema import ExperimentConfig, Intervention, MemoryConfig, Notice, Session

fixture_world = base.fixture_world
template, factory = engine_tests.template, engine_tests.factory
MARKER = "CI_PLANTED_MARKER"
NOTICE = Notice(id="guard", variants=["noticed"], sessions=["s"], text=f"CI_NOTICE about the note holding {MARKER}.")
PLANT = Intervention(
    id="plant", method="memory", sessions=["s"], path=f"{ROOT}/memory/note.md", text=MARKER, marker=MARKER
)


def runtime_with(tmp_path, factory, engine):
    return Runtime(
        factory(), engine, EventLog(tmp_path / "s.jsonl", "s"), Session(id="s", exposure=True), MemoryConfig(), {}, []
    )


def test_a_later_system_message_keeps_its_place_and_takes_no_breakpoint():
    first = [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "TASK"},
        {"role": "system", "content": "NOTICE", "extra": {"notice_id": "guard"}},
    ]
    request = native_request(first, cache=True)
    assert request["system"] == [{"type": "text", "text": "SYSTEM"}]
    assert request["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "TASK", "cache_control": {"type": "ephemeral"}}]},
        {"role": "system", "content": "NOTICE"},
    ]
    reply = {"role": "assistant", "content": [bash("t1", "ls")]}
    later = [*first, reply, {"role": "tool", "tool_call_id": "t1", "content": "OUT"}]
    turns = native_request(later, cache=True)["messages"]
    assert [t["role"] for t in turns] == ["user", "system", "assistant", "user"]
    assert turns[1] == {"role": "system", "content": "NOTICE"}
    assert json.dumps(turns).count("cache_control") == 1 and "cache_control" in json.dumps(turns[-1])
    # Without a later system message the request is what it was: the last message carries the breakpoint.
    plain = native_request(first[:2], cache=True)
    assert plain["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_the_notice_follows_the_task_turn_once_and_is_not_exposure(tmp_path, factory):
    engine = InterventionEngine([PLANT], default_registry(), notices=[NOTICE])
    engine.session_fires[("plant", "s")] = 1
    runtime = runtime_with(tmp_path, factory, engine)
    transport = Transport([native(bash("t1", "ls")), native(bash("t2", SUBMIT))])
    outcome = AnthropicAdapter(client=transport.client()).run(
        runtime, "task", "", adapter_config(tmp_path), runtime.session
    )
    assert outcome["exit_status"] == "Submitted"
    first, second = transport.bodies
    assert [m["role"] for m in first["messages"]] == ["user", "system"]
    assert first["messages"][1]["content"] == NOTICE.text
    assert [m["role"] for m in second["messages"]] == ["user", "system", "assistant", "user"]
    events = read_log(runtime.log.path)
    notices = [e for e in events if e["kind"] == "notice"]
    assert [(e["notice_id"], e["text"]) for e in notices] == [("guard", NOTICE.text)]
    assert notices[0]["step"] == 0
    contexts = [e for e in events if e["kind"] == "context_sent"]
    # The notice names the planted marker, but the harness's own text is neither exposure nor recall.
    assert [c["matched_interventions"] for c in contexts] == [[], []]
    assert [m["role"] for m in runtime.initial_messages] == ["system", "user", "system"]
    # The same text in an incoming message would count.
    runtime.context_sent([{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": MARKER}])
    assert read_log(runtime.log.path)[-1]["matched_interventions"] == ["plant"]


@pytest.mark.parametrize(
    ("engine", "session"),
    [
        (InterventionEngine([], default_registry(), enabled=False, notices=[NOTICE]), "s"),
        (InterventionEngine([], default_registry(), notices=[NOTICE]), "other"),
    ],
    ids=["clean arm", "other session"],
)
def test_no_notice_outside_its_arm_and_session(tmp_path, factory, engine, session):
    runtime = Runtime(
        factory(),
        engine,
        EventLog(tmp_path / "s.jsonl", session),
        Session(id=session, exposure=True),
        MemoryConfig(),
        {},
        [],
    )
    assert runtime.take_notices() == []


def config_with(**fields) -> dict:
    return {
        "dataset_root": "/data",
        "targets": [{"task_id": "t"}],
        "sessions": [{"id": "s", "exposure": True}],
        "variants": {"noticed": [], "plain": []},
        "arms": ["clean", "attack_carry"],
        **fields,
    }


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"notices": [{"id": "n", "variants": ["missing"], "sessions": ["s"], "text": "x"}]}, "unknown variants"),
        ({"notices": [{"id": "n", "variants": ["noticed"], "sessions": ["missing"], "text": "x"}]}, "unknown sessions"),
        (
            {"arms": ["clean"], "notices": [{"id": "n", "variants": ["noticed"], "sessions": ["s"], "text": "x"}]},
            "attack_carry",
        ),
        ({"notices": [{"id": "n", "variants": ["noticed"], "sessions": ["s"]}]}, "exactly one of text or payload_file"),
        ({"notices": [{"id": "n", "variants": ["noticed"], "sessions": ["s"], "text": ""}]}, "cannot be empty"),
    ],
)
def test_notice_config_errors(fields, message):
    with pytest.raises(ValidationError, match=message):
        ExperimentConfig.model_validate(config_with(**fields))


def test_notices_need_the_anthropic_agent(tmp_path):
    config = ExperimentConfig.model_validate(
        config_with(notices=[{"id": "n", "variants": ["noticed"], "sessions": ["s"], "text": "x"}])
    )
    with pytest.raises(ValueError, match="notices require the anthropic agent"):
        validate(config, default_registry())


def test_a_campaign_sends_the_notice_only_in_the_noticed_attack_session(tmp_path, fixture_world):
    dataset, environments, _, _ = fixture_world
    config = runtime_tests.mini_swe_config(tmp_path, dataset, [])
    config.agent = "anthropic"
    config.model_config_file = model_file(
        tmp_path, "sdk", {"model_name": "claude-haiku-5-5", "model_kwargs": {"max_tokens": 4096}}
    )
    config.arms = ["clean", "attack_carry"]
    config.sessions = [Session(id="task", exposure=True)]
    config.variants = {"noticed": [], "plain": []}
    config.notices = [Notice(id="guard", variants=["noticed"], sessions=["task"], text="CI_NOTICE")]
    config.workers = 1
    transport = Transport([native(bash(f"t{n}", SUBMIT)) for n in range(3)])
    registry = default_registry()
    registry.agents["anthropic"] = AnthropicAdapter(client=transport.client())
    root = tmp_path / "campaign"
    run_campaign(config, root, registry, env_factory=environments, identities=runtime_tests.IDENTITIES)
    noticed = [p for p in root.glob("cases/*noticed*/arms/attack_carry/sessions/*/events.jsonl")]
    assert len(noticed) == 1
    assert [e["notice_id"] for e in read_log(noticed[0]) if e["kind"] == "notice"] == ["guard"]
    others = [p for p in root.glob("**/sessions/*/events.jsonl") if p not in noticed]
    # The clean origin, its copy in each case, and the plain variant's attack session.
    assert len(others) == 4 and not any(e["kind"] == "notice" for p in others for e in read_log(p))
    assert sum(any(m["role"] == "system" for m in body["messages"]) for body in transport.bodies) == 1
