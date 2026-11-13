"""Decline resampling and accounting with scripted providers only."""

import json
from dataclasses import replace

import pytest
from attacker_world import INERT, NOTE, ScriptedProvider, call, completion, make_config, make_request
from test_writer_live import answer, request
from writer_world import KEY_ENV, Transport, World, reply

from sereno.attacker.agent import run_agent
from sereno.attacker.config import LoopSettings
from sereno.attacker.deadline import Deadline
from sereno.attacker.errors import ProviderFailure
from sereno.attacker.providers.anthropic import parse_completion as parse_anthropic
from sereno.attacker.providers.openai import parse_completion as parse_openai
from sereno.writer import live
from sereno.writer.errors import ProviderFailure as WriterProviderFailure


def decline():
    return replace(completion(finish="stop"), declined=True)


@pytest.mark.parametrize("signal", ["anthropic", "content_filter", "refusal"])
def test_adapter_decline_signals_preserve_usage(signal):
    if signal == "anthropic":
        body = {"content": [], "stop_reason": "refusal", "usage": {"input_tokens": 4, "output_tokens": 3}}
        result = parse_anthropic(json.dumps(body).encode())
        expected = 7
    else:
        message: dict = {"content": None}
        if signal == "refusal":
            message["refusal"] = "fixture"
        body = {
            "choices": [
                {"message": message, "finish_reason": "content_filter" if signal == "content_filter" else "stop"}
            ],
            "usage": {"total_tokens": 7},
        }
        result = parse_openai(json.dumps(body).encode(), None)
        expected = 7
    assert result.declined and result.usage["total_tokens"] == expected
    assert not result.tool_calls


def test_runtime_resamples_identical_turns_and_recovers(tmp_path):
    provider = ScriptedProvider(decline(), completion(call("finish")))
    result = run_agent(make_config(tmp_path), make_request(), provider, Deadline(120))
    assert provider.turns[0] == provider.turns[1]
    assert result.finish == "finish"
    assert result.response["writer_usage"]["declined_attempts"] == 1
    assert result.response["writer_usage"]["usage"]["total_tokens"] == 30


def test_runtime_exhausted_declines_preserve_staged_actions(tmp_path):
    provider = ScriptedProvider(
        completion(call("stage_action", op="write", path=NOTE, content=INERT)),
        decline(),
        decline(),
        decline(),
    )
    result = run_agent(make_config(tmp_path), make_request(), provider, Deadline(120))
    assert result.finish == result.response["status"] == "declined"
    assert len(result.response["actions"]) == 1
    assert provider.turns[1] == provider.turns[2] == provider.turns[3]
    assert result.response["writer_usage"]["declined_attempts"] == 3
    assert result.response["writer_usage"]["usage"]["total_tokens"] == 60


@pytest.mark.parametrize("bound", ["tokens", "deadline", "steps", "retries"])
def test_runtime_decline_resampling_stays_within_bounds(tmp_path, bound):
    now = [0.0]

    class BoundedProvider(ScriptedProvider):
        def complete(self, *args, **kwargs):
            result = super().complete(*args, **kwargs)
            if bound == "deadline":
                now[0] = 200.0
            return result

    provider = BoundedProvider(decline())
    config = make_config(tmp_path, agent=LoopSettings(max_steps=1 if bound == "steps" else 24))
    if bound == "retries":
        config = replace(config, settings=config.settings.model_copy(update={"decline_retries": 0}))
    req = make_request()
    if bound == "tokens":
        req["contract"]["budget"]["max_tokens"] = 20
        req["contract"]["used"]["tokens"] = 5
    result = run_agent(config, req, provider, Deadline(120, clock=lambda: now[0]))
    assert result.finish == "declined" and len(result.completions) == 1
    assert result.response["writer_usage"]["usage"]["total_tokens"] == 15


def test_runtime_decline_then_provider_failure_preserves_known_usage(tmp_path):
    class FailingProvider(ScriptedProvider):
        def complete(self, *args, **kwargs):
            if self.turns:
                raise ProviderFailure("fixture transport failure", "http_permanent")
            return super().complete(*args, **kwargs)

    with pytest.raises(ProviderFailure) as caught:
        run_agent(make_config(tmp_path), make_request(), FailingProvider(decline()), Deadline(120))
    assert caught.value.usage is not None
    assert caught.value.usage["usage"]["total_tokens"] == 15
    assert caught.value.usage["declined_attempts"] == 1
    assert caught.value.usage["unreported_completions"] == 1


@pytest.mark.parametrize("signal", ["unable", "content_filter", "refusal"])
@pytest.mark.parametrize("recover", [True, False])
def test_live_writer_declines_resample_identical_requests_and_account_for_all_attempts(
    tmp_path, monkeypatch, signal, recover
):
    monkeypatch.setenv(KEY_ENV, "fixture-key")
    config = World(tmp_path).config()
    body = reply(answer(status="unable"))
    if signal == "content_filter":
        body = reply("", finish="content_filter")
    elif signal == "refusal":
        body = reply("", extra={"usage": {}})
        body["choices"][0]["message"]["refusal"] = "fixture"
    transport = Transport(body, reply(answer()) if recover else body, *([] if recover else [body]))
    response = tmp_path / "response.json"
    live.run_live(config, request(tmp_path), response, transport=transport.transport)
    result = json.loads(response.read_bytes())
    assert len(transport.requests) == (2 if recover else 3)
    assert len({item.content for item in transport.requests}) == 1
    assert result.get("status") == (None if recover else "declined")
    assert result["actions"] == []
    usage = result["writer_usage"]
    assert usage["declined_attempts"] == (1 if recover else 3)
    if signal == "refusal":
        assert usage["unreported_completions"] == (1 if recover else 3)
        assert usage["usage"].get("total_tokens", 0) == (15 if recover else 0)
    else:
        assert usage["usage"]["total_tokens"] == (30 if recover else 45)


@pytest.mark.parametrize("bound", ["tokens", "deadline", "retries", "calls"])
def test_live_writer_decline_resampling_respects_remaining_bounds(tmp_path, monkeypatch, bound):
    monkeypatch.setenv(KEY_ENV, "fixture-key")
    config = World(tmp_path).config()
    if bound == "retries":
        config = replace(config, settings=config.settings.model_copy(update={"decline_retries": 0}))
    if bound == "calls":
        config = replace(config, control=replace(config.control, repairs=0))
    remaining = iter([1.0, 0.0])
    if bound == "deadline":

        class FixedDeadline:
            def __init__(self, seconds):
                pass

            def remaining(self):
                return next(remaining, 0.0)

        monkeypatch.setattr(live, "Deadline", FixedDeadline)
    transport = Transport(reply(answer(status="unable")))
    response = tmp_path / "response.json"
    req = request(tmp_path, **({"budget": {"max_tokens": 20}, "used": {"tokens": 5}} if bound == "tokens" else {}))
    live.run_live(config, req, response, transport=transport.transport)
    result = json.loads(response.read_bytes())
    assert result["status"] == "declined" and len(transport.requests) == 1
    assert result["writer_usage"]["usage"]["total_tokens"] == 15


def test_live_writer_decline_then_http_failure_preserves_usage_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "fixture-key")
    config = World(tmp_path).config()
    transport = Transport(reply(answer(status="unable")), (400, {"error": "fixture"}, {}))
    response = tmp_path / "response.json"
    with pytest.raises(WriterProviderFailure):
        live.run_live(config, request(tmp_path), response, transport=transport.transport)
    assert not response.exists()
    usage = json.loads((tmp_path / "failure_usage.json").read_bytes())
    assert usage["usage"]["total_tokens"] == 15
    assert usage["declined_attempts"] == 1 and usage["unreported_completions"] == 1
