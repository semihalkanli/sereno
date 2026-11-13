"""The Anthropic Messages API adapter.

The conversation maps to content blocks: an assistant turn carries its thinking and tool_use blocks exactly as the API
returned them, and each tool result is a user turn's tool_result block. The adapter is tested with a scripted
transport only; nothing in this package starts an Anthropic call on its own.
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

FINISHES = frozenset({"end_turn", "stop_sequence", "tool_use"})
RAW_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking", "tool_use"})


def arguments_object(text: str) -> dict:
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def append(messages: list[dict], role: str, blocks: list[dict]) -> None:
    """Adjacent turns of one role are one message: every tool result sits right after the assistant's tool_use."""
    if messages and messages[-1]["role"] == role and not any(block.get("type") == "text" for block in blocks):
        messages[-1]["content"].extend(blocks)
    else:
        messages.append({"role": role, "content": blocks})


def messages(turns: Sequence[Turn]) -> list[dict]:
    out: list[dict] = []
    for turn in turns:
        if isinstance(turn, SystemTurn):
            continue
        if isinstance(turn, UserTurn):
            append(out, "user", [{"type": "text", "text": turn.text}])
        elif isinstance(turn, AssistantTurn):
            raw = copy.deepcopy(turn.reasoning.get("blocks") or [])
            if raw:
                append(out, "assistant", raw)
                continue
            blocks: list[dict] = []
            if turn.text:
                blocks.append({"type": "text", "text": turn.text})
            for call in turn.tool_calls:
                blocks.append(
                    {"type": "tool_use", "id": call.id, "name": call.name, "input": arguments_object(call.arguments)}
                )
            append(out, "assistant", blocks)
        else:
            block: dict = {"type": "tool_result", "tool_use_id": turn.call_id, "content": turn.text}
            if turn.failed:
                block["is_error"] = True
            append(out, "user", [block])
    return out


def request_body(config: AttackerConfig, turns: Sequence[Turn], tools: Sequence[ToolSpec]) -> dict:
    body = copy.deepcopy(config.settings.provider.request)
    body["model"] = config.settings.provider.model
    body["system"] = "\n\n".join(turn.text for turn in turns if isinstance(turn, SystemTurn))
    body["messages"] = messages(turns)
    if tools:
        body["tools"] = [
            {"name": tool.name, "description": tool.description, "input_schema": tool.parameters} for tool in tools
        ]
    return body


def parse_completion(answer: bytes) -> Completion:
    try:
        body = json.loads(answer.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ProviderFailure("the response body is not valid JSON", "malformed_body") from None
    if not isinstance(body, dict):
        raise ProviderFailure("the response body is not a JSON object", "malformed_body")
    if body.get("type") == "error" or body.get("error"):
        raise ProviderFailure(f"error in the response body: {describe(answer)}", "body_error")
    content = body.get("content")
    if not isinstance(content, list):
        raise ProviderFailure("the response holds no content list", "malformed_body")
    blocks = [block for block in content if isinstance(block, dict)]
    texts = [block["text"] for block in blocks if block.get("type") == "text" and isinstance(block.get("text"), str)]
    calls = []
    for block in blocks:
        if block.get("type") != "tool_use":
            continue
        call_id, name = block.get("id"), block.get("name")
        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
            raise ProviderFailure("a tool_use block has no id or name", "malformed_body")
        calls.append(ToolCall(call_id, name, json.dumps(block.get("input") or {}, ensure_ascii=False)))
    finish = body.get("stop_reason")
    if finish == "max_tokens":
        raise ProviderFailure("the answer was cut off at the length limit", "truncated")
    declined = finish == "refusal"
    if finish not in FINISHES and not declined:
        raise ProviderFailure(f"the answer ended with stop_reason {finish!r}", "unexpected_finish")
    text = "\n".join(texts)
    if not text.strip() and not calls and not declined:
        raise ProviderFailure("the completion is empty", "empty_completion")
    raw_usage = body.get("usage")
    usage: dict = raw_usage if isinstance(raw_usage, dict) else {}
    prompt, completion = usage.get("input_tokens"), usage.get("output_tokens")
    creation = usage.get("cache_creation_input_tokens", 0)
    cached = usage.get("cache_read_input_tokens", 0)
    if all(isinstance(value, int) and not isinstance(value, bool) for value in (prompt, completion, creation, cached)):
        prompt += creation + cached
        usage = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
    else:
        usage = {}
    keeps_raw = any(block.get("type") in RAW_BLOCK_TYPES for block in blocks)
    return Completion(
        text=text,
        tool_calls=tuple(calls),
        reasoning={"blocks": copy.deepcopy(blocks)} if keeps_raw else {},
        finish_reason=finish,
        usage=usage,
        model=body.get("model"),
        provider="anthropic",
        generation_id=body.get("id"),
        declined=declined,
    )


class AnthropicProvider:
    name = "anthropic"

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
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": self.config.settings.provider.api_version,
            **self.config.settings.provider.headers,
        }
        if self.key:
            headers["x-api-key"] = self.key
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
        return parse_completion(answer)
