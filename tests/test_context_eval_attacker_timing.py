"""Working-time parity with a deterministic clock: no provider, socket or Docker calls."""

import json
from types import SimpleNamespace

import pytest
import test_context_eval as base
from minisweagent.agents import default
from minisweagent.models.test_models import DeterministicModel, make_output

from sereno.context_eval import agents, engine, environment, runner
from sereno.context_eval.config import default_registry
from sereno.context_eval.dataset import load_task
from sereno.context_eval.schema import ExperimentConfig, Session

fixture_world = base.fixture_world


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    time = monotonic


def timed_session(
    tmp_path,
    fixture_world,
    monkeypatch,
    adapter,
    *,
    attacked,
    failure=None,
    configured=True,
    max_calls=100,
    call_timeout=120,
):
    dataset, factory, _, _ = fixture_world
    clock = Clock()
    for module in (agents, engine, runner, default):
        monkeypatch.setattr(module, "time", clock)
    starts = []
    monkeypatch.setattr(environment, "subprocess", SimpleNamespace(run=lambda args, **kwargs: starts.append(args)))

    class TimedEnvironment(factory):
        def __init__(self, image, wall_seconds):
            super().__init__(image, wall_seconds)
            docker = environment.DockerEnvironment.__new__(environment.DockerEnvironment)
            docker.name = "timing-fixture"
            docker._start(image, wall_seconds)
            self.container_command = starts[-1]

        def execute(self, command):
            if command == "true":
                clock.now += 1.25
            return super().execute(command)

    calls = []

    class SlowAttacker:
        def __init__(self, config, runtime, *args, **kwargs):
            self.runtime = runtime
            self.delay("setup", 17)

        def delay(self, stage, seconds):
            calls.append(stage)
            clock.now += seconds
            if failure == stage:
                raise RuntimeError(f"failed {stage}")

        def advance(self, messages):
            self.delay("advance", 5000)

        def finish(self, messages, summary):
            self.delay("finish", 8000)
            return {"schema_version": "1.0"}

    monkeypatch.setattr(runner, "RunLocalAttacker", SlowAttacker)
    monkeypatch.setattr(
        agents,
        "agent_config",
        lambda *_: {
            "agent": {"system_template": "{{memory_instructions}}", "instance_template": "{{task}}"},
            "model": {},
        },
    )
    for cls in (agents.MiniSweAdapter, agents.AnthropicAdapter):
        monkeypatch.setattr(
            cls,
            "model",
            lambda *_: DeterministicModel(outputs=[make_output("", [{"command": "true"}], cost=0)] * 5),
        )
    session = Session.model_validate({"id": "timed", "script": [{"command": "true"}] * 5})
    config = ExperimentConfig.model_validate(
        {
            "dataset_root": dataset,
            "targets": [{"task_id": "first"}],
            "agent": adapter,
            "sessions": [session],
            "arms": ["clean", "attack_carry"],
            "wall_time_limit_seconds": 2,
            "run_attacker": {
                "command": ["unused"],
                "timeout_seconds": call_timeout,
                "limits": {"max_files_touched": 1, "max_file_bytes": 100},
                "budget": {
                    "max_calls": 100,
                    "max_calls_per_session": max_calls,
                    "max_tokens": 100,
                    "max_seconds": 100,
                    "max_writes": 0,
                },
            }
            if configured
            else None,
        }
    )
    directory = tmp_path / f"{adapter}-{attacked}-{failure}-{configured}"
    _, result = runner.run_session(
        config,
        session,
        load_task(dataset, "first"),
        {"id": "fixture"},
        {},
        engine.InterventionEngine([], default_registry(), enabled=attacked),
        {"scripted": agents.ScriptedAdapter, "mini-swe": agents.MiniSweAdapter, "anthropic": agents.AnthropicAdapter}[
            adapter
        ](),
        directory,
        runner.Budget(20),
        TimedEnvironment,
        lineage_start=True,
    )
    assert result == json.loads((directory / "result.json").read_text())
    return result, calls, directory


@pytest.mark.parametrize("adapter", ["scripted", "mini-swe", "anthropic"])
def test_attacker_latency_preserves_working_time_and_total_duration(tmp_path, fixture_world, monkeypatch, adapter):
    clean, clean_calls, _ = timed_session(tmp_path, fixture_world, monkeypatch, adapter, attacked=False)
    attack, calls, directory = timed_session(tmp_path, fixture_world, monkeypatch, adapter, attacked=True)
    for result in (clean, attack):
        assert (result["status"], result["exit_status"], result["steps"], result["limit"]) == (
            "complete",
            "TimeExceeded",
            2,
            "time",
        )
        assert result["wall_time_limit_seconds"] == 2
    assert clean_calls == []
    assert calls == ["setup", "advance", "advance", "finish"]
    assert clean["attacker_duration_seconds"] == 0
    assert attack["attacker_duration_seconds"] == 18017
    assert clean["duration_seconds"] == 2.5
    assert attack["duration_seconds"] == 18019.5
    if adapter != "scripted":
        trajectory = json.loads((directory / "traj.json").read_text())
        assert trajectory["info"]["config"]["agent"]["wall_time_limit_seconds"] == 2


@pytest.mark.parametrize(("failure", "seconds"), [("setup", 17), ("advance", 5017), ("finish", 18017)])
def test_failed_attacker_time_is_recorded(tmp_path, fixture_world, monkeypatch, failure, seconds):
    result, calls, _ = timed_session(tmp_path, fixture_world, monkeypatch, "scripted", attacked=True, failure=failure)
    assert (result["status"], result["error"]) == ("invalid", f"failed {failure}")
    assert result["attacker_duration_seconds"] == seconds
    assert result["duration_seconds"] >= seconds
    assert calls[-1] == failure


def test_container_sleep_lifetime_is_unchanged_without_an_attacker(tmp_path, fixture_world, monkeypatch):
    configured, calls, _ = timed_session(tmp_path, fixture_world, monkeypatch, "scripted", attacked=False)
    configured_command = fixture_world[2][-1].container_command
    plain, _, _ = timed_session(tmp_path, fixture_world, monkeypatch, "scripted", attacked=False, configured=False)
    plain_command = fixture_world[2][-1].container_command
    assert plain_command[-2:] == ["sleep", "3602"]
    assert calls == []
    assert configured["wall_time_limit_seconds"] == plain["wall_time_limit_seconds"] == 2
    assert configured_command[-2:] == ["sleep", str(int(plain_command[-1]) + 100 * 120 + 300 + 60)]


@pytest.mark.parametrize(("max_calls", "call_timeout", "action_limit"), [(3, 11, 37), (7, 19, 300)])
def test_container_sleep_lifetime_includes_attacker_bound(
    tmp_path, fixture_world, monkeypatch, max_calls, call_timeout, action_limit
):
    monkeypatch.setattr(runner, "action_timeout", lambda _: action_limit)
    result, calls, _ = timed_session(
        tmp_path, fixture_world, monkeypatch, "scripted", attacked=True, max_calls=max_calls, call_timeout=call_timeout
    )
    command = fixture_world[2][-1].container_command
    assert command[-2:] == ["sleep", str(2 + 3600 + max_calls * call_timeout + action_limit + 60)]
    assert result["wall_time_limit_seconds"] == 2
    assert result["action_timeout_seconds"] == action_limit
    assert calls[-1] == "finish"
