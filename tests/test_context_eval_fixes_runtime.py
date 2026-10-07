"""Runner, adapter and memory behaviour that keeps campaign samples whole and resumes faithful."""

import json
import threading

import pytest
import test_context_eval as base
import test_context_eval_runtime as runtime_tests
import yaml
from pydantic import ValidationError
from test_context_eval_runtime import SUBMIT, output, read, result_of

from sereno.context_eval.config import default_registry, fingerprint
from sereno.context_eval.runner import Budget, NotStarted, run_campaign
from sereno.context_eval.schema import Session

fixture_world = base.fixture_world


def yaml_text(value):
    return yaml.safe_dump(value)


def test_budget_waits_for_a_whole_allowance():
    budget = Budget(3.0)
    assert budget.acquire(2.0) == 2.0
    granted = []
    waiting = threading.Thread(target=lambda: granted.append(budget.acquire(2.0)), daemon=True)
    waiting.start()
    waiting.join(0.2)
    assert waiting.is_alive() and granted == []
    budget.finish(2.0, 0.5)
    waiting.join(5)
    assert granted == [2.0] and budget.reserved == 2.0


def test_budget_never_grants_a_partial_allowance():
    budget = Budget(3.0)
    budget.finish(budget.acquire(2.0), 1.5)
    with pytest.raises(NotStarted, match="campaign cost budget exhausted"):
        budget.acquire(2.0)
    assert budget.acquire(1.5) == 1.5
    exact = Budget(0.3)
    exact.finish(exact.acquire(0.1), 0.1)
    assert exact.acquire(0.2) == 0.2


def test_budget_exhaustion_is_not_hidden_by_float_residue():
    budget = Budget(1.0)
    for amount in [budget.acquire(0.1) for _ in range(3)]:
        budget.finish(amount, 0.0)
    assert budget.reserved != 0.0
    budget.spent = 0.95
    outcome = []

    def acquire():
        try:
            budget.acquire(0.1)
        except NotStarted as error:
            outcome.append(str(error))

    waiting = threading.Thread(target=acquire, daemon=True)
    waiting.start()
    waiting.join(5)
    assert outcome == ["campaign cost budget exhausted"]


def test_session_cap_cannot_exceed_the_campaign_cap(fixture_world):
    raw = base.campaign_config(fixture_world[0]).model_dump()
    with pytest.raises(ValidationError, match="cannot exceed campaign_cost_limit_usd"):
        type(base.campaign_config(fixture_world[0])).model_validate(
            raw | {"cost_limit_usd": 3.0, "campaign_cost_limit_usd": 2.0}
        )


def test_an_unaffordable_session_is_missing_and_resumable(tmp_path, fixture_world):
    config = runtime_tests.mini_swe_config(tmp_path, fixture_world[0], [output(SUBMIT, cost=0.3)])
    config.sessions = [Session(id="first-task"), Session(id="second-task"), Session(id="third-task")]
    config.cost_limit_usd, config.campaign_cost_limit_usd = 0.5, 0.7
    root, summary = base.run_fixture(tmp_path, fixture_world, config)
    sessions = root / "clean" / "first--r001" / "sessions"
    first = result_of(sessions / "001-first-task")
    assert (first["status"], first["cost_limit_usd"], first["cost_usd"]) == ("complete", 0.5, pytest.approx(0.3))
    assert sorted(p.name for p in sessions.iterdir()) == ["001-first-task"]
    rows = {row["session"]: row["status"] for row in summary["sessions"]}
    assert rows == {"first-task": "complete", "second-task": "missing", "third-task": "missing"}

    # Each resume spends a new campaign budget, which here affords one more session.
    for name in ("002-second-task", "003-third-task"):
        runtime_tests.resume(fixture_world, config, root)
        assert result_of(sessions / name)["status"] == "complete"
    assert read(root / "campaign.json")["resumes"][0]["sessions"] == [
        "cases/first--canary--r001/arms/clean/sessions/002-second-task",
        "clean/first--r001/sessions/002-second-task",
    ]
    assert read(sessions / "002-second-task" / "memory_start.json") == read(
        sessions / "001-first-task" / "memory_end.json"
    )


def test_a_failed_model_call_after_priced_calls_stops_the_campaign(tmp_path, fixture_world):
    # The deterministic model has no second output, so its second query raises after a priced first call.
    config = runtime_tests.mini_swe_config(tmp_path, fixture_world[0], [output("cat README.md", cost=0.4)])
    config.arms = ["clean", "attack_carry"]
    config.sessions = [Session(id="exposure", exposure=True), Session(id="probe")]
    root, summary = base.run_fixture(tmp_path, fixture_world, config)
    first = result_of(root / "clean" / "first--r001" / "sessions" / "001-exposure")
    assert (first["status"], first["steps"], first["cost_usd"]) == ("invalid", 2, pytest.approx(0.4))
    assert first["cost_status"] == "unknown" and read(root / "campaign.json")["unknown_cost"] is True
    rows = {(row["arm"], row["session"]): row["status"] for row in summary["sessions"]}
    assert rows[("attack_carry", "exposure")] == "missing"


def test_a_failed_model_call_after_a_reply_with_a_line_separator_still_settles_its_cost(tmp_path, fixture_world):
    # The event log keeps U+2028 raw, so the cost settlement must not split the log on it.
    config = runtime_tests.mini_swe_config(tmp_path, fixture_world[0], [output("printf 'a\u2028b'", cost=0.4)])
    config.arms = ["clean"]
    config.sessions = [Session(id="exposure", exposure=True), Session(id="probe")]
    root, _ = base.run_fixture(tmp_path, fixture_world, config)
    first = result_of(root / "clean" / "first--r001" / "sessions" / "001-exposure")
    assert (first["steps"], first["cost_usd"], first["cost_status"]) == (2, pytest.approx(0.4), "unknown")
    campaign = read(root / "campaign.json")
    assert campaign["cost_usd"] == pytest.approx(0.4) and campaign["unknown_cost"] is True


def test_copies_of_sessions_that_never_started_are_missing(tmp_path, fixture_world):
    config = runtime_tests.mini_swe_config(tmp_path, fixture_world[0], [output(SUBMIT, cost=0.3)])
    config.arms = ["clean", "attack_carry", "attack_reset"]
    config.sessions = [Session(id="exposure", exposure=True), Session(id="probe")]
    config.cost_limit_usd, config.campaign_cost_limit_usd = 0.5, 0.7
    _, summary = base.run_fixture(tmp_path, fixture_world, config)
    rows = {(row["arm"], row["session"]): row["status"] for row in summary["sessions"]}
    assert rows == {
        ("clean", "exposure"): "complete",
        ("clean", "probe"): "missing",
        ("attack_carry", "exposure"): "missing",
        ("attack_carry", "probe"): "missing",
        ("attack_reset", "exposure"): "missing",
        ("attack_reset", "probe"): "missing",
    }


ZERO_COST_USAGE = {
    "prompt_tokens": 26859,
    "completion_tokens": 151,
    "total_tokens": 27010,
    "cost": 0,
    "is_byok": False,
    "prompt_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0, "audio_tokens": 0, "video_tokens": 0},
    "cost_details": {
        "upstream_inference_cost": 0.00410435,
        "upstream_inference_prompt_cost": 0.00402885,
        "upstream_inference_completions_cost": 7.55e-05,
    },
}


def openrouter_reply(monkeypatch, usage):
    import httpx

    from sereno.context_eval.models import TrackedOpenRouterModel

    tool_call = {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": '{"command": "ls"}'}}
    response = {
        "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tool_call]}}],
        "usage": usage,
    }
    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: httpx.Response(200, json=response))
    monkeypatch.setenv("OPENROUTER_API_KEY", "fixture-not-a-key")
    return TrackedOpenRouterModel(model_name="fixture-model", cost_tracking="default").query(
        [{"role": "user", "content": "fixture"}]
    )


def test_zero_billed_cost_falls_back_to_the_upstream_cost(monkeypatch):
    extra = openrouter_reply(monkeypatch, ZERO_COST_USAGE)["extra"]
    assert (extra["cost"], extra["cost_source"]) == (0.00410435, "upstream")
    assert extra["actions"][0]["command"] == "ls"
    billed = openrouter_reply(monkeypatch, ZERO_COST_USAGE | {"cost": 0.005})["extra"]
    assert billed["cost"] == 0.005 and "cost_source" not in billed


@pytest.mark.parametrize(("cost", "cost_details"), [(0, None), (0, {}), (None, {"upstream_inference_cost": 0})])
def test_a_response_without_any_cost_still_fails(monkeypatch, cost, cost_details):
    usage = {key: value for key, value in ZERO_COST_USAGE.items() if key != "cost_details"} | {"cost": cost}
    if cost_details is not None:
        usage["cost_details"] = cost_details
    with pytest.raises(RuntimeError, match="No valid cost information"):
        openrouter_reply(monkeypatch, usage)


def two_task_campaign(tmp_path, fixture_world):
    config = runtime_tests.mini_swe_config(tmp_path, fixture_world[0], [output(SUBMIT)])
    config.sessions = [Session(id="first-task"), Session(id="second-task")]
    return config


def system_message(directory):
    return runtime_tests.contents(runtime_tests.opening(directory))[0]


def test_sessions_run_with_the_frozen_agent_configuration(tmp_path, fixture_world, monkeypatch):
    import minisweagent.config

    from sereno.context_eval.agents import mini_swe_config

    dataset, factory, created, _ = fixture_world
    config = two_task_campaign(tmp_path, fixture_world)
    expected = mini_swe_config(config.model_config_file)
    builtin = minisweagent.config.builtin_config_dir
    changed = tmp_path / "builtin"
    changed.mkdir()
    agent = yaml.safe_load((builtin / "mini.yaml").read_text())
    agent["agent"]["system_template"] = "CI_CHANGED_TEMPLATE"
    (changed / "mini.yaml").write_text(yaml.safe_dump(agent))

    class ChangesMiniSwe(factory):
        def __init__(self, image, wall_seconds):
            super().__init__(image, wall_seconds)
            # mini-swe's own mini.yaml changes on disk once the first session is under way.
            monkeypatch.setattr(minisweagent.config, "builtin_config_dir", changed)

    root = tmp_path / "campaign"
    run_campaign(config, root, default_registry(), env_factory=ChangesMiniSwe, identities=runtime_tests.IDENTITIES)
    frozen = root / "mini-swe-config.yaml"
    assert yaml.safe_load(frozen.read_text()) == expected
    assert read(root / "manifest.json")["agent_config_sha256"] == fingerprint(frozen.read_text())
    sessions = root / "clean" / "first--r001" / "sessions"
    first, second = (system_message(sessions / name) for name in ("001-first-task", "002-second-task"))
    assert first == second and "CI_CHANGED_TEMPLATE" not in second
    runtime_tests.invalidate(sessions / "002-second-task")
    runtime_tests.resume((dataset, factory, created, None), config, root)
    assert system_message(sessions / "002-second-task") == first


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("versions", {"sereno": "0.0.1", "mini-swe-agent": "2.4.6"}),
        ("plugins", [{"module": "fixture_plugin", "source_sha256": "0" * 64}]),
    ],
)
def test_resume_refuses_other_code(tmp_path, fixture_world, key, value):
    config = two_task_campaign(tmp_path, fixture_world)
    root, _ = base.run_fixture(tmp_path, fixture_world, config)
    manifest = read(root / "manifest.json")
    (root / "manifest.json").write_text(json.dumps(manifest | {key: value}))
    with pytest.raises(ValueError, match=f"cannot resume: {key} changed"):
        runtime_tests.resume(fixture_world, config, root)


def test_resume_refuses_a_changed_frozen_agent_configuration(tmp_path, fixture_world):
    config = two_task_campaign(tmp_path, fixture_world)
    root, _ = base.run_fixture(tmp_path, fixture_world, config)
    with (root / "mini-swe-config.yaml").open("a") as stream:
        stream.write("# edited\n")
    with pytest.raises(ValueError, match="frozen mini-swe configuration changed"):
        runtime_tests.resume(fixture_world, config, root)


def test_a_campaign_started_before_freezing_freezes_on_resume(tmp_path, fixture_world):
    from sereno.context_eval.agents import mini_swe_config

    config = two_task_campaign(tmp_path, fixture_world)
    root, _ = base.run_fixture(tmp_path, fixture_world, config)
    manifest = read(root / "manifest.json")
    del manifest["agent_config_sha256"]
    (root / "manifest.json").write_text(json.dumps(manifest))
    (root / "mini-swe-config.yaml").unlink()
    second = root / "clean" / "first--r001" / "sessions" / "002-second-task"
    runtime_tests.invalidate(second)
    _, runs = runtime_tests.resume(fixture_world, config, root)
    assert runs == 1 and result_of(second)["status"] == "complete"
    frozen = root / "mini-swe-config.yaml"
    assert frozen.read_text() == yaml_text(mini_swe_config(root / "model-config.yaml"))
    resumed = read(root / "manifest.json")
    assert resumed["agent_config_sha256"] == fingerprint(frozen.read_text())
    assert "agent_config_frozen_on_resume" in resumed
    assert {k: v for k, v in resumed.items() if not k.startswith("agent_config")} == manifest


def test_a_truncated_branch_file_is_an_unfinished_copy(tmp_path, fixture_world):
    from sereno.context_eval.runner import complete

    root, _ = base.run_fixture(tmp_path, fixture_world, base.campaign_config(fixture_world[0]))
    copy = base.session_dir(root, "clean", "002-probe")
    assert complete(copy, copy=True)
    assert not list(copy.glob(".*partial"))
    first_run = result_of(copy)["run_id"]
    (copy / "branch.json").write_text('{"shared_from": "clean/fi')
    assert not complete(copy, copy=True) and complete(copy)
    _, runs = runtime_tests.resume(fixture_world, base.campaign_config(fixture_world[0]), root)
    assert runs == 0 and complete(copy, copy=True) and result_of(copy)["run_id"] == first_run


def test_an_oversized_first_index_line_is_cut_not_dropped():
    from sereno.context_eval.memory import INDEX, reminder

    index = "-" + "ğ" * 13000 + "\n- [b](b.md) - short\n"
    text = reminder({INDEX: index})
    loaded = text.split("(the agent's memory index):\n\n", 1)[1].split("\n\nWARNING", 1)[0]
    # 25,000 bytes end inside a two-byte character, which is left out whole.
    assert len(loaded.encode()) == 24999 and loaded == "-" + "ğ" * 12499
    assert f"WARNING: /memories/MEMORY.md is 2 lines and {len(index.encode())} bytes" in text
    exact = "a" * 25000
    assert exact + "\n</system-reminder>" in reminder({INDEX: exact}) and "WARNING" not in reminder({INDEX: exact})


@pytest.mark.parametrize(("agent", "seconds", "has_timeout"), [("scripted", 300, None), ("mini-swe", 42, True)])
def test_the_action_limit_is_set_by_the_runner_and_recorded(tmp_path, fixture_world, agent, seconds, has_timeout):
    from sereno.context_eval.metrics import events_at

    dataset, factory, created, _ = fixture_world
    if agent == "mini-swe":
        config = runtime_tests.mini_swe_config(tmp_path, dataset, [output(SUBMIT)])
        model = yaml.safe_load(config.model_config_file.read_text()) | {"environment": {"timeout": 42}}
        config.model_config_file.write_text(yaml.safe_dump(model))
        directory = tmp_path / "campaign" / "clean" / "first--r001" / "sessions" / "001-task"

        class WithTimeout(factory):
            has_timeout = True

        world = (dataset, WithTimeout, created, None)
    else:
        config = base.campaign_config(dataset)
        config.arms = ["clean"]
        directory = tmp_path / "campaign" / "clean" / "first--r001" / "sessions" / "001-exposure"
        world = fixture_world
    base.run_fixture(tmp_path, world, config)
    result = result_of(directory)
    assert (result["action_timeout_seconds"], result["has_timeout"]) == (seconds, has_timeout)
    assert created[0].action_timeout == seconds
    start = next(e for e in events_at(directory) if e["kind"] == "session_start")
    assert (start["action_timeout_seconds"], start["has_timeout"]) == (seconds, has_timeout)


NOTE = "/memories/note.md"
OVERSIZED = "python3 -c \"open('/memories/big.md', 'w').write('x' * 30000)\""


def memory_campaign(dataset, first_script):
    config = base.campaign_config(dataset)
    config.arms, config.variants, config.checks = ["clean"], {"canary": []}, []
    config.memory.max_bytes = 25000
    config.sessions = [
        Session(id="first-task", script=first_script),
        Session(id="second-task", script=[{"command": f"cat {NOTE}"}]),
    ]
    return config


def test_an_agent_memory_violation_ends_the_task_as_an_outcome(tmp_path, fixture_world):
    from sereno.context_eval.metrics import events_at

    config = memory_campaign(
        fixture_world[0],
        [
            {"command": base.write_command(NOTE, "CI_NOTE")},
            {"command": base.write_command("/app/change.txt", "edited")},
            {"command": OVERSIZED},
            {"command": "echo never-run"},
        ],
    )
    root, summary = base.run_fixture(tmp_path, fixture_world, config)
    sessions = root / "clean" / "first--r001" / "sessions"
    first, second = sessions / "001-first-task", sessions / "002-second-task"
    result = result_of(first)
    assert (result["status"], result["exit_status"], result["limit"], result["steps"]) == (
        "complete",
        "MemoryViolation",
        "memory",
        3,
    )
    assert "exceeds limits" in result["memory_error"] and "error" not in result
    assert result["patch_status"] == "ready" and "change.txt" in (first / "model.patch").read_text()
    assert read(first / "memory_end.json") == {NOTE: "CI_NOTE"}
    assert [e["kind"] for e in events_at(first)].count("memory_violation") == 1
    assert "never-run" not in (first / "events.jsonl").read_text()
    assert read(second / "memory_start.json") == {NOTE: "CI_NOTE"}
    assert result_of(second)["status"] == "complete"
    assert [row["status"] for row in summary["sessions"]] == ["complete", "complete"]


def test_an_infrastructure_snapshot_failure_stays_invalid(tmp_path, fixture_world):
    dataset, factory, created, _ = fixture_world

    class BrokenBridge(factory):
        def snapshot_memory(self, max_files, max_bytes):
            if (self.app / "change.txt").exists():
                raise RuntimeError("bridge memory failed: Error response from daemon: container is not running")
            return super().snapshot_memory(max_files, max_bytes)

    config = memory_campaign(dataset, [{"command": base.write_command("/app/change.txt", "edited")}])
    root, _ = base.run_fixture(tmp_path, (dataset, BrokenBridge, created, None), config)
    sessions = root / "clean" / "first--r001" / "sessions"
    result = result_of(sessions / "001-first-task")
    assert (result["status"], result["exit_status"], result["limit"]) == ("invalid", "RuntimeError", None)
    assert result_of(sessions / "002-second-task")["error"] == "preceding session invalid"


@pytest.mark.parametrize(
    ("message", "agent"),
    [
        ("bridge memory failed: ValueError: memory snapshot exceeds limits", True),
        ("bridge memory failed: ValueError: non-UTF-8 file /memories/a.md", True),
        ("bridge memory failed: ValueError: symlink memory entry", True),
        ("bridge memory failed: ValueError: noncanonical or reserved path", True),
        ("bridge memory failed: exit status 137", False),
        ("bridge memory failed: Error response from daemon: No such container", False),
    ],
)
def test_snapshot_failures_caused_by_memory_content(message, agent):
    from sereno.context_eval.memory import agent_violation

    assert agent_violation(RuntimeError(message)) is agent
    assert agent_violation(ValueError("memory snapshot exceeds limits"))


def test_mini_swe_memory_violation_keeps_its_trajectory(tmp_path, fixture_world):
    config = runtime_tests.mini_swe_config(
        tmp_path, fixture_world[0], [output(base.write_command(NOTE, "CI_NOTE")), output(OVERSIZED)], max_bytes=25000
    )
    root, _ = base.run_fixture(tmp_path, fixture_world, config)
    directory = root / "clean" / "first--r001" / "sessions" / "001-task"
    result = result_of(directory)
    assert (result["status"], result["limit"], result["steps"], result["cost_usd"]) == (
        "complete",
        "memory",
        2,
        pytest.approx(0.2),
    )
    assert read(directory / "traj.json")["info"]["exit_status"] == "MemoryViolation"
    assert read(directory / "memory_end.json") == {NOTE: "CI_NOTE"}
    assert read(root / "campaign.json")["unknown_cost"] is False


class RecordingStrategy:
    """A plugin strategy that draws from the intervention RNG on every eligible action and never fires."""

    def __init__(self):
        self.draws = []

    def eligible(self, event, total_fires, session_fires, context):
        self.draws.append((context["session_id"], context["rng"].random()))
        return False


def test_resume_continues_the_intervention_rng(tmp_path, fixture_world):
    from sereno.context_eval.schema import Intervention

    dataset, factory, _, _ = fixture_world
    config = base.campaign_config(dataset)
    config.arms = ["attack_carry"]
    actions = [{"command": "echo one"}, {"command": "echo two"}]
    config.sessions = [Session(id=name, exposure=True, script=actions) for name in ("first", "second")]
    config.checks = []
    config.variants = {
        "canary": [
            Intervention(
                id="drawn",
                method="file",
                strategy="draw",
                phase="before_action",
                sessions=["first", "second"],
                path="/app/README.md",
                text="CI_DRAWN",
            )
        ]
    }

    def campaign(resume):
        registry, strategy = default_registry(), RecordingStrategy()
        registry.register_strategy("draw", strategy)
        run_campaign(
            config,
            tmp_path / "campaign",
            registry,
            env_factory=factory,
            identities=runtime_tests.IDENTITIES,
            resume=resume,
        )
        return strategy.draws

    draws = campaign(False)
    assert [session for session, _ in draws] == ["first", "first", "second", "second"]
    arm = tmp_path / "campaign" / "cases" / "first--canary--r001" / "arms" / "attack_carry" / "sessions"
    runtime_tests.invalidate(arm / "002-second")
    assert campaign(True) == draws[2:]


def test_an_error_body_with_http_200_is_retried(monkeypatch):
    import time

    import httpx

    from sereno.context_eval.models import TrackedOpenRouterModel

    tool_call = {"id": "c1", "type": "function", "function": {"name": "bash", "arguments": '{"command": "ls"}'}}
    replies = [
        {
            "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [tool_call]}}],
            "usage": {"cost": 0.005},
        },
        {"id": "gen-x", "error": {"code": 502, "message": "fixture provider error"}},
    ]
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: httpx.Response(200, json=replies.pop()))
    reply = TrackedOpenRouterModel(model_name="fixture-model", cost_tracking="default").query(
        [{"role": "user", "content": "fixture"}]
    )
    assert not replies and reply["extra"]["cost"] == 0.005
