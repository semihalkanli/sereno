"""The writer against what it meets outside its own package: the cost meter's hook, and the real design package of
agent-learning when it is present on this machine."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from writer_world import KEY_ENV, World

from sereno.context_eval.schema import AdaptiveLimits
from sereno.writer.config import load_config
from sereno.writer.parsers import Answer, check_answer

HOOK = Path(__file__).parents[1] / "scripts" / "cost_hook"
PACKAGE = Path.home() / "agent-learning" / "docs" / "deepseek-writer-20261023"
CHILD = """
import sys
from writer_world import Transport, reply
from sereno.writer.config import load_config
from sereno.writer.provider import Deadline, Provider

config = load_config(sys.argv[1])
script = Transport(reply('{"a": 1}', cost=0.0123, generation="gen-cost"))
messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
Provider(config, Deadline(60), transport=script.transport).complete(messages, 1)
"""


def test_the_cost_meter_records_the_writers_calls(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "key")
    world = World(tmp_path)
    path = world.write_config(endpoint="https://openrouter.ai/api/v1/chat/completions")
    ledger = tmp_path / "calls.jsonl"
    env = {
        **os.environ,
        "SERENO_COST_CALLS": str(ledger),
        "PYTHONPATH": os.pathsep.join([str(HOOK), str(Path(__file__).parent)]),
    }
    subprocess.run([sys.executable, "-c", CHILD, str(path)], env=env, check=True, cwd=tmp_path)
    (row,) = (json.loads(line) for line in ledger.read_text().splitlines())
    assert row["id"] == "gen-cost" and row["cost"] == 0.0123 and row["status"] == 200
    assert row["provider"] == "DeepSeek" and row["model"] == "deepseek/test-model"
    assert row["prompt_tokens"] == 10 and row["completion_tokens"] == 5


@pytest.mark.skipif(not (PACKAGE / "writer-design.json").exists(), reason="the agent-learning design package is absent")
def test_the_real_design_package_loads_and_its_fixtures_classify_as_labelled(tmp_path):
    settings = tmp_path / "writer.json"
    settings.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "route": "memory",
                "design": str(PACKAGE / "writer-design.json"),
                "deadline_seconds": 1740,
                "max_input_bytes": 900_000,
            }
        )
    )
    config = load_config(settings)
    assert config.endpoint.startswith("https://openrouter.ai/") and config.api["provider"]["order"] == ["DeepSeek"]
    assert config.api["max_tokens"] == 128000 and "reasoning" not in config.api and "temperature" not in config.api
    assert len(config.forbidden_patterns) == 7 and config.memory_root.endswith("/projects/httpx/memory")
    assert config.last_write and config.system_prompt.strip() and config.objective
    for forbidden in ("research", "benchmark", "experiment", "defense", "disposable"):
        assert forbidden not in config.system_prompt.lower(), forbidden
    fixtures = json.loads((PACKAGE / "content-check-fixtures.json").read_text())
    limits = AdaptiveLimits(
        max_files_touched=2, max_file_bytes=6000, forbidden_patterns=list(config.forbidden_patterns)
    )
    root = config.memory_root
    original = "Original agent line.\n"
    agent_path = f"{root}/agent.md"
    for label, rejected in (("rejected", True), ("accepted", False), ("known_unmatched", False)):
        for fixture in fixtures[label]:
            text = fixture["content"] + "\n"
            new_file = {"op": "write", "path": f"{root}/n.md", "content": text}
            insertion = {"op": "write", "path": agent_path, "content": original + text}
            for action, memory in ((new_file, {}), (insertion, {agent_path: original})):
                checked = check_answer(Answer("ready", [action], "", ""), memory, [], limits, "httpx")
                assert ("content_constraint" in checked.codes) == rejected, (label, fixture["id"])
                assert checked.deliverable
