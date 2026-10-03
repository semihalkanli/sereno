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
TIMEOUT_S = 180.0
MAX_ATTEMPTS = 4

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

    def complete(self, messages: list[dict], tools: list[dict]) -> Completion: ...


class OpenRouterModel:
    def __init__(
        self,
        name: str,
        provider: str,
        api_key: str | None = None,
    ) -> None:
        self.name = name
        self.provider = provider
        key = api_key or os.environ["OPENROUTER_API_KEY"]
        self._client = httpx.Client(headers={"Authorization": f"Bearer {key}"}, timeout=TIMEOUT_S)

    def complete(self, messages: list[dict], tools: list[dict]) -> Completion:
        body: dict[str, Any] = {
            "model": self.name,
            "messages": list(messages),
            "provider": {"order": [self.provider], "allow_fallbacks": False, "require_parameters": True},
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"

        attempts: list[dict[str, Any]] = []
        for attempt in range(1, MAX_ATTEMPTS + 1):
            t0 = time.monotonic()
            try:
                response = self._client.post(OPENROUTER_URL, json=body)
            except httpx.TransportError as e:
                record = {"status": None, "duration_s": round(time.monotonic() - t0, 3), "error": repr(e)}
                attempts.append(record)
                log.warning("attempt %d transport error", attempt, extra={"attempt": attempt, **record})
                if attempt == MAX_ATTEMPTS:
                    raise ModelCallError(f"OpenRouter transport error: {e!r}", attempts) from e
            else:
                duration = round(time.monotonic() - t0, 3)
                try:
                    data = response.json() if response.content else {}
                except ValueError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                choices = data.get("choices")
                choice = choices[0] if isinstance(choices, list) and choices else None
                complete = (
                    isinstance(choice, dict)
                    and isinstance(choice.get("message"), dict)
                    and not choice.get("error")
                    and choice.get("finish_reason") != "error"
                )
                if response.status_code == 200 and complete:
                    attempts.append({"status": 200, "duration_s": duration, "error": None})
                    log.info(
                        "attempt %d ok", attempt, extra={"attempt": attempt, "status": 200, "duration_s": duration}
                    )
                    return Completion(
                        message=choice["message"],
                        finish_reason=choice.get("finish_reason"),
                        usage=data.get("usage") or {},
                        latency_s=duration,
                        request=body,
                        response=data,
                        attempts=attempts,
                    )
                if data.get("error"):
                    error = data["error"]
                elif isinstance(choice, dict) and choice.get("error"):
                    error = choice["error"]
                else:
                    error = response.text[:500]
                record = {"status": response.status_code, "duration_s": duration, "error": error}
                attempts.append(record)
                log.warning(
                    "attempt %d failed with %d", attempt, response.status_code, extra={"attempt": attempt, **record}
                )
                # OpenRouter reports some upstream provider failures as 200 with an error body, or with an
                # error inside the choice; a pinned provider's 5xx (Z.AI's 520, say) is as transient.
                retryable = (
                    response.status_code in RETRY_STATUS or response.status_code >= 500 or response.status_code == 200
                )
                if not retryable or attempt == MAX_ATTEMPTS:
                    raise ModelCallError(f"OpenRouter {response.status_code}: {data or response.text[:500]}", attempts)
            wait = min(2**attempt, 30)
            log.info("retrying in %ds", wait, extra={"attempt": attempt, "sleep_s": wait})
            time.sleep(wait)
        raise AssertionError("unreachable")


class ScriptedModel:
    """Returns the given assistant messages in order, one per call."""

    def __init__(self, messages: Iterable[dict[str, Any]]) -> None:
        self.name = "scripted"
        self.provider = None
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
