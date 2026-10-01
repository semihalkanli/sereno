"""The agent loop: one session, one model, one tool set.

The loop keeps the conversation in the OpenAI chat format that OpenRouter
accepts. For each user turn it calls the model, runs every tool call it asks
for, and repeats until the model answers without tool calls; the step cap
covers the whole session. Every model response and tool result goes to the
event log.
"""

import json
from dataclasses import dataclass
from typing import Any

from sereno.events import EventLog
from sereno.model import ChatModel
from sereno.tools import Toolset


@dataclass
class SessionResult:
    reason: str
    final_text: str | None
    messages: list[dict[str, Any]]
    model_calls: int
    tool_calls: int
    cost_usd: float


def _parse_args(raw: str | None) -> tuple[dict[str, Any] | None, str | None]:
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        return None, f"Arguments are not valid JSON: {e}"
    if not isinstance(args, dict):
        return None, "Arguments must be a JSON object."
    return args, None


def _reasoning_text(message: dict[str, Any]) -> str | None:
    if message.get("reasoning"):
        return message["reasoning"]
    parts = [d.get("text") for d in message.get("reasoning_details") or [] if d.get("type") == "reasoning.text"]
    return "\n".join(p for p in parts if p) or None


def _usage(usage: dict[str, Any]) -> dict[str, Any]:
    details = usage.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": details.get("reasoning_tokens"),
        "cost": usage.get("cost"),
    }


def _history_message(message: dict[str, Any]) -> dict[str, Any]:
    """The assistant message as it is sent back to the model on the next call."""
    kept = {"role": "assistant", "content": message.get("content")}
    for key in ("tool_calls", "reasoning_details"):
        if message.get(key):
            kept[key] = message[key]
    return kept


def run_session(
    model: ChatModel,
    toolset: Toolset,
    messages: list[dict[str, Any]],
    turns: list[str],
    log: EventLog,
    max_steps: int = 30,
) -> SessionResult:
    """Run the user's turns in order, after `messages` (system prompt and any history).

    Each turn ends when the model answers without tool calls. `max_steps` caps the
    model calls of the whole session; an error or the cap ends the session early.
    """
    schemas = toolset.schemas()
    cost = 0.0
    tool_calls_run = 0
    step = 0
    text = None

    for turn, user_prompt in enumerate(turns, start=1):
        log.turn = turn
        messages.append({"role": "user", "content": user_prompt})
        log.emit("user_message", text=user_prompt)
        while True:
            if step >= max_steps:
                log.emit("session_end", step=max_steps - 1, reason="max_steps", final_text=None)
                return SessionResult("max_steps", None, messages, max_steps, tool_calls_run, cost)
            try:
                completion = model.complete(messages, schemas)
            except Exception as e:
                log.emit("session_end", step=step, reason="error", final_text=None, error=repr(e))
                return SessionResult("error", None, messages, step, tool_calls_run, cost)

            message = completion.message
            calls = message.get("tool_calls") or []
            cost += completion.usage.get("cost") or 0.0
            log.emit(
                "model_response",
                step=step,
                text=message.get("content"),
                reasoning=_reasoning_text(message),
                tool_calls=[
                    {"id": c.get("id"), "name": c["function"]["name"], "args": c["function"].get("arguments")}
                    for c in calls
                ],
                finish_reason=completion.finish_reason,
                usage=_usage(completion.usage),
                latency_s=completion.latency_s,
            )
            messages.append(_history_message(message))
            step += 1

            if not calls:
                text = message.get("content")
                break
            for call in calls:
                tool_calls_run += 1
                messages.append(run_tool_call(toolset, call, log, step - 1))

    log.emit("session_end", step=step - 1, reason="final_answer", final_text=text)
    return SessionResult("final_answer", text, messages, step, tool_calls_run, cost)


def run_tool_call(
    toolset: Toolset, call: dict[str, Any], log: EventLog, step: int | None, prefilled: bool = False
) -> dict[str, Any]:
    """Run one tool call on the world, log it, and return the tool message for the conversation."""
    name = call["function"]["name"]
    args, parse_error = _parse_args(call["function"].get("arguments"))
    if parse_error is None:
        outcome = toolset.call(name, args or {})
        result, error, provenance, changed = outcome.result, outcome.error, outcome.provenance, outcome.state_changed
    else:
        result, error, provenance, changed = "", parse_error, {"channel": f"loop.{name}"}, False
    extra = {"prefilled": True} if prefilled else {}
    log.emit(
        "tool_result",
        step=step,
        provenance=provenance,
        call_id=call.get("id"),
        name=name,
        args=args,
        result=result,
        error=error,
        state_changed=changed,
        **extra,
    )
    if changed:
        log.emit("world_state", step=step, reason=name, state=toolset.world.snapshot())
    return {"role": "tool", "tool_call_id": call.get("id"), "content": f"Error: {error}" if error else result}
