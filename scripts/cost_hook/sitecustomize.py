"""Record the usage of every OpenRouter and Anthropic completion made by this process.

`scripts/cost.py run` puts this directory on PYTHONPATH, so Python imports it at
startup in the wrapped command and in every Python child it spawns. Each
non-streaming response from OpenRouter or the Anthropic Messages API is appended
as one JSON line to the file named by SERENO_COST_CALLS. Patching httpx (and
httpx2, which openai 3.x uses) and requests covers any client built on them,
including mini-swe's requests-based OpenRouter models, which also use the
Responses API, and litellm's Anthropic calls.

OpenRouter returns the billed cost with each response. Anthropic returns only
token counts, so the cost is computed from ANTHROPIC_PRICES, copied from
https://platform.claude.com/docs/en/about-claude/pricing on 2026-10-08. A model
missing from the table is recorded as unpriced rather than free.
"""

import importlib
import json
import os
import threading
import time
from urllib.parse import urlsplit

_CALLS_PATH = os.environ.get("SERENO_COST_CALLS")

# USD per million tokens: input, output, cache read multiplier, and for prompts over the threshold the long-prompt
# input and output prices. Cache writes cost 1.25x (5 minutes) or 2x (1 hour) the input price of the same tier.
ANTHROPIC_PRICES = {
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "read": 0.1, "long_over": 100_000, "long": (0.50, 2.50)},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "read": 0.05},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "read": 0.05},
}
# Pinning inference to the US (inference_geo "us") multiplies every token price.
US_INFERENCE = 1.1


def anthropic_cost(model, usage: dict) -> float | None:
    """The cost of one Messages API response from its usage block; None for a model without a price."""
    price = ANTHROPIC_PRICES.get(model)
    if price is None:
        return None
    uncached = usage.get("input_tokens") or 0
    read = usage.get("cache_read_input_tokens") or 0
    written = usage.get("cache_creation_input_tokens") or 0
    split = usage.get("cache_creation") or {}
    hour = split.get("ephemeral_1h_input_tokens") or 0
    minutes = split.get("ephemeral_5m_input_tokens", written - hour) or 0
    input_price, output_price = price["input"], price["output"]
    if "long_over" in price and uncached + read + written > price["long_over"]:
        input_price, output_price = price["long"]
    total = (
        input_price * (uncached + 1.25 * minutes + 2 * hour + price["read"] * read)
        + output_price * (usage.get("output_tokens") or 0)
    ) / 1_000_000
    return total * (US_INFERENCE if usage.get("inference_geo") == "us" else 1)


def _install(calls_path: str) -> None:
    lock = threading.Lock()

    def record(url, response) -> None:
        url = urlsplit(str(url))
        openrouter = url.hostname == "openrouter.ai" and url.path.endswith(("/chat/completions", "/responses"))
        anthropic = url.hostname == "api.anthropic.com" and url.path.endswith("/v1/messages")
        if not (openrouter or anthropic):
            return
        try:
            body = response.json()
        except ValueError:
            body = {}
        usage = body.get("usage") or {}
        row = {
            "ts": time.time(),
            "pid": os.getpid(),
            "status": response.status_code,
            "id": body.get("id"),
            "model": body.get("model"),
            "provider": body.get("provider"),
            "prompt_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
            "completion_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
            "cost": usage.get("cost"),
        }
        if anthropic:
            # Anthropic's input_tokens leaves out cached tokens; prompt_tokens counts the whole prompt, as OpenRouter's.
            cached = (usage.get("cache_read_input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
            billed = response.status_code == 200 and bool(usage)
            row |= {
                "provider": "anthropic",
                "prompt_tokens": (usage.get("input_tokens") or 0) + cached if usage else None,
                "cache_read_tokens": usage.get("cache_read_input_tokens"),
                "cache_write_tokens": usage.get("cache_creation_input_tokens"),
                "cost": anthropic_cost(body.get("model"), usage) if billed else None,
                "unpriced": billed and body.get("model") not in ANTHROPIC_PRICES,
            }
        with lock, open(calls_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def patch(module) -> None:
        send = module.Client.send
        async_send = module.AsyncClient.send

        def recording_send(self, request, *args, **kwargs):
            response = send(self, request, *args, **kwargs)
            if not kwargs.get("stream"):
                record(request.url, response)
            return response

        async def recording_async_send(self, request, *args, **kwargs):
            response = await async_send(self, request, *args, **kwargs)
            if not kwargs.get("stream"):
                record(request.url, response)
            return response

        module.Client.send = recording_send
        module.AsyncClient.send = recording_async_send

    def patch_requests(module) -> None:
        send = module.Session.send

        def recording_send(self, request, **kwargs):
            response = send(self, request, **kwargs)
            if not kwargs.get("stream"):
                record(request.url, response)
            return response

        module.Session.send = recording_send

    for name, patcher in (("httpx", patch), ("httpx2", patch), ("requests", patch_requests)):
        try:
            patcher(importlib.import_module(name))
        except ImportError:
            pass


if _CALLS_PATH:
    _install(_CALLS_PATH)
