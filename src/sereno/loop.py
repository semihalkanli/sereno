"""The agent loop: one session, one model, one tool set.

The loop keeps the conversation in the OpenAI chat format that OpenRouter
accepts, calls the model, runs every tool call it asks for, and repeats until
the model answers without tool calls or the step cap is reached. Every model
response and tool result goes to the event log.
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
    system_prompt: str,
    user_prompt: str,
    log: EventLog,
    max_steps: int = 30,
) -> SessionResult:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    log.emit("user_message", text=user_prompt)
    schemas = toolset.schemas()
    cost = 0.0
    tool_calls_run = 0

    for step in range(max_steps):
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

        if not calls:
            text = message.get("content")
            log.emit("session_end", step=step, reason="final_answer", final_text=text)
            return SessionResult("final_answer", text, messages, step + 1, tool_calls_run, cost)

        for call in calls:
            name = call["function"]["name"]
            args, parse_error = _parse_args(call["function"].get("arguments"))
            if parse_error is None:
                outcome = toolset.call(name, args or {})
                result, error, provenance, changed = (
                    outcome.result,
                    outcome.error,
                    outcome.provenance,
                    outcome.state_changed,
                )
            else:
                result, error, provenance, changed = "", parse_error, {"channel": f"loop.{name}"}, False
            tool_calls_run += 1
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
            )
            if changed:
                log.emit("world_state", step=step, reason=name, state=toolset.world.snapshot())
            messages.append(
                {"role": "tool", "tool_call_id": call.get("id"), "content": f"Error: {error}" if error else result}
            )

    log.emit("session_end", step=max_steps - 1, reason="max_steps", final_text=None)
    return SessionResult("max_steps", None, messages, max_steps, tool_calls_run, cost)
