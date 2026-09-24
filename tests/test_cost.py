"""Offline checks for the cost hook and ledger summary in scripts/."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"

CHILD = """
import importlib, json, sys
module = importlib.import_module(sys.argv[1])

def handler(request):
    body = {
        "id": "gen-1",
        "model": "z-ai/glm-5.3-flash",
        "provider": "Z.AI",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.25},
    }
    return module.Response(200, json=body)

client = module.Client(transport=module.MockTransport(handler))
client.post("https://openrouter.ai/api/v1/chat/completions", json={})
client.post("https://example.com/v1/chat/completions", json={})
"""


@pytest.mark.parametrize("module", ["httpx", "httpx2"])
def test_hook_records_openrouter_calls_only(tmp_path: Path, module: str) -> None:
    if importlib.util.find_spec(module) is None:
        pytest.skip(f"{module} is not installed")
    calls = tmp_path / "calls.jsonl"
    env = {**os.environ, "PYTHONPATH": str(SCRIPTS / "cost_hook"), "SERENO_COST_CALLS": str(calls)}
    subprocess.run([sys.executable, "-c", CHILD, module], env=env, check=True)

    rows = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["provider"] == "Z.AI"
    assert rows[0]["cost"] == 0.25


def test_summarize_calls_separates_failed_calls(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("cost", SCRIPTS / "cost.py")
    assert spec and spec.loader
    cost = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cost)

    calls = tmp_path / "calls.jsonl"
    rows = [
        {"model": "m", "provider": "P", "prompt_tokens": 10, "completion_tokens": 2, "cost": 0.5},
        {"model": "m", "provider": "P", "prompt_tokens": 20, "completion_tokens": 3, "cost": 0.25},
        {"model": None, "provider": None, "prompt_tokens": None, "completion_tokens": None, "cost": None},
    ]
    calls.write_text("".join(json.dumps(r) + "\n" for r in rows))

    summary = cost.summarize_calls(calls)
    assert summary["calls"] == 2
    assert summary["failed_calls"] == 1
    assert summary["prompt_tokens"] == 30
    assert summary["cost_usd"] == 0.75
    assert summary["by_model"]["m"]["providers"] == ["P"]
