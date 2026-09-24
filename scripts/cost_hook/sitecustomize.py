"""Record the usage of every OpenRouter chat completion made by this process.

`scripts/cost.py run` puts this directory on PYTHONPATH, so Python imports it at
startup in the wrapped command and in every Python child it spawns. Each
non-streaming response from OpenRouter is appended as one JSON line to the file
named by SERENO_COST_CALLS. Patching httpx (and httpx2, which openai 3.x uses)
covers any client built on them, including clients that third-party defenses
construct on their own.
"""

import importlib
import json
import os
import threading
import time

_CALLS_PATH = os.environ.get("SERENO_COST_CALLS")


def _install(calls_path: str) -> None:
    lock = threading.Lock()

    def record(request, response) -> None:
        if request.url.host != "openrouter.ai" or not request.url.path.endswith("/chat/completions"):
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
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
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
                record(request, response)
            return response

        async def recording_async_send(self, request, *args, **kwargs):
            response = await async_send(self, request, *args, **kwargs)
            if not kwargs.get("stream"):
                record(request, response)
            return response

        module.Client.send = recording_send
        module.AsyncClient.send = recording_async_send

    for name in ("httpx", "httpx2"):
        try:
            patch(importlib.import_module(name))
        except ImportError:
            pass


if _CALLS_PATH:
    _install(_CALLS_PATH)
