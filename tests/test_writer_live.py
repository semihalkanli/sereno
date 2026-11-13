"""The live writer adapter with a scripted provider: no network, no state of its own."""

import json
from pathlib import Path

import pytest
from writer_world import (
    KEY_ENV,
    LIMITS,
    OPENCODE_API,
    OPENCODE_BUILD,
    OPENCODE_ENDPOINT,
    Transport,
    World,
    opencode_reply,
    reply,
)

from sereno.writer import live
from sereno.writer.errors import InputError, OutputError


def request(tmp_path: Path, **contract) -> Path:
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


def request_with_session(tmp_path: Path, session_id: str, call: int = 1) -> Path:
    """A live request that names the session and the call, as the harness's request does."""
    path = request(tmp_path)
    data = json.loads(path.read_text())
    data["observation"]["session"] = {"id": session_id, "task_id": "fixture"}
    data["contract"]["session_call"] = call
    path.write_text(json.dumps(data))
    return path


def test_an_input_limit_after_a_completion_keeps_the_failure_usage(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "fixture-key")
    world = World(tmp_path)
    req = request(tmp_path)
    response = tmp_path / "live-response.json"
    sizes = []
    original = live.check_size

    def recording(config, messages):
        size = original(config, messages)
        sizes.append(size)
        return size

    monkeypatch.setattr(live, "check_size", recording)
    live.run_live(
        world.config(), req, response, transport=Transport(reply(answer(strategy=7)), reply(answer())).transport
    )
    first, second = sizes[0], sizes[1]
    assert second > first
    transport = Transport(reply(answer(strategy=7)), reply(answer()))
    with pytest.raises(InputError) as caught:
        live.run_live(world.config(max_input=first), req, response, transport=transport.transport)
    assert caught.value.exit_code == 3
    usage = json.loads((tmp_path / "failure_usage.json").read_text())
    assert usage["usage"]["total_tokens"] == 15
    assert usage["cost"] == 0.0123
    assert usage["unreported_completions"] == 0


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


def test_live_usage_preserves_reported_repair_totals_when_one_call_has_no_usage(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "fixture-key")
    config = World(tmp_path).config()
    transport = Transport(reply("not json", extra={"usage": {}}), reply(answer()))
    response = tmp_path / "live-response.json"
    live.run_live(config, request(tmp_path), response, transport=transport.transport)
    usage = json.loads(response.read_text())["writer_usage"]
    assert usage["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert usage["unreported_completions"] == 1
    assert usage["cost"] == 0.0123


def test_opencode_usage_records_the_allowance_the_build_and_one_conversation(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "fixture-key")
    config = World(tmp_path).config(api=OPENCODE_API, endpoint=OPENCODE_ENDPOINT)
    transport = Transport(opencode_reply(answer(strategy=7)), opencode_reply(answer()))
    response = tmp_path / "live-response.json"
    summary = live.run_live(config, request_with_session(tmp_path, "exposure"), response, transport=transport.transport)
    usage = json.loads(response.read_text())["writer_usage"]
    assert usage["provider_kind"] == "opencode" and usage["provider"] == "opencode"
    assert usage["requested_model"] == "deepseek-v4.1-flash" and usage["returned_model"] == OPENCODE_BUILD
    assert usage["billing"] == "allowance" and usage["billing_source"] == "subscription"
    assert usage["allowance_usd"] == pytest.approx(0.0246)
    assert usage["cost"] == summary["cost"] == pytest.approx(0.0246)
    assert usage["completions"][0]["allowance"] == pytest.approx(0.0123)
    # One conversation for the whole call: the repair carries the same session id.
    sessions = [record.headers["x-opencode-session"] for record in transport.requests]
    assert len(sessions) == 2 and len(set(sessions)) == 1
    # The same request on a resume rebuilds the same id; another call of the session is another conversation.
    again = Transport(opencode_reply(answer(strategy=7)), opencode_reply(answer()))
    live.run_live(
        config, request_with_session(tmp_path, "exposure"), tmp_path / "again.json", transport=again.transport
    )
    assert again.requests[0].headers["x-opencode-session"] == sessions[0]
    other = Transport(opencode_reply(answer(strategy=7)), opencode_reply(answer()))
    live.run_live(
        config, request_with_session(tmp_path, "exposure", 2), tmp_path / "other.json", transport=other.transport
    )
    assert other.requests[0].headers["x-opencode-session"] != sessions[0]


def test_unusable_answers_end_without_a_response(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "fixture-key")
    config = World(tmp_path).config()
    transport = Transport(*[reply("not json")] * (config.control.repairs + 1))
    response = tmp_path / "live-response.json"
    with pytest.raises(OutputError, match="invalid_json"):
        live.run_live(config, request(tmp_path), response, transport=transport.transport)
    assert not response.exists()
