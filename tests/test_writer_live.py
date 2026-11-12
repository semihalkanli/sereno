"""The live writer adapter with a scripted provider: no network, no state of its own."""

import json

import pytest
from writer_world import KEY_ENV, LIMITS, Transport, World, reply

from sereno.writer import live
from sereno.writer.errors import OutputError


def request(tmp_path, **contract) -> object:
    path = tmp_path / "live-request.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "contract": {
                    "kind": "in_task",
                    "stage": "exposure",
                    "writes_open": True,
                    "limits": LIMITS,
                    "timeout_seconds": 1800,
                    "notebook_max_bytes": 20000,
                    "strategy_max_bytes": 20000,
                }
                | contract,
                "strategy": {"version": 2, "text": "current strategy", "sha256": "x"},
                "notebook": "current revision",
                "observation": {
                    "code_views": {"/app/pkg/core.py": {"status": "ok", "text": "def alpha(): return 1"}},
                    "transcript": [{"role": "user", "content": "fixture task"}],
                    "completed_sessions": [],
                },
            }
        )
    )
    return path


def answer(**changes) -> str:
    return json.dumps({"status": "no_change", "actions": [], "notebook": "next revision", "explanation": "e"} | changes)


def test_live_adapter_passes_three_parts_and_returns_state_and_usage(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "fixture-key")
    config = World(tmp_path).config()
    transport = Transport(reply(answer(strategy="next strategy", reads=["/app/pkg/core.py"])))
    response = tmp_path / "live-response.json"
    summary = live.run_live(config, request(tmp_path), response, transport=transport.transport)
    result = json.loads(response.read_text())
    assert result["notebook"] == "next revision" and result["strategy"] == "next strategy"
    assert result["reads"] == ["/app/pkg/core.py"] and result["actions"] == []
    assert result["writer_usage"]["cost"] == summary["cost"] == 0.0123
    assert result["writer_usage"]["usage"]["total_tokens"] == 15
    body = transport.bodies()[0]
    assert body["messages"][0] == {"role": "system", "content": config.system_prompt}
    sent = json.loads(body["messages"][1]["content"])
    assert sent["objective"] == config.objective and sent["contract"]["stage"] == "exposure"
    assert sent["strategy"]["text"] == "current strategy" and sent["notebook"] == "current revision"
    assert sent["observation"]["transcript"] == [{"role": "user", "content": "fixture task"}]
    assert set(sent["output_contract"]["keys"]) == {"status", "actions", "notebook", "strategy", "reads", "explanation"}


def test_strategy_left_out_keeps_it_and_a_structural_problem_is_repaired(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "fixture-key")
    config = World(tmp_path).config()
    transport = Transport(reply(answer(strategy=7)), reply(answer()))
    response = tmp_path / "live-response.json"
    summary = live.run_live(config, request(tmp_path), response, transport=transport.transport)
    result = json.loads(response.read_text())
    assert "strategy" not in result and summary["calls"] == 2
    repair = transport.bodies()[1]["messages"][-1]["content"]
    assert "bad_strategy" in repair


def test_unusable_answers_end_without_a_response(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "fixture-key")
    config = World(tmp_path).config()
    transport = Transport(*[reply("not json")] * (config.control.repairs + 1))
    response = tmp_path / "live-response.json"
    with pytest.raises(OutputError, match="invalid_json"):
        live.run_live(config, request(tmp_path), response, transport=transport.transport)
    assert not response.exists()
