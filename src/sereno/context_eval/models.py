"""OpenRouter transport compatible with Sereno's existing per-call cost hook."""

import json

import httpx
from minisweagent.models.openrouter_model import (
    OpenRouterAPIError,
    OpenRouterAuthenticationError,
    OpenRouterModel,
    OpenRouterRateLimitError,
)
from minisweagent.models.utils.actions_toolcall import BASH_TOOL


class TrackedOpenRouterModel(OpenRouterModel):
    """Retain upstream parsing/retries/costs using the project's instrumented httpx transport."""

    def _query(self, messages, **kwargs):
        payload = {
            "model": self.config.model_name,
            "messages": messages,
            "tools": [BASH_TOOL],
            "usage": {"include": True},
            **(self.config.model_kwargs | kwargs),
        }
        try:
            response = httpx.post(
                self._api_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
                timeout=60,
            )
        except httpx.RequestError as error:
            raise OpenRouterAPIError(f"Request failed: {type(error).__name__}") from error
        if response.status_code == 401:
            raise OpenRouterAuthenticationError("OpenRouter authentication failed")
        if response.status_code == 429:
            raise OpenRouterRateLimitError("OpenRouter rate limit exceeded")
        if not response.is_success:
            raise OpenRouterAPIError(f"OpenRouter HTTP {response.status_code}")
        try:
            return response.json()
        except json.JSONDecodeError as error:
            raise OpenRouterAPIError("OpenRouter returned invalid JSON") from error
