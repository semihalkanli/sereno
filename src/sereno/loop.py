"""The agent loop: one session, one model, one tool set.

The loop keeps the conversation in the OpenAI chat format that OpenRouter
accepts. For each user turn it calls the model, runs every tool call it asks
for, and repeats until the model answers without tool calls; the step cap
covers the whole session. Every model response and tool result goes to the
event log.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from sereno.events import EventLog, new_id
from sereno.model import ChatModel
from sereno.tools import ToolOutcome, Toolset

logger = logging.getLogger("sereno.loop")


@dataclass
class SessionResult:
    reason: str
    final_text: str | None
    messages: list[dict[str, Any]]
    model_calls: int
    tool_calls: int
    cost_usd: float
    question: str | None = None
    """The answer that ended the last turn with a question to the user, when the user's reply was sent."""


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


def output_messages(message: dict[str, Any], finish_reason: str | None = None) -> list[dict[str, Any]]:
    """The assistant message in the OpenTelemetry GenAI shape: role and typed parts."""
    parts: list[dict[str, Any]] = []
    reasoning = _reasoning_text(message)
    if reasoning:
        parts.append({"type": "reasoning", "content": reasoning})
    if message.get("content"):
        parts.append({"type": "text", "content": message["content"]})
    for call in message.get("tool_calls") or []:
        parts.append(
            {
                "type": "tool_call",
                "id": call.get("id"),
                "name": call["function"]["name"],
                "arguments": call["function"].get("arguments"),
            }
        )
    out = {"role": "assistant", "parts": parts}
    if finish_reason is not None:
        out["finish_reason"] = finish_reason
    return [out]


def _usage(usage: dict[str, Any]) -> dict[str, Any]:
    completion = usage.get("completion_tokens_details") or {}
    prompt = usage.get("prompt_tokens_details") or {}
    return {
        "gen_ai.usage.input_tokens": usage.get("prompt_tokens"),
        "gen_ai.usage.output_tokens": usage.get("completion_tokens"),
        "gen_ai.usage.reasoning.output_tokens": completion.get("reasoning_tokens"),
        "gen_ai.usage.cache_read.input_tokens": prompt.get("cached_tokens"),
        "sereno.cost_usd": usage.get("cost"),
    }


def _history_message(message: dict[str, Any]) -> dict[str, Any]:
    """The assistant message as it is sent back to the model on the next call."""
    kept = {"role": "assistant", "content": message.get("content")}
    for key in ("tool_calls", "reasoning_details"):
        if message.get(key):
            kept[key] = message[key]
    return kept


def asks_user(text: str | None) -> bool:
    """Whether an answer ends with a question to the user: its last non-empty line holds a question mark."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return bool(lines) and "?" in lines[-1]


def run_session(
    model: ChatModel,
    toolset: Toolset,
    messages: list[dict[str, Any]],
    turns: list[str],
    log: EventLog,
    max_steps: int = 30,
    reply: str | None = None,
) -> SessionResult:
    """Run the user's turns in order, after `messages` (system prompt and any history).

    Each turn ends when the model answers without tool calls. When the answer to the
    last turn ends with a question (`asks_user`) and a `reply` is given, the user sends
    it once as one more turn. `max_steps` caps the model calls of the whole session;
    an error or the cap ends the session early.
    """
    schemas = toolset.schemas()
    cost = 0.0
    tool_calls_run = 0
    step = 0
    text = None
    question = None
    turns = list(turns)

    for turn, user_prompt in enumerate(turns, start=1):
        is_reply = question is not None
        with log.span("turn", f"turn {turn}", turn=turn, content=user_prompt, reply=is_reply) as span_end:
            messages.append({"role": "user", "content": user_prompt})
            log.emit("input", content=user_prompt, reply=is_reply)
            while True:
                if step >= max_steps:
                    span_end["reason"] = "max_steps"
                    return SessionResult("max_steps", None, messages, max_steps, tool_calls_run, cost, question)
                event_id = new_id()
                with log.building(event_id):
                    try:
                        completion = model.complete(messages, schemas)
                    except Exception as e:
                        logger.exception("model call failed")
                        log.error(e, step=step, id=event_id, attempts=getattr(e, "attempts", None))
                        span_end["reason"] = "error"
                        return SessionResult("error", None, messages, step, tool_calls_run, cost, question)

                message = completion.message
                calls = message.get("tool_calls") or []
                cost += completion.usage.get("cost") or 0.0
                response = completion.response
                log.emit(
                    "chat",
                    step=step,
                    id=event_id,
                    call={"request": completion.request, "response": response},
                    **{
                        "gen_ai.request.model": completion.request.get("model"),
                        "gen_ai.response.id": response.get("id"),
                        "gen_ai.response.model": response.get("model"),
                        "gen_ai.response.finish_reasons": [completion.finish_reason],
                        "gen_ai.output.messages": output_messages(message, completion.finish_reason),
                    },
                    **_usage(completion.usage),
                    duration_s=completion.latency_s,
                    retries=len(completion.attempts) - 1,
                    attempts=completion.attempts,
                )
                messages.append(_history_message(message))
                step += 1

                if not calls:
                    text = message.get("content")
                    if reply and question is None and turn == len(turns) and asks_user(text):
                        question = text
                        turns.append(reply)
                    break
                for call in calls:
                    tool_calls_run += 1
                    messages.append(run_tool_call(toolset, call, log, step - 1))

    return SessionResult("final_answer", text, messages, step, tool_calls_run, cost, question)


def run_tool_call(
    toolset: Toolset, call: dict[str, Any], log: EventLog, step: int | None, prefilled: bool = False
) -> dict[str, Any]:
    """Run one tool call on the world, log it, and return the tool message for the conversation."""
    name = call["function"]["name"]
    raw_args = call["function"].get("arguments")
    args, parse_error = _parse_args(raw_args)
    t0 = time.monotonic()
    if parse_error is None:
        outcome = toolset.call(name, args or {})
    else:
        outcome = ToolOutcome("", parse_error, {"channel": f"loop.{name}"}, False)
    duration = round(time.monotonic() - t0, 3)
    result, error = outcome.result, outcome.error
    if error:
        logger.info("tool %s returned an error: %s", name, error, extra={"tool": name})
    extra = {"prefilled": True} if prefilled else {}
    log.emit(
        "execute_tool",
        step=step,
        **{
            "gen_ai.tool.name": name,
            "gen_ai.tool.call.id": call.get("id"),
            "gen_ai.tool.call.arguments": args if parse_error is None else raw_args,
            "gen_ai.tool.call.result": None if error else result,
            "sereno.provenance": outcome.provenance,
            "sereno.state_changed": outcome.state_changed,
        },
        error=error,
        duration_s=duration,
        **extra,
    )
    if outcome.snapshot is not None:
        log.emit("state", step=step, reason=name, snapshot=outcome.snapshot)
    return {"role": "tool", "tool_call_id": call.get("id"), "content": f"Error: {error}" if error else result}
