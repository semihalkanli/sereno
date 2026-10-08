"""Record the usage of every OpenRouter and Anthropic completion made by this process.

`scripts/cost.py run` puts this directory on PYTHONPATH, so Python imports it at
startup in the wrapped command and in every Python child it spawns. Each
non-streaming response from OpenRouter or the Anthropic Messages API is appended
as one JSON line to the file named by SERENO_COST_CALLS. Patching httpx (and
httpx2, which openai 3.x and anthropic 1.x use) and requests covers any client built on them,
including mini-swe's requests-based OpenRouter models, which also use the
Responses API, litellm's Anthropic calls and the Anthropic SDK.

OpenRouter returns the billed cost with each response. Anthropic returns only
token counts, so the cost is computed with the official price table in
src/sereno/pricing.py, the one the context-eval Anthropic agent uses. A model
missing from the table is recorded as unpriced rather than free.
"""

import importlib
import importlib.util
import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

_CALLS_PATH = os.environ.get("SERENO_COST_CALLS")


def _pricing():
    """src/sereno/pricing.py of this checkout, loaded by path: the wrapped command may use any Python environment."""
    path = Path(__file__).resolve().parents[2] / "src" / "sereno" / "pricing.py"
    spec = importlib.util.spec_from_file_location("_sereno_cost_pricing", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install(calls_path: str) -> None:
    lock = threading.Lock()
    pricing = _pricing()

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
                "cost": pricing.anthropic_cost(body.get("model"), usage) if billed else None,
                "unpriced": billed and body.get("model") not in pricing.ANTHROPIC_PRICES,
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
