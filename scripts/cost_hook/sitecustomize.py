"""Record the usage of every OpenRouter completion made by this process.

`scripts/cost.py run` puts this directory on PYTHONPATH, so Python imports it at
startup in the wrapped command and in every Python child it spawns. Each
non-streaming response from OpenRouter is appended as one JSON line to the file
named by SERENO_COST_CALLS. Patching httpx (and httpx2, which openai 3.x uses)
and requests covers any client built on them, including mini-swe's requests-based
OpenRouter models, which also use the Responses API.
"""

import importlib
import json
import os
import threading
import time
from urllib.parse import urlsplit

_CALLS_PATH = os.environ.get("SERENO_COST_CALLS")


def _install(calls_path: str) -> None:
    lock = threading.Lock()

    def record(url, response) -> None:
        url = urlsplit(str(url))
        if url.hostname != "openrouter.ai" or not url.path.endswith(("/chat/completions", "/responses")):
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
