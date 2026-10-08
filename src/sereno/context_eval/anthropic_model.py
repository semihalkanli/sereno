"""Claude through the official Anthropic SDK as a mini-swe model.

The agent loop, templates, bash tool, action parsing, format errors and limits are mini-swe's, so a session sees
what mini-swe's litellm path sends. History stays in mini-swe's message format and becomes a native request on
every call: assistant turns replay the returned content blocks unchanged, a refusal ends the session, and each
reply is priced from its usage with the official table.
"""

import copy
import inspect
import json
import time
from types import SimpleNamespace
from typing import Any, Literal

import anthropic
from minisweagent.exceptions import FormatError, InterruptAgentFlow
from minisweagent.models import GLOBAL_MODEL_STATS
from minisweagent.models.utils.actions_toolcall import (
    BASH_TOOL,
    format_toolcall_observation_messages,
    parse_toolcall_actions,
)
from pydantic import BaseModel, field_validator

from sereno.pricing import anthropic_cost

# mini-swe's bash tool in the shape litellm sends it.
TOOL = {
    "name": BASH_TOOL["function"]["name"],
    "input_schema": BASH_TOOL["function"]["parameters"],
    "type": "custom",
    "description": BASH_TOOL["function"]["description"],
}
# litellm's finish_reason for each stop_reason; format_error_template tells a cut-off reply by it.
FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
    "compaction": "length",
}
# Arguments the model builds from the messages; model_kwargs may set any other argument of messages.create.
BUILT = {"model", "messages", "system", "tools", "stream"}
ARGUMENTS = set(inspect.signature(anthropic.resources.Messages.create).parameters) - {"self"} - BUILT


class Refused(InterruptAgentFlow):
    """The model declined the request (stop_reason refusal): the reply, then the exit message."""


class AnthropicModelConfig(BaseModel, extra="forbid"):
    model_name: str
    model_kwargs: dict[str, Any]
    """Passed verbatim to messages.create; max_tokens is required, unknown arguments are an error."""
    max_retries: int = 9
    """SDK retries of connection errors, 408, 409, 429 and 5xx: ten attempts, as mini-swe's default."""
    set_cache_control: Literal["default_end"] | None = "default_end"
    observation_template: str
    format_error_template: str

    @field_validator("model_kwargs")
    @classmethod
    def known_arguments(cls, kwargs: dict) -> dict:
        if unknown := sorted(set(kwargs) - ARGUMENTS):
            raise ValueError(f"not messages.create arguments: {', '.join(unknown)}")
        if "max_tokens" not in kwargs:
            raise ValueError("model_kwargs requires max_tokens")
        return kwargs


def assistant_message(response: dict) -> dict:
    """A native reply in mini-swe's message format: content keeps the returned blocks for the next request, and
    tool_calls, reasoning_content and thinking_blocks are what litellm's message carries for the readers."""
    blocks = response["content"]
    thinking = [b for b in blocks if b["type"] in ("thinking", "redacted_thinking")]
    calls = [
        {"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b["input"])}}
        for b in blocks
        if b["type"] == "tool_use"
    ]
    return {
        "role": "assistant",
        "content": blocks,
        "tool_calls": calls or None,
        "reasoning_content": "".join(b.get("thinking") or "" for b in thinking) if thinking else None,
        "thinking_blocks": thinking or None,
    }


def native_request(messages: list[dict], cache: bool) -> dict:
    """The system prompt and turns litellm builds from mini-swe messages: tool results and the user text after them
    form one user turn, and with cache control the last message carries the only breakpoint."""
    system, turns = [], []
    for index, message in enumerate(messages):
        role, content = message["role"], message["content"]
        mark = {"cache_control": {"type": "ephemeral"}} if cache and index == len(messages) - 1 else {}
        if role == "system":
            system.append({"type": "text", "text": content, **mark})
            continue
        if role == "assistant":
            blocks = copy.deepcopy(content)
            if mark:
                blocks[-1] |= mark
        elif role == "tool":
            text = [{"type": "text", "text": content}] if mark else content
            blocks = [{"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": text, **mark}]
        else:
            blocks = [{"type": "text", "text": content, **mark}]
        side = "assistant" if role == "assistant" else "user"
        if turns and turns[-1]["role"] == side:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": side, "content": blocks})
    return {"system": system, "messages": turns}


class AnthropicModel:
    def __init__(self, *, client: anthropic.Anthropic | None = None, **kwargs):
        self.config = AnthropicModelConfig(**kwargs)
        self.client = client or anthropic.Anthropic(max_retries=self.config.max_retries)

    def query(self, messages: list[dict], **kwargs) -> dict:
        response = self.client.messages.create(
            model=self.config.model_name,
            tools=[TOOL],
            **native_request(messages, self.config.set_cache_control == "default_end"),
            **(self.config.model_kwargs | kwargs),
        )
        body = response.to_dict(mode="json")
        cost = anthropic_cost(body["model"], body["usage"])
        if cost is None:
            raise RuntimeError(f"no price for model {body['model']} in sereno.pricing")
        GLOBAL_MODEL_STATS.add(cost)
        extra = {"response": body, "request_id": response._request_id, "cost": cost, "timestamp": time.time()}
        message = assistant_message(body) | {"extra": extra}
        if body["stop_reason"] == "refusal":
            exit_extra = {"exit_status": "Refused", "submission": "", "stop_details": body.get("stop_details")}
            raise Refused(message, {"role": "exit", "content": "Refused", "extra": exit_extra})
        calls = [
            SimpleNamespace(id=c["id"], function=SimpleNamespace(**c["function"])) for c in message["tool_calls"] or []
        ]
        try:
            extra["actions"] = parse_toolcall_actions(
                calls,
                format_error_template=self.config.format_error_template,
                template_kwargs={"finish_reason": FINISH_REASONS.get(body["stop_reason"], "stop")},
            )
        except FormatError as error:
            error.messages[0]["extra"].update(extra)
            raise
        return message

    def format_message(self, **kwargs) -> dict:
        return kwargs

    def format_observation_messages(
        self, message: dict, outputs: list[dict], template_vars: dict | None = None
    ) -> list[dict]:
        return format_toolcall_observation_messages(
            actions=message.get("extra", {}).get("actions", []),
            outputs=outputs,
            observation_template=self.config.observation_template,
            template_vars=template_vars,
        )

    def get_template_vars(self, **kwargs) -> dict[str, Any]:
        return self.config.model_dump()

    def serialize(self) -> dict:
        return {
            "info": {
                "config": {
                    "model": self.config.model_dump(mode="json"),
                    "model_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                },
            }
        }
