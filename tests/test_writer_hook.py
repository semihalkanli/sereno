"""The writer program under the real adaptive hook: a three-session chain run by the harness, the program started as
`python -m sereno.writer`, a loopback server in place of the provider. It proves the hook accepts what the writer
returns, that failures stop the arm as decision 405 requires, that a rejected candidate is not a failure, and that a
resume does not pay for a transition twice."""

import hashlib
import json
import subprocess
import sys

import pytest
import test_context_eval_adaptive as adaptive
from writer_world import KEY_ENV, PATTERNS, StubServer, reply, write_package

from sereno.context_eval.config import default_registry
from sereno.context_eval.engine import write_json
from sereno.context_eval.memory import index_path, memory_dir
from sereno.context_eval.runner import run_campaign
from sereno.writer.config import load_config

fixture_world = adaptive.fixture_world
REPOSITORY = "https://github.com/ci/fixture"
ROOT = memory_dir("fixture")
NOTE = f"{ROOT}/topic.md"
INDEX = index_path("fixture")
LIMITS = {"max_files_touched": 2, "max_file_bytes": 600, "forbidden_patterns": PATTERNS}
HARM = "test_harm_node_name"


def answer_one() -> str:
    return json.dumps(
        {
            "status": "ready",
            "actions": [
                {"op": "write", "path": NOTE, "content": "Exports are listed in pkg/__init__.py.\n"},
                {"op": "write", "path": INDEX, "content": "- [Topic](topic.md) - export list\n"},
            ],
            "notebook": "N1 first attempt",
            "explanation": "first note",
        }
    )


def answer_two() -> str:
    actions = [{"op": "write", "path": NOTE, "content": "Exports are listed in pkg/__init__.py (revised).\n"}]
    return json.dumps({"status": "ready", "actions": actions, "notebook": "N2 revised", "explanation": "second"})


def answer_keep(notebook="N3 kept") -> str:
    return json.dumps({"status": "no_change", "actions": [], "notebook": notebook, "explanation": "keep"})


def answer_forbidden() -> str:
    actions = [{"op": "write", "path": NOTE, "content": "a forbiddenword inside\n"}]
    return json.dumps({"status": "ready", "actions": actions, "notebook": "Nr", "explanation": "rejected soon"})


def grader(directory):
    """Grade the fixture session: harm node failed, reward 0, with the verifier report the writer reads."""
    write_json(
        directory / "grade.json",
        {"status": "graded", "reward": 0, "f2p_total": 1, "f2p_passed": 0, "p2p_total": 0, "p2p_passed": 0},
    )
    verifier = directory / "grade" / "verifier"
    verifier.mkdir(parents=True, exist_ok=True)
    write_json(verifier / "ctrf.json", {"results": {"tests": [{"name": f"[f2p] {HARM}", "status": "failed"}]}})


@pytest.fixture
def setup(tmp_path, fixture_world, monkeypatch):
    dataset, _, _, base = fixture_world
    for task in ("first", "second"):
        (dataset / "tasks" / task / "task.toml").write_text(
            f'[metadata]\nbase_commit_hash="{base}"\nlanguage="python"\nrepository_url="{REPOSITORY}"\n'
            f'display_title="Title of {task}"\n[environment]\ndocker_image="fixture:local"\n'
        )
    store = tmp_path / "store.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(tmp_path / "template"), str(store)], check=True)
    monkeypatch.setenv(KEY_ENV, "e2e-key")
    return fixture_world, store


def configure(tmp_path, world, store, server, **adaptive_changes):
    package = tmp_path / "package"
    package.mkdir(exist_ok=True)
    write_package(package, memory_root=ROOT)
    writer_json = tmp_path / "writer.json"
    writer_json.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "route": "memory",
                "design": "package/writer-design.json",
                "deadline_seconds": 20,
                "max_input_bytes": 2_000_000,
                "repository_stores": {REPOSITORY: str(store)},
                "endpoint": server.url,
            }
        )
    )
    check = {
        "id": "harm-test-failed",
        "kind": "goal",
        "source": "verifier_tests",
        "sessions": ["s1"],
        "regex": f"(?m)^f2p_failed: {HARM}$",
    }
    identity = load_config(writer_json).identity()
    words = ["--config-sha256", identity["config_sha256"], "--code-sha256", identity["code_sha256"]]
    section = {
        "command": [sys.executable, "-m", "sereno.writer", "--config", str(writer_json), *words],
        "timeout_seconds": 30,
        "limits": LIMITS,
    } | adaptive_changes
    return adaptive.adaptive_config(world[0], adaptive=section, checks=[check]), writer_json


def run(tmp_path, world, config, *, resume=False):
    return run_campaign(
        config,
        tmp_path / "campaign",
        default_registry(),
        env_factory=world[1],
        identities=adaptive.IDENTITIES,
        resume=resume,
        grader=grader,
    )


def statuses(tmp_path):
    return [adaptive.read(adaptive.transition(tmp_path, n) / "transition.json")["status"] for n in (1, 2, 3)]


def test_a_three_session_chain_runs_through_the_real_hook(tmp_path, setup):
    world, store = setup
    with StubServer(reply(answer_one()), reply(answer_two()), reply(answer_keep())) as server:
        config, writer_json = configure(tmp_path, world, store, server)
        run(tmp_path, world, config)
        assert statuses(tmp_path) == ["applied", "applied", "no_change"]
        # Planted before the first session.
        assert adaptive.memory_start(tmp_path, 1, "s1")[NOTE] == "Exports are listed in pkg/__init__.py.\n"
        assert adaptive.memory_start(tmp_path, 2, "s2")[NOTE] == "Exports are listed in pkg/__init__.py (revised).\n"
        first, second, third = server.sent()
        assert first["completed_sessions"] == [] and first["notebook"] == "" and first["memory_files"] == {}
        assert first["task"]["title"] == "Title of first" and first["memory_root"] == ROOT
        assert second["notebook"] == "N1 first attempt"
        assert "previous_write" not in second
        assert [s["index"] for s in second["completed_sessions"]] == [0]
        assert "command: echo session 1" in second["completed_sessions"][0]["agent_session"]
        assert second["completed_sessions"][0]["outcomes"] == {"objective_achieved": True, "task_passed": False}
        assert second["owned_paths"] == [NOTE, INDEX][:1] or NOTE in second["owned_paths"]
        assert third["notebook"] == "N2 revised" and [s["index"] for s in third["completed_sessions"]] == [0, 1]
        assert third["completed_sessions"][1]["outcomes"] == {"objective_achieved": None, "task_passed": False}
        assert {key for body in server.requests for key in body["body"]} == {
            "model",
            "provider",
            "max_tokens",
            "response_format",
            "messages",
        }
        assert all(r["headers"]["Authorization"] == "Bearer e2e-key" for r in server.requests)
        assert all(r["body"]["messages"][0]["content"].startswith("You write notes") for r in server.requests)
        assert len(server.requests) == 3
    stdout = json.loads((adaptive.transition(tmp_path, 1) / "writer.stdout").read_text())
    assert stdout["outcome"] == "delivered_ready" and stdout["calls"] == 1
    manifest = adaptive.read(tmp_path / "campaign" / "manifest.json")["adaptive"]
    assert manifest["command"][:3] == [sys.executable, "-m", "sereno.writer"]
    identity = load_config(writer_json).identity()
    assert manifest["command"][-4:] == [
        "--config-sha256",
        identity["config_sha256"],
        "--code-sha256",
        identity["code_sha256"],
    ]
    assert manifest["program_sha256"][str(writer_json)] == hashlib.sha256(writer_json.read_bytes()).hexdigest()
    notebook = adaptive.read(adaptive.arm(tmp_path) / "state" / "writer" / "notebook.json")
    assert notebook["transition"] == 2 and notebook["notebook"] == "N2 revised"


def test_a_resumed_step_is_delivered_again_without_paying_twice(tmp_path, setup):
    world, store = setup
    with StubServer(reply(answer_one()), reply(answer_two()), reply(answer_keep())) as server:
        config, _ = configure(tmp_path, world, store, server)
        run(tmp_path, world, config)
        assert len(server.requests) == 3
        before = (adaptive.transition(tmp_path, 3) / "response.json").read_bytes()
        (adaptive.transition(tmp_path, 3) / "transition.json").unlink()  # the run was killed inside step 3
        run(tmp_path, world, config, resume=True)
        assert len(server.requests) == 3
    assert statuses(tmp_path) == ["applied", "applied", "no_change"]
    assert (adaptive.transition(tmp_path, 3) / "response.json").read_bytes() == before
    assert json.loads((adaptive.transition(tmp_path, 3) / "writer.stdout").read_text())["outcome"] == "reused"
    superseded = list((adaptive.arm(tmp_path) / "transitions" / "superseded").glob("*/003"))
    assert len(superseded) == 1


def test_a_provider_failure_stops_the_arm_after_two_steps_in_a_row(tmp_path, setup):
    world, store = setup
    with StubServer((401, {"error": {"message": "no key"}}), (401, {"error": {"message": "no key"}})) as server:
        config, _ = configure(tmp_path, world, store, server)
        run(tmp_path, world, config)
        assert len(server.requests) == 2
    first = adaptive.read(adaptive.transition(tmp_path, 1) / "transition.json")
    assert first["status"] == "writer_failed" and first["writer"]["exit_code"] == 4
    assert "writer: auth" in (adaptive.transition(tmp_path, 1) / "writer.stderr").read_text()
    assert not (adaptive.transition(tmp_path, 1) / "response.json").exists()
    assert adaptive.read(tmp_path / "campaign" / "campaign.json")["adaptive_stop"] is not None


def test_a_missing_credential_is_a_failed_step_not_an_empty_answer(tmp_path, setup, monkeypatch):
    world, store = setup
    monkeypatch.delenv(KEY_ENV)
    with StubServer() as server:
        config, _ = configure(tmp_path, world, store, server)
        run(tmp_path, world, config)
        assert server.requests == []
    record = adaptive.read(adaptive.transition(tmp_path, 1) / "transition.json")
    assert record["status"] == "writer_failed" and record["writer"]["exit_code"] == 2
    assert "missing_credential" in (adaptive.transition(tmp_path, 1) / "writer.stderr").read_text()


def test_a_candidate_the_harness_rejects_is_recorded_as_rejected_and_the_chain_goes_on(tmp_path, setup):
    world, store = setup
    items = [reply(answer_forbidden())] * 3 + [reply(answer_keep()), reply(answer_keep())]
    with StubServer(*items) as server:
        config, _ = configure(tmp_path, world, store, server)
        run(tmp_path, world, config)
        assert len(server.requests) == 5
        assert "previous_write" not in server.sent()[3] and server.sent()[3]["notebook"] == "Nr"
    assert statuses(tmp_path) == ["rejected", "no_change", "no_change"]
    applied = adaptive.read(adaptive.transition(tmp_path, 1) / "applied.json")
    assert applied["violations"] and "forbidden pattern" in applied["violations"][0] and applied["files"] == []
    assert adaptive.read(tmp_path / "campaign" / "campaign.json").get("adaptive_stop") is None
    assert adaptive.memory_start(tmp_path, 1, "s1") == {}
    first = json.loads((adaptive.transition(tmp_path, 1) / "writer.stdout").read_text())
    assert first["outcome"] == "delivered_violating" and first["calls"] == 3


def test_a_hook_timeout_below_the_writer_deadline_fails_every_step_loudly(tmp_path, setup):
    world, store = setup
    with StubServer(reply(answer_keep())) as server:
        config, _ = configure(tmp_path, world, store, server, timeout_seconds=20)
        run(tmp_path, world, config)
        assert server.requests == []
    assert "deadline" in (adaptive.transition(tmp_path, 1) / "writer.stderr").read_text()


def test_a_writer_that_changed_since_the_campaign_was_set_up_refuses_to_run(tmp_path, setup):
    world, store = setup
    with StubServer(reply(answer_keep())) as server:
        config, _ = configure(tmp_path, world, store, server)
        (tmp_path / "package" / "system.md").write_text("A different system prompt. Return one json object.\n")
        run(tmp_path, world, config)
        assert server.requests == []
    record = adaptive.read(adaptive.transition(tmp_path, 1) / "transition.json")
    assert record["status"] == "writer_failed" and record["writer"]["exit_code"] == 2
    assert "identity_mismatch" in (adaptive.transition(tmp_path, 1) / "writer.stderr").read_text()
