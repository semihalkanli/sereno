"""The OpenAI-compatible and Anthropic adapters against scripted HTTP transports."""

import json

import httpx
import pytest
from attacker_world import make_config, make_request

from sereno.attacker.deadline import Deadline
from sereno.attacker.errors import ConfigError, ProviderFailure
from sereno.attacker.protocol import usage_record
from sereno.attacker.providers import build_provider
from sereno.attacker.providers.anthropic import AnthropicProvider
from sereno.attacker.providers.anthropic import messages as anthropic_messages
from sereno.attacker.providers.anthropic import parse_completion as parse_anthropic_completion
from sereno.attacker.providers.base import AssistantTurn, SystemTurn, ToolCall, ToolTurn, UserTurn
from sereno.attacker.providers.openai import OpenAICompatibleProvider, conversation_id
from sereno.attacker.tools import TOOL_SPECS

OPENCODE_URL = "https://opencode.ai/inference/go/openai/v1/chat/completions"
OPENCODE_UA = "sereno-attacker/1.0"
SESSION_HEADER = "x-opencode-session"


def openai_completion(content, *, finish="tool_calls", message=None) -> dict:
    return {
        "id": "gen-1",
        "model": "fixture-model",
        "choices": [{"message": message or {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def test_anthropic_counts_cache_creation_and_cache_read_input_tokens():
    completion = parse_anthropic_completion(
        json.dumps(
            {
                "content": [{"type": "text", "text": "answer"}],
                "stop_reason": "end_turn",
                "usage": {
                    "input_tokens": 4,
                    "output_tokens": 3,
                    "cache_creation_input_tokens": 10,
                    "cache_read_input_tokens": 20,
                },
            }
        ).encode()
    )
    assert completion.usage["prompt_tokens"] == 34
    assert completion.usage["completion_tokens"] == 3
    assert completion.usage["total_tokens"] == 37
    assert "cost" not in completion.usage


def test_openai_maps_turns_tools_and_reasoning(tmp_path):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=openai_completion(
                None,
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "finish", "arguments": "{}"}}],
                },
            ),
        )

    provider = OpenAICompatibleProvider(make_config(tmp_path), Deadline(60), transport=httpx.MockTransport(handler))
    turns = [
        SystemTurn("system"),
        UserTurn("user"),
        AssistantTurn(
            "assistant",
            (ToolCall("c1", "read_file", '{"path":"/app/pkg/core.py"}'),),
            {"reasoning_content": "kept"},
        ),
        ToolTurn("c1", "read_file", '{"status":"ok"}'),
    ]
    try:
        completion = provider.complete(turns, TOOL_SPECS)
    finally:
        provider.close()
    body = seen[0]
    assert body["model"] == "fixture-model"
    assert body["messages"][0] == {"role": "system", "content": "system"}
    assert body["messages"][1] == {"role": "user", "content": "user"}
    assert body["messages"][2] == {
        "role": "assistant",
        "content": "assistant",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"/app/pkg/core.py"}'},
            }
        ],
        "reasoning_content": "kept",
    }
    assert body["messages"][3] == {
        "role": "tool",
        "tool_call_id": "c1",
        "name": "read_file",
        "content": '{"status":"ok"}',
    }
    assert body["tools"][0]["function"]["name"] == "list_sessions"
    assert "stream" not in body
    assert completion.tool_calls[0].name == "finish"
    assert completion.usage["total_tokens"] == 5
    assert completion.model == "fixture-model"


@pytest.mark.parametrize(
    ("message", "finish", "code"),
    [
        ({"role": "assistant", "content": "cut"}, "length", "truncated"),
        ({"role": "assistant", "content": ""}, "stop", "empty_completion"),
    ],
)
def test_openai_ends_on_an_unusable_answer(tmp_path, message, finish, code):
    def handler(request):
        return httpx.Response(200, json=openai_completion(message["content"], finish=finish, message=message))

    provider = OpenAICompatibleProvider(make_config(tmp_path), Deadline(60), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(ProviderFailure) as caught:
            provider.complete([UserTurn("user")], TOOL_SPECS, call=1)
    finally:
        provider.close()
    assert caught.value.code == code


def test_anthropic_maps_system_and_tool_blocks(tmp_path):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "model": "claude-fixture",
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "answer"},
                    {"type": "tool_use", "id": "c2", "name": "finish", "input": {}},
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 4, "output_tokens": 3},
            },
        )

    config = make_config(tmp_path, kind="anthropic", request={"max_tokens": 1024})
    provider = AnthropicProvider(config, Deadline(60), transport=httpx.MockTransport(handler))
    turns = [
        SystemTurn("system"),
        UserTurn("user"),
        AssistantTurn(
            "assistant",
            (ToolCall("c1", "read_file", '{"path":"/app/pkg/core.py"}'),),
        ),
        ToolTurn("c1", "read_file", '{"status":"ok"}'),
    ]
    try:
        completion = provider.complete(turns, TOOL_SPECS)
    finally:
        provider.close()
    body = seen[0]
    assert body["system"] == "system"
    assert body["max_tokens"] == 1024
    assert body["messages"][0] == {"role": "user", "content": [{"type": "text", "text": "user"}]}
    assert body["messages"][1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "assistant"},
            {"type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": "/app/pkg/core.py"}},
        ],
    }
    assert body["messages"][2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "c1", "content": '{"status":"ok"}'}],
    }
    assert body["tools"][0] == {
        "name": "list_sessions",
        "description": TOOL_SPECS[0].description,
        "input_schema": TOOL_SPECS[0].parameters,
    }
    assert completion.usage == {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7}
    assert completion.tool_calls[0].name == "finish"


def test_anthropic_replays_raw_thinking_blocks():
    raw = [
        {"type": "thinking", "thinking": "private", "signature": "signature"},
        {"type": "tool_use", "id": "c1", "name": "finish", "input": {}},
    ]
    out = anthropic_messages(
        [
            SystemTurn("system"),
            UserTurn("user"),
            AssistantTurn("assistant", reasoning={"blocks": raw}),
            ToolTurn("c1", "finish", "{}"),
        ]
    )
    assert out[1]["content"] == raw
    assert out[2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "{}"}]}


def test_build_provider_chooses_the_configured_kind(tmp_path):
    openai = build_provider(make_config(tmp_path), Deadline(60))
    try:
        assert isinstance(openai, OpenAICompatibleProvider)
    finally:
        openai.close()
    anthropic = build_provider(make_config(tmp_path, kind="anthropic", request={"max_tokens": 1024}), Deadline(60))
    try:
        assert isinstance(anthropic, AnthropicProvider)
    finally:
        anthropic.close()


def opencode_body(allowance=0.02, billing_source="subscription", model="deepseek-v4.1-flash"):
    """A chat-completions answer as the opencode Go API returns it: no serving provider, an allowance charge, and the
    requested alias as its `model`, which is what the live API returned."""
    return {
        "id": "gen-oc-1",
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
            "opencode": {"allowance": allowance, "billing_source": billing_source},
        },
    }


def opencode_config(tmp_path):
    return make_config(
        tmp_path,
        model="deepseek-v4.1-flash",
        base_url=OPENCODE_URL,
        user_agent=OPENCODE_UA,
        session_header=SESSION_HEADER,
    )


def test_the_opencode_request_carries_the_session_the_product_agent_and_no_sampling_parameters(tmp_path):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=opencode_body())

    request = make_request()
    provider = build_provider(
        opencode_config(tmp_path), Deadline(60), transport=httpx.MockTransport(handler), request=request
    )
    try:
        provider.complete([SystemTurn("system"), UserTurn("user")], TOOL_SPECS, call=1)
        provider.complete([UserTurn("more")], TOOL_SPECS, call=2)
    finally:
        provider.close()
    session = seen[0].headers[SESSION_HEADER]
    assert len(session) == 32 and "/" not in session
    assert [record.headers[SESSION_HEADER] for record in seen] == [session, session]
    assert {record.headers["user-agent"] for record in seen} == {OPENCODE_UA}
    body = json.loads(seen[0].content)
    assert body["model"] == "deepseek-v4.1-flash" and body["messages"][0] == {"role": "system", "content": "system"}
    for barred in ("temperature", "top_p", "top_k", "seed", "n", "reasoning", "reasoning_effort", "provider"):
        assert barred not in body
    # A different call of the session is a different conversation.
    other = conversation_id(make_request(session_call=2))
    assert other != session


def test_opencode_usage_and_record_carry_the_allowance_and_the_billing_source(tmp_path):
    def handler(request):
        return httpx.Response(200, json=opencode_body())

    config = opencode_config(tmp_path)
    provider = build_provider(config, Deadline(60), transport=httpx.MockTransport(handler), request=make_request())
    try:
        completion = provider.complete([UserTurn("user")], TOOL_SPECS, call=1)
    finally:
        provider.close()
    record = completion.record()
    assert record["cost"] == pytest.approx(0.02) and record["allowance"] == pytest.approx(0.02)
    assert record["billing_source"] == "subscription" and record["returned_model"] == "deepseek-v4.1-flash"
    usage = usage_record([completion], finish="finish", steps=1, provider=provider, config=config)
    assert usage["cost"] == pytest.approx(0.02) and usage["allowance_usd"] == pytest.approx(0.02)
    assert usage["billing"] == "allowance" and usage["billing_source"] == "subscription"
    assert usage["requested_model"] == "deepseek-v4.1-flash"
    assert usage["returned_model"] == "deepseek-v4.1-flash"


def test_a_returned_model_other_than_the_alias_is_recorded_as_returned(tmp_path):
    """The live API returns the requested alias; a different string the API returns is still recorded as returned,
    never replaced by the requested model."""

    def handler(request):
        return httpx.Response(200, json=opencode_body(model="deepseek-v4.1-flash-20261101"))

    config = opencode_config(tmp_path)
    provider = build_provider(config, Deadline(60), transport=httpx.MockTransport(handler), request=make_request())
    try:
        completion = provider.complete([UserTurn("user")], TOOL_SPECS, call=1)
    finally:
        provider.close()
    usage = usage_record([completion], finish="finish", steps=1, provider=provider, config=config)
    assert usage["requested_model"] == "deepseek-v4.1-flash"
    assert usage["returned_model"] == "deepseek-v4.1-flash-20261101"


def test_the_conversation_id_is_unique_per_chain_case_arm_repeat_and_agent():
    """The id a provider routes by comes from the chain identity the request carries: two campaigns, cases, arms,
    repeats, targets or agents never share one, and the same conversation always rebuilds the same id."""
    base = {
        "campaign": "campaign-a",
        "case": "t1--default--r001",
        "arm": "attack_carry",
        "repeat": 1,
        "target": "t1",
        "agent_id": "main",
    }

    def request(chain):
        req = make_request()
        req["contract"]["chain"] = chain
        return req

    ids = {conversation_id(request(base))}
    assert conversation_id(request(dict(base))) in ids
    for change in (
        {"campaign": "campaign-b"},
        {"case": "t1--defended--r001"},
        {"arm": "attack_reset"},
        {"repeat": 2},
        {"target": "t2"},
        {"agent_id": "helper"},
    ):
        other = conversation_id(request(base | change))
        assert other not in ids
        ids.add(other)
    assert len(ids) == 7
    # A request without the field still yields a stable id, never an error.
    assert conversation_id(make_request()) == conversation_id(make_request())


def test_a_400_missing_session_id_is_a_provider_failure(tmp_path):
    def handler(request):
        return httpx.Response(400, json={"error": {"message": "MissingSessionID"}})

    provider = build_provider(
        opencode_config(tmp_path), Deadline(60), transport=httpx.MockTransport(handler), request=make_request()
    )
    try:
        with pytest.raises(ProviderFailure) as caught:
            provider.complete([UserTurn("user")], TOOL_SPECS, call=1)
    finally:
        provider.close()
    assert caught.value.code == "http_400" and "MissingSessionID" in str(caught.value)


def test_a_user_agent_and_session_header_are_optional(tmp_path):
    def handler(request):
        return httpx.Response(200, json=openai_completion("answer"))

    provider = build_provider(make_config(tmp_path), Deadline(60), transport=httpx.MockTransport(handler))
    try:
        completion = provider.complete([UserTurn("user")], TOOL_SPECS, call=1)
    finally:
        provider.close()
    assert completion.record()["allowance"] is None and completion.record()["billing_source"] is None


def test_a_missing_credential_is_refused_before_a_call(tmp_path, monkeypatch):
    monkeypatch.delenv("FIXTURE_ATTACKER_MISSING_KEY", raising=False)
    config = make_config(tmp_path, credential_env="FIXTURE_ATTACKER_MISSING_KEY")
    with pytest.raises(ConfigError) as caught:
        build_provider(config, Deadline(60))
    assert caught.value.code == "missing_credential"
