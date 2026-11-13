"""The OpenAI-compatible and Anthropic adapters against scripted HTTP transports."""

import json

import httpx
import pytest
from attacker_world import make_config

from sereno.attacker.deadline import Deadline
from sereno.attacker.errors import ConfigError, ProviderFailure
from sereno.attacker.providers import build_provider
from sereno.attacker.providers.anthropic import AnthropicProvider
from sereno.attacker.providers.anthropic import messages as anthropic_messages
from sereno.attacker.providers.base import AssistantTurn, SystemTurn, ToolCall, ToolTurn, UserTurn
from sereno.attacker.providers.openai import OpenAICompatibleProvider
from sereno.attacker.tools import TOOL_SPECS


def openai_completion(content, *, finish="tool_calls", message=None) -> dict:
    return {
        "id": "gen-1",
        "model": "fixture-model",
        "choices": [{"message": message or {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


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
        ({"role": "assistant", "content": "", "refusal": "no"}, "stop", "refusal"),
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
            provider.complete([UserTurn("user")], TOOL_SPECS)
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


def test_a_missing_credential_is_refused_before_a_call(tmp_path, monkeypatch):
    monkeypatch.delenv("SERENO_ATTACKER_MISSING_KEY", raising=False)
    config = make_config(tmp_path, credential_env="SERENO_ATTACKER_MISSING_KEY")
    with pytest.raises(ConfigError) as caught:
        build_provider(config, Deadline(60))
    assert caught.value.code == "missing_credential"
