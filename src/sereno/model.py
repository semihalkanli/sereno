"""Chat models the loop can call.

`OpenRouterModel` posts to OpenRouter's chat completions endpoint with httpx,
non-streaming, so the cost hook in `scripts/cost_hook` sees every call. The
provider is pinned with fallbacks off, as in the feasibility runs.
`ScriptedModel` replays fixed responses for offline tests and demos.
"""

import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
RETRY_STATUS = {408, 429, 500, 502, 503, 504}


@dataclass
class Completion:
    message: dict[str, Any]
    finish_reason: str | None
    usage: dict[str, Any]
    latency_s: float
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


class ChatModel(Protocol):
    name: str
    provider: str | None
    temperature: float | None

    def complete(self, messages: list[dict], tools: list[dict]) -> Completion: ...


class OpenRouterModel:
    def __init__(
        self,
        name: str,
        provider: str,
        temperature: float | None = 0.0,
        api_key: str | None = None,
        timeout_s: float = 180.0,
        max_attempts: int = 4,
    ) -> None:
        self.name = name
        self.provider = provider
        self.temperature = temperature
        self.max_attempts = max_attempts
        key = api_key or os.environ["OPENROUTER_API_KEY"]
        self._client = httpx.Client(headers={"Authorization": f"Bearer {key}"}, timeout=timeout_s)

    def complete(self, messages: list[dict], tools: list[dict]) -> Completion:
        body: dict[str, Any] = {
            "model": self.name,
            "messages": messages,
            "provider": {"order": [self.provider], "allow_fallbacks": False, "require_parameters": True},
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if self.temperature is not None:
            body["temperature"] = self.temperature

        for attempt in range(1, self.max_attempts + 1):
            t0 = time.monotonic()
            try:
                response = self._client.post(OPENROUTER_URL, json=body)
            except httpx.TransportError:
                if attempt == self.max_attempts:
                    raise
            else:
                data = response.json() if response.content else {}
                if response.status_code == 200 and "choices" in data:
                    choice = data["choices"][0]
                    return Completion(
                        message=choice["message"],
                        finish_reason=choice.get("finish_reason"),
                        usage=data.get("usage") or {},
                        latency_s=round(time.monotonic() - t0, 2),
                        raw=data,
                    )
                # OpenRouter reports some upstream provider failures as 200 with an error body.
                retryable = response.status_code in RETRY_STATUS or response.status_code == 200
                if not retryable or attempt == self.max_attempts:
                    raise RuntimeError(f"OpenRouter {response.status_code}: {data or response.text[:500]}")
            time.sleep(min(2**attempt, 30))
        raise AssertionError("unreachable")


class ScriptedModel:
    """Returns the given assistant messages in order, one per call."""

    def __init__(self, messages: Iterable[dict[str, Any]], name: str = "scripted") -> None:
        self.name = name
        self.provider = None
        self.temperature = None
        self._messages = list(messages)
        self.requests: list[list[dict]] = []

    def complete(self, messages: list[dict], tools: list[dict]) -> Completion:
        self.requests.append(list(messages))
        if len(self.requests) > len(self._messages):
            raise RuntimeError("ScriptedModel ran out of responses.")
        message = {"role": "assistant", "content": None, **self._messages[len(self.requests) - 1]}
        return Completion(message=message, finish_reason="stop", usage={"cost": 0.0}, latency_s=0.0)
