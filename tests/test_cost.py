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


REQUESTS_CHILD = """
import json, requests

class Adapter(requests.adapters.BaseAdapter):
    def send(self, request, **kwargs):
        usage = {"input_tokens": 7, "output_tokens": 3, "cost": 0.125}
        response = requests.models.Response()
        response.status_code = 200
        response._content = json.dumps({"id": "gen-2", "model": "m", "provider": "P", "usage": usage}).encode()
        return response

session = requests.Session()
session.mount("https://", Adapter())
session.post("https://openrouter.ai/api/v1/responses", json={})
session.post("https://openrouter.ai/api/v1/chat/completions", json={})
session.post("https://example.com/v1/responses", json={})
"""


def test_hook_records_requests_calls_and_the_responses_api(tmp_path: Path) -> None:
    if importlib.util.find_spec("requests") is None:
        pytest.skip("requests is not installed")
    calls = tmp_path / "calls.jsonl"
    env = {**os.environ, "PYTHONPATH": str(SCRIPTS / "cost_hook"), "SERENO_COST_CALLS": str(calls)}
    subprocess.run([sys.executable, "-c", REQUESTS_CHILD], env=env, check=True)

    rows = [json.loads(line) for line in calls.read_text().splitlines()]
    assert len(rows) == 2
    assert (rows[0]["prompt_tokens"], rows[0]["completion_tokens"], rows[0]["cost"]) == (7, 3, 0.125)


ANTHROPIC_CHILD = """
import json, sys, httpx

USAGES = {
    "short": {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 4000,
              "cache_creation_input_tokens": 3000,
              "cache_creation": {"ephemeral_5m_input_tokens": 2000, "ephemeral_1h_input_tokens": 1000}},
    "long": {"input_tokens": 100001, "output_tokens": 10},
    "us": {"input_tokens": 1000, "output_tokens": 0, "inference_geo": "us"},
    "unknown": {"input_tokens": 5, "output_tokens": 5},
}

def handler(request):
    case = json.loads(request.content)["case"]
    if case == "error":
        return httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error"}})
    model = "claude-new-model" if case == "unknown" else "claude-haiku-5-5"
    return httpx.Response(200, json={"id": case, "model": model, "usage": USAGES[case]})

client = httpx.Client(transport=httpx.MockTransport(handler))
for case in ("short", "long", "us", "unknown", "error"):
    client.post("https://api.anthropic.com/v1/messages", json={"case": case})
client.post("https://api.anthropic.com/v1/models", json={"case": "short"})
"""


def test_hook_prices_anthropic_calls_from_their_usage(tmp_path: Path) -> None:
    calls = tmp_path / "calls.jsonl"
    env = {**os.environ, "PYTHONPATH": str(SCRIPTS / "cost_hook"), "SERENO_COST_CALLS": str(calls)}
    subprocess.run([sys.executable, "-c", ANTHROPIC_CHILD], env=env, check=True)

    rows = {row["id"] or "error": row for row in map(json.loads, calls.read_text().splitlines())}
    assert set(rows) == {"short", "long", "us", "unknown", "error"}
    short = rows["short"]
    assert (short["provider"], short["prompt_tokens"], short["completion_tokens"]) == ("anthropic", 8000, 200)
    # 1000 input + 2000 x 1.25 + 1000 x 2 + 4000 x 0.1 at USD 0.10, and 200 output at USD 0.50 per million.
    assert short["cost"] == pytest.approx((1000 + 2500 + 2000 + 400) * 0.10e-6 + 200 * 0.50e-6)
    assert rows["long"]["cost"] == pytest.approx(100001 * 0.50e-6 + 10 * 2.50e-6)
    assert rows["us"]["cost"] == pytest.approx(1000 * 0.10e-6 * 1.1)
    assert (rows["unknown"]["cost"], rows["unknown"]["unpriced"]) == (None, True)
    assert (rows["error"]["cost"], rows["error"]["unpriced"], rows["error"]["status"]) == (None, False, 529)


SDK_CHILD = """
import anthropic, httpx2

def handler(request):
    usage = {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 4000}
    body = {"id": "msg_sdk", "type": "message", "role": "assistant", "model": "claude-haiku-5-5", "content": [],
            "stop_reason": "end_turn", "stop_sequence": None, "usage": usage}
    return httpx2.Response(200, json=body)

client = anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
messages = [{"role": "user", "content": "x"}]
client.messages.create(model="claude-haiku-5-5", max_tokens=128000, timeout=6000, messages=messages)
"""


def test_hook_records_the_anthropic_sdk_without_streaming(tmp_path: Path) -> None:
    calls = tmp_path / "calls.jsonl"
    env = {**os.environ, "PYTHONPATH": str(SCRIPTS / "cost_hook"), "SERENO_COST_CALLS": str(calls)}
    subprocess.run([sys.executable, "-c", SDK_CHILD], env=env, check=True)

    [row] = [json.loads(line) for line in calls.read_text().splitlines()]
    assert (row["id"], row["provider"], row["prompt_tokens"]) == ("msg_sdk", "anthropic", 5000)
    assert row["cost"] == pytest.approx((1000 + 400) * 0.10e-6 + 200 * 0.50e-6)


def test_summarize_calls_keeps_unpriced_calls_apart(tmp_path: Path) -> None:
    cost = load_cost()
    calls = tmp_path / "calls.jsonl"
    rows = [
        {
            "model": "claude-haiku-5-5",
            "provider": "anthropic",
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "cost": 0.5,
        },
        {
            "model": "claude-new",
            "provider": "anthropic",
            "prompt_tokens": 9,
            "completion_tokens": 1,
            "cost": None,
            "unpriced": True,
        },
        {
            "model": None,
            "provider": "anthropic",
            "prompt_tokens": None,
            "completion_tokens": None,
            "cost": None,
            "unpriced": False,
        },
    ]
    calls.write_text("".join(json.dumps(r) + "\n" for r in rows))

    summary = cost.summarize_calls(calls)
    assert (summary["calls"], summary["failed_calls"], summary["unpriced_calls"]) == (1, 1, 1)
    assert summary["unpriced_models"] == ["claude-new"]
    assert summary["cost_usd"] == 0.5


def load_cost():
    spec = importlib.util.spec_from_file_location("cost", SCRIPTS / "cost.py")
    assert spec and spec.loader
    cost = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cost)
    return cost


def test_summarize_calls_separates_failed_calls(tmp_path: Path) -> None:
    cost = load_cost()
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


def test_interrupted_run_still_appends_its_ledger_row(tmp_path: Path, monkeypatch) -> None:
    cost = load_cost()
    ledger = tmp_path / "ledger.jsonl"

    def interrupted(command, env):
        row = {"model": "m", "provider": "P", "prompt_tokens": 1, "completion_tokens": 1, "cost": 0.5}
        Path(env["SERENO_COST_CALLS"]).write_text(json.dumps(row) + "\n")
        raise KeyboardInterrupt

    monkeypatch.setattr(cost, "REPO", tmp_path)
    monkeypatch.setattr(cost.subprocess, "run", interrupted)
    monkeypatch.setattr(cost, "git_state", lambda: {"commit": "c", "dirty": False})
    monkeypatch.setattr(cost, "openrouter_get", lambda path: None)
    monkeypatch.setattr(sys, "argv", ["cost.py", "--ledger", str(ledger), "run", "--label", "t", "--", "true"])

    assert cost.main() == -2
    (row,) = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert (row["exit_code"], row["calls"], row["cost_usd"]) == (-2, 1, 0.5)


def test_run_passes_the_repository_env_file_to_the_command(tmp_path: Path, monkeypatch) -> None:
    cost = load_cost()
    (tmp_path / ".env").write_text("SERENO_COST_FIXTURE=from-dotenv\n")
    monkeypatch.setenv("SERENO_COST_FIXTURE", "unset")
    monkeypatch.delenv("SERENO_COST_FIXTURE")
    seen = {}

    def run(command, env):
        seen.update(env)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(cost, "REPO", tmp_path)
    monkeypatch.setattr(cost.subprocess, "run", run)
    monkeypatch.setattr(cost, "git_state", lambda: {"commit": "c", "dirty": False})
    monkeypatch.setattr(cost, "openrouter_get", lambda path: None)
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(sys, "argv", ["cost.py", "--ledger", str(ledger), "run", "--label", "t", "--", "true"])

    assert cost.main() == 0
    assert seen["SERENO_COST_FIXTURE"] == "from-dotenv"
