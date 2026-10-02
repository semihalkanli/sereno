"""Chat models the loop can call.

`OpenRouterModel` posts to OpenRouter's chat completions endpoint with httpx,
non-streaming, so the cost hook in `scripts/cost_hook` sees every call. The
provider is pinned with fallbacks off, as in the feasibility runs. Every
attempt is written to the `sereno.model` diagnostic logger and returned with
the completion, together with the exact request body and response JSON.
`ScriptedModel` replays fixed responses for offline tests and demos.
"""

import logging
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
RETRY_STATUS = {408, 429, 500, 502, 503, 504}

log = logging.getLogger("sereno.model")


@dataclass
class Completion:
    message: dict[str, Any]
    finish_reason: str | None
    usage: dict[str, Any]
    latency_s: float
    request: dict[str, Any]
    response: dict[str, Any]
    attempts: list[dict[str, Any]]


class ModelCallError(RuntimeError):
    """The model call failed after its attempts; `attempts` says how each one went."""

    def __init__(self, message: str, attempts: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.attempts = attempts


class ChatModel(Protocol):
    name: str
    provider: str | None
    temperature: float | None
    top_p: float | None
    reasoning_effort: str | None

    def complete(self, messages: list[dict], tools: list[dict]) -> Completion: ...


class OpenRouterModel:
    def __init__(
        self,
        name: str,
        provider: str,
        temperature: float | None = None,
        top_p: float | None = None,
        reasoning_effort: str | None = None,
        api_key: str | None = None,
        timeout_s: float = 180.0,
        max_attempts: int = 4,
    ) -> None:
        self.name = name
        self.provider = provider
        self.temperature = temperature
        self.top_p = top_p
        self.reasoning_effort = reasoning_effort
        self.max_attempts = max_attempts
        key = api_key or os.environ["OPENROUTER_API_KEY"]
        self._client = httpx.Client(headers={"Authorization": f"Bearer {key}"}, timeout=timeout_s)

    def complete(self, messages: list[dict], tools: list[dict]) -> Completion:
        body: dict[str, Any] = {
            "model": self.name,
            "messages": list(messages),
            "provider": {"order": [self.provider], "allow_fallbacks": False, "require_parameters": True},
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.top_p is not None:
            body["top_p"] = self.top_p
        if self.reasoning_effort is not None:
            body["reasoning"] = {"effort": self.reasoning_effort}

        attempts: list[dict[str, Any]] = []
        for attempt in range(1, self.max_attempts + 1):
            t0 = time.monotonic()
            try:
                response = self._client.post(OPENROUTER_URL, json=body)
            except httpx.TransportError as e:
                record = {"status": None, "duration_s": round(time.monotonic() - t0, 3), "error": repr(e)}
                attempts.append(record)
                log.warning("attempt %d transport error", attempt, extra={"attempt": attempt, **record})
                if attempt == self.max_attempts:
                    raise ModelCallError(f"OpenRouter transport error: {e!r}", attempts) from e
            else:
                duration = round(time.monotonic() - t0, 3)
                data = response.json() if response.content else {}
                if response.status_code == 200 and "choices" in data:
                    attempts.append({"status": 200, "duration_s": duration, "error": None})
                    log.info(
                        "attempt %d ok", attempt, extra={"attempt": attempt, "status": 200, "duration_s": duration}
                    )
                    choice = data["choices"][0]
                    return Completion(
                        message=choice["message"],
                        finish_reason=choice.get("finish_reason"),
                        usage=data.get("usage") or {},
                        latency_s=duration,
                        request=body,
                        response=data,
                        attempts=attempts,
                    )
                error = data.get("error") if isinstance(data, dict) and data.get("error") else response.text[:500]
                record = {"status": response.status_code, "duration_s": duration, "error": error}
                attempts.append(record)
                log.warning(
                    "attempt %d failed with %d", attempt, response.status_code, extra={"attempt": attempt, **record}
                )
                # OpenRouter reports some upstream provider failures as 200 with an error body.
                retryable = response.status_code in RETRY_STATUS or response.status_code == 200
                if not retryable or attempt == self.max_attempts:
                    raise ModelCallError(f"OpenRouter {response.status_code}: {data or response.text[:500]}", attempts)
            wait = min(2**attempt, 30)
            log.info("retrying in %ds", wait, extra={"attempt": attempt, "sleep_s": wait})
            time.sleep(wait)
        raise AssertionError("unreachable")


class ScriptedModel:
    """Returns the given assistant messages in order, one per call."""

    def __init__(self, messages: Iterable[dict[str, Any]], name: str = "scripted") -> None:
        self.name = name
        self.provider = None
        self.temperature = None
        self.top_p = None
        self.reasoning_effort = None
        self._messages = list(messages)
        self.requests: list[list[dict]] = []

    def complete(self, messages: list[dict], tools: list[dict]) -> Completion:
        self.requests.append(list(messages))
        if len(self.requests) > len(self._messages):
            raise RuntimeError("ScriptedModel ran out of responses.")
        message = {"role": "assistant", "content": None, **self._messages[len(self.requests) - 1]}
        request = {"model": self.name, "messages": list(messages), **({"tools": tools} if tools else {})}
        response = {"choices": [{"message": message, "finish_reason": "stop"}], "usage": {"cost": 0.0}}
        return Completion(
            message=message,
            finish_reason="stop",
            usage={"cost": 0.0},
            latency_s=0.0,
            request=request,
            response=response,
            attempts=[{"status": None, "duration_s": 0.0, "error": None}],
        )
