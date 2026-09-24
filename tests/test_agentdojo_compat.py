"""Offline checks that the locked dependency set works with AgentDojo.

AgentDojo 0.1.35 was locked upstream against openai 1.x; these tests drive its
OpenAI wrapper through the installed client with a mocked HTTP transport, so a
breaking client upgrade fails here instead of in a paid run.
"""

import json

import httpx
import openai
from agentdojo.agent_pipeline import (
    AgentPipeline,
    InitQuery,
    OpenAILLM,
    SystemMessage,
    ToolsExecutionLoop,
    ToolsExecutor,
)
from agentdojo.functions_runtime import FunctionsRuntime
from agentdojo.task_suite.load_suites import get_suite, get_suites

BENCHMARK_VERSION = "v1.2.2"


def _completion(message: dict) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }


def test_original_suites_load() -> None:
    suites = get_suites(BENCHMARK_VERSION)
    assert set(suites) == {"workspace", "travel", "banking", "slack"}


def test_openai_wrapper_tool_loop_offline() -> None:
    suite = get_suite(BENCHMARK_VERSION, "banking")
    requests: list[dict] = []
    responses = [
        _completion(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_balance", "arguments": "{}"},
                    }
                ],
            }
        ),
        _completion({"role": "assistant", "content": "Your balance is shown above."}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(requests) - 1])

    client = openai.OpenAI(
        api_key="test",
        base_url="https://example.invalid/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    llm = OpenAILLM(client, "test-model")
    pipeline = AgentPipeline(
        [
            SystemMessage("You are a banking assistant."),
            InitQuery(),
            llm,
            ToolsExecutionLoop([ToolsExecutor(), llm]),
        ]
    )

    environment = suite.load_and_inject_default_environment({})
    _, _, _, messages, _ = pipeline.query("What is my balance?", FunctionsRuntime(suite.tools), environment)

    assert len(requests) == 2
    assert any(tool["function"]["name"] == "get_balance" for tool in requests[0]["tools"])
    tool_messages = [m for m in messages if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["error"] is None
    assert messages[-1]["role"] == "assistant"
