"""The pre-run dry check of replace interventions: seed memory, the files a task image ships, and what it cannot
know in advance."""

import json
import os

import pytest
import test_context_eval as base
import test_context_eval_fixes_engine as engine_fixes
import yaml

from sereno.context_eval.cli import main
from sereno.context_eval.config import default_registry
from sereno.context_eval.environment import DockerEnvironment
from sereno.context_eval.runner import dry_check, run_campaign
from sereno.context_eval.schema import ExperimentConfig

fixture_world = base.fixture_world
IDENTITIES = {"first": {"id": "fixture"}, "second": {"id": "fixture"}}
NOTE = "/root/.claude/projects/fixture/memory/note.md"


def replace(identifier, method, path, old_text, **fields):
    return {
        "id": identifier,
        "method": method,
        "sessions": ["exposure"],
        "path": path,
        "operation": "replace",
        "old_text": old_text,
        "text": "CI_DRY",
        **fields,
    }


def config_with(dataset, events, **fields):
    """The fixture campaign with one variant per intervention, or with the given variants."""
    config = base.campaign_config(dataset).model_dump(mode="json")
    config["variants"] = events if isinstance(events, dict) else {event["id"]: [event] for event in events}
    return ExperimentConfig.model_validate(config | fields)


def statuses(items):
    return {item["intervention"]: (item["status"], item["reason"]) for item in items}


def test_each_replace_is_checked_where_it_can_be(fixture_world):
    dataset, factory, created, _ = fixture_world
    events = [
        replace("file", "file", "/app/README.md", "documentation"),
        replace("absent", "file", "/app/README.md", "CI_ABSENT"),
        replace("twice", "file", "/app/README.md", "o"),
        replace("unshipped", "file", "/app/missing.md", "x"),
        replace("seeded", "memory", NOTE, "seed line"),
        replace("unseeded", "memory", "/root/.claude/projects/fixture/memory/later.md", "x", phase="session_end"),
        replace("first-start", "memory", "/root/.claude/projects/fixture/memory/other.md", "x"),
        replace("output", "output", None, "passed", phase="after_observation"),
    ]
    variants = {event["id"]: [event] for event in events}
    append = {"id": "append", "method": "file", "sessions": ["exposure"], "path": "/app/NOTES.md", "text": "CI_DRY"}
    variants["append"] = [append, replace("after-append", "file", "/app/NOTES.md", "CI_DRY", phase="session_end")]
    memory = {"seed": {NOTE: "seed line\n"}}
    items = dry_check(config_with(dataset, variants, memory=memory), IDENTITIES, factory)
    found = statuses(items)
    assert found["file"] == ("ok", None)
    assert found["absent"] == ("missing", "old_text matches 0 times; replace needs exactly one match")
    assert found["twice"] == ("missing", "old_text matches 3 times; replace needs exactly one match")
    assert found["unshipped"] == ("missing", "the image ships no file at this path")
    assert found["seeded"] == ("ok", None)
    assert found["unseeded"][0] == "uncheckable"
    assert found["first-start"] == ("missing", "the first session starts with no memory at this path")
    assert found["output"] == ("uncheckable", "command output is unknown before the run")
    assert found["after-append"] == ("uncheckable", "append writes /app/NOTES.md earlier")
    assert {item["task"] for item in items} == {"first"} and len(items) == 9
    # One container for the task, closed after the reads.
    assert len(created) == 1 and created[0].closed


def test_memory_written_in_an_earlier_session_is_uncheckable(fixture_world):
    dataset = fixture_world[0]
    config = base.campaign_config(dataset).model_dump(mode="json")
    config["sessions"].insert(0, {**config["sessions"][0], "id": "early"})
    plant = {"id": "plant", "method": "memory", "sessions": ["early"], "path": NOTE, "text": "CI_DRY\n"}
    later = replace("later", "memory", NOTE, "seed line")
    config["variants"] = {"canary": [plant, later]}
    config["memory"] = {"seed": {NOTE: "seed line\n"}}
    items = dry_check(ExperimentConfig.model_validate(config), IDENTITIES, fixture_world[1])
    assert statuses(items)["later"] == (
        "uncheckable",
        "plant writes /root/.claude/projects/fixture/memory/note.md earlier",
    )
    # A repository starts from the image in every session, so an earlier session's file write does not count.
    config["variants"] = {
        "canary": [
            {"id": "early", "method": "file", "sessions": ["early"], "path": "/app/README.md", "text": "CI_DRY"},
            replace("file", "file", "/app/README.md", "documentation"),
        ]
    }
    items = dry_check(ExperimentConfig.model_validate(config), IDENTITIES, fixture_world[1])
    assert statuses(items)["file"] == ("ok", None)


def test_a_miss_after_session_start_is_uncheckable(fixture_world):
    dataset, factory, _, _ = fixture_world
    events = [
        replace("created", "file", "/app/solution_new.py", "x", phase="before_action"),
        replace("changed", "file", "/app/README.md", "CI_ABSENT", phase="session_end"),
        replace("seeded", "memory", NOTE, "CI_ABSENT", phase="after_observation"),
        replace("shipped", "file", "/app/README.md", "documentation", phase="before_action"),
    ]
    items = dry_check(config_with(dataset, events, memory={"seed": {NOTE: "seed line\n"}}), IDENTITIES, factory)
    found = statuses(items)
    assert found["created"] == ("uncheckable", "the agent may change or create the text before before_action")
    assert found["changed"] == ("uncheckable", "the agent may change or create the text before session_end")
    assert found["seeded"] == ("uncheckable", "the agent may change or create the text before after_observation")
    assert found["shipped"] == ("ok", None)


def test_action_phases_repeat_so_any_writer_of_the_session_may_come_first(fixture_world):
    dataset, factory, _, _ = fixture_world
    writer = {
        "id": "writer",
        "method": "file",
        "sessions": ["exposure"],
        "path": "/app/README.md",
        "phase": "after_observation",
        "text": "CI_DRY",
    }
    later = replace("later", "file", "/app/README.md", "CI_DRY", phase="before_action", min_step=2)
    closing = dict(writer, id="closing", phase="session_end")
    start = replace("start", "file", "/app/README.md", "documentation")
    variants = {"chain": [later, writer], "closing": [later, closing], "start": [start, writer]}
    found = {
        item["variant"]: (item["status"], item["reason"])
        for item in dry_check(config_with(dataset, variants), IDENTITIES, factory)
    }
    assert found["chain"] == ("uncheckable", "writer writes /app/README.md earlier")
    assert found["closing"][0] == "uncheckable" and "writer" not in found["closing"][1]
    assert found["start"] == ("ok", None)


def test_a_once_event_is_checked_only_in_its_first_session(fixture_world):
    dataset = fixture_world[0]
    config = base.campaign_config(dataset).model_dump(mode="json")
    config["sessions"].insert(0, {**config["sessions"][0], "id": "early"})
    once = replace("once", "file", "/app/README.md", "CI_ABSENT", sessions=["early", "exposure"])
    repeat = dict(once, id="repeat", strategy="repeat", max_fires=2)
    config["variants"] = {"once": [once], "repeat": [repeat]}
    items = dry_check(ExperimentConfig.model_validate(config), IDENTITIES, fixture_world[1])
    found = {(item["intervention"], item["session"]): item["status"] for item in items}
    assert found == {
        ("once", "early"): "missing",
        ("once", "exposure"): "uncheckable",
        ("repeat", "early"): "missing",
        ("repeat", "exposure"): "missing",
    }


def test_a_definite_miss_stops_the_campaign_before_any_session(tmp_path, fixture_world):
    dataset, factory, created, _ = fixture_world
    config = config_with(dataset, [replace("absent", "file", "/app/README.md", "CI_ABSENT")])
    root = tmp_path / "campaign"
    with pytest.raises(ValueError, match=r"absent/absent in session exposure of task first at /app/README\.md"):
        run_campaign(config, root, default_registry(), env_factory=factory, identities=IDENTITIES)
    assert not root.exists() and len(created) == 1
    config = config_with(dataset, [replace("file", "file", "/app/README.md", "documentation", marker="CI_DRY")])
    summary = run_campaign(config, root, default_registry(), env_factory=factory, identities=IDENTITIES)
    assert {row["status"] for row in summary["sessions"]} == {"complete"}


def test_the_cli_prints_every_item_and_fails_on_a_miss(tmp_path, fixture_world, capsys):
    config = config_with(fixture_world[0], [replace("seeded", "memory", NOTE, "CI_ABSENT")])
    raw = config.model_dump(mode="json") | {"memory": {"seed": {NOTE: "seed line\n"}}}
    path = tmp_path / "experiment.yaml"
    path.write_text(yaml.safe_dump(raw))
    assert main(["context-eval", "dry-check", str(path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False and [item["status"] for item in result["items"]] == ["missing"]
    raw["variants"]["seeded"][0]["old_text"] = "seed line"
    path.write_text(yaml.safe_dump(raw))
    assert main(["context-eval", "dry-check", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


@pytest.mark.skipif(os.environ.get("SERENO_DOCKER_TESTS") != "1", reason="set SERENO_DOCKER_TESTS=1 to run Docker")
def test_docker_dry_check_reads_the_file_the_image_ships(fixture_world):
    image = engine_fixes.local_deepswe_image()
    if image is None:
        pytest.skip("needs Docker and a local DeepSWE image")
    env = DockerEnvironment(image, 60)
    try:
        # Regular UTF-8 text files the image tracks, so the bridge can read them.
        tracked = "git -c safe.directory=/app ls-files -s | awk '$1 == \"100644\" {print $4}'"
        listed = env.execute(f"{tracked} | grep -E '\\.(md|py|txt|toml)$' | head -20")["output"].splitlines()
        path, line = next(
            (f"/app/{name}", line)
            for name in listed
            if (text := env.read(f"/app/{name}"))
            for line in text.splitlines()
            if len(line) > 20 and text.count(line) == 1
        )
    finally:
        env.close()
    events = [replace("shipped", "file", path, line), replace("absent", "file", path, "CI_DRY_ABSENT")]
    items = dry_check(config_with(fixture_world[0], events), {"first": {"id": image}}, DockerEnvironment)
    assert [item["status"] for item in items] == ["ok", "missing"]
