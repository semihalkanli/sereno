"""The OpenAI-compatible chat-completions adapter.

It serves OpenRouter, DeepSeek's own endpoint, GLM, a local server and any other endpoint that speaks the same
protocol. The model, the URL, the credential variable and the extra body fields are configuration; nothing here knows
which model it talks to. Reasoning fields the API returns on an assistant turn are kept and replayed verbatim on later
turns (decision 370).
"""

import copy
import json
import time
from collections.abc import Sequence

import httpx

from sereno.attacker.config import AttackerConfig
from sereno.attacker.deadline import Deadline
from sereno.attacker.errors import ProviderFailure
from sereno.attacker.providers.base import (
    AssistantTurn,
    Completion,
    SystemTurn,
    ToolCall,
    ToolSpec,
    Turn,
    UserTurn,
)
from sereno.attacker.providers.transport import Sink, describe, post

REASONING_FIELDS = ("reasoning", "reasoning_details", "reasoning_content")
FINISHES = frozenset({"stop", "tool_calls"})


def messages(turns: Sequence[Turn]) -> list[dict]:
    """The conversation in the chat-completions shape; every reasoning field of an assistant turn travels with it."""
    out: list[dict] = []
    for turn in turns:
        if isinstance(turn, SystemTurn):
            out.append({"role": "system", "content": turn.text})
        elif isinstance(turn, UserTurn):
            out.append({"role": "user", "content": turn.text})
        elif isinstance(turn, AssistantTurn):
            message: dict = {"role": "assistant", "content": turn.text or None}
            if turn.tool_calls:
                message["tool_calls"] = [tool_call(call) for call in turn.tool_calls]
            message.update(copy.deepcopy(turn.reasoning))
            out.append(message)
        else:
            out.append({"role": "tool", "tool_call_id": turn.call_id, "name": turn.name, "content": turn.text})
    return out


def tool_call(call: ToolCall) -> dict:
    return {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}


def tool_specs(tools: Sequence[ToolSpec]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
        }
        for tool in tools
    ]


def request_body(config: AttackerConfig, turns: Sequence[Turn], tools: Sequence[ToolSpec]) -> dict:
    """The provider's extra fields, the model, the conversation and the tool catalog; nothing else."""
    body = copy.deepcopy(config.settings.provider.request)
    body["model"] = config.settings.provider.model
    body["messages"] = messages(turns)
    if tools:
        body["tools"] = tool_specs(tools)
    return body


def parse_tool_calls(raw: object) -> list[ToolCall]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ProviderFailure("tool_calls is not a list", "malformed_body")
    calls = []
    for number, item in enumerate(raw):
        function = item.get("function") if isinstance(item, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        arguments = function.get("arguments") if isinstance(function, dict) else None
        if not isinstance(name, str) or not name:
            raise ProviderFailure(f"tool call {number} has no name", "malformed_body")
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, ensure_ascii=False)
        if not isinstance(arguments, str):
            raise ProviderFailure(f"tool call {number} has no argument text", "malformed_body")
        call_id = item.get("id")
        if not isinstance(call_id, str) or not call_id:
            call_id = f"call_{number + 1}"
        calls.append(ToolCall(call_id, name, arguments))
    return calls


def parse_completion(answer: bytes, expected_provider: str | None) -> Completion:
    """A usable answer, or the explicit failure its body amounts to."""
    try:
        body = json.loads(answer.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ProviderFailure("the response body is not valid JSON", "malformed_body") from None
    if not isinstance(body, dict):
        raise ProviderFailure("the response body is not a JSON object", "malformed_body")
    if body.get("error"):
        raise ProviderFailure(f"error in the response body: {describe(answer)}", "body_error")
    provider = body.get("provider")
    if expected_provider is not None and provider != expected_provider:
        raise ProviderFailure(f"answered by {provider!r}, not {expected_provider!r}", "unexpected_provider")
    choices = body.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise ProviderFailure("the response holds no choice with a message", "malformed_body")
    message, finish = choice["message"], choice.get("finish_reason")
    declined = bool(message.get("refusal")) or finish == "content_filter"
    if finish == "length" and not declined:
        raise ProviderFailure("the answer was cut off at the length limit", "truncated")
    if finish not in FINISHES and not declined:
        raise ProviderFailure(f"the answer ended with finish_reason {finish!r}", "unexpected_finish")
    calls = [] if declined else parse_tool_calls(message.get("tool_calls"))
    text = message.get("content")
    if text is None:
        text = ""
    if not isinstance(text, str):
        raise ProviderFailure("the answer's content is not text", "malformed_body")
    if not text.strip() and not calls and not declined:
        raise ProviderFailure("the completion is empty", "empty_completion")
    usage = body.get("usage")
    return Completion(
        text=text,
        tool_calls=tuple(calls),
        reasoning={key: copy.deepcopy(message[key]) for key in REASONING_FIELDS if key in message},
        finish_reason=finish,
        usage=usage if isinstance(usage, dict) else {},
        model=body.get("model"),
        provider=provider if isinstance(provider, str) else None,
        generation_id=body.get("id"),
        declined=declined,
    )


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def __init__(
        self,
        config: AttackerConfig,
        deadline: Deadline,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
        sink: Sink | None = None,
    ):
        self.config, self.deadline, self.sleep, self.sink = config, deadline, sleep, sink
        self.key = config.credential()
        self.client = httpx.Client(transport=transport, follow_redirects=False)

    def close(self) -> None:
        self.client.close()

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self.config.settings.provider.headers}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        return headers

    def complete(self, turns: Sequence[Turn], tools: Sequence[ToolSpec], call: int = 1) -> Completion:
        body = request_body(self.config, turns, tools)
        _, answer = post(
            self.client,
            self.config.settings.provider.base_url,
            self.headers(),
            body,
            self.config.settings.http,
            self.deadline,
            sleep=self.sleep,
            sink=self.sink,
            call=call,
        )
        return parse_completion(answer, self.config.settings.provider.expected_provider)
