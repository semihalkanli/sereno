"""The HTTP layer both adapters share: attempts, retries and the shared deadline.

One logical call is at most `attempts` transport attempts, all inside the invocation's deadline. A transient status
or a connection failure is retried within that budget; every other trouble (missing credential, authentication,
balance, a body-level error under HTTP 200, a refusal, a cut-off, an empty answer) is an explicit failure that is
never retried or worked around. An ambiguous transport failure can be billed twice, so every attempt is recorded and
a request is never described as exactly-once.
"""

import email.utils
import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from sereno.attacker.config import HttpSettings
from sereno.attacker.deadline import Deadline, DeadlineHit, alarm
from sereno.attacker.errors import ProviderFailure


@dataclass
class Attempt:
    """One transport attempt, as recorded: no credential, the exact bytes' hashes."""

    call: int
    attempt: int
    started_at: str
    duration_seconds: float
    request_sha256: str
    http_status: int | None = None
    error: str | None = None
    response_sha256: str | None = None
    retry_after: str | None = None

    def record(self) -> dict:
        return dict(vars(self))


Sink = Callable[[Attempt, bytes, bytes | None], None]


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def encode(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def describe(raw: bytes) -> str:
    """The provider's error message when the body is JSON, else the first characters of the body."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw[:200].decode("utf-8", "replace")
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or "unspecified")[:300]
        if isinstance(error, str):
            return error[:300]
        message = body.get("message")
        if isinstance(message, str):
            return message[:300]
    return raw[:200].decode("utf-8", "replace")


def retry_wait(header: str | None, default: float, cap: float) -> float:
    """Seconds before the next attempt: Retry-After (seconds or an HTTP date) capped, else the default."""
    if header is None:
        return default
    seconds = None
    try:
        seconds = float(header)
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(header)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            seconds = (parsed.replace(tzinfo=parsed.tzinfo or UTC) - datetime.now(UTC)).total_seconds()
    if seconds is None or not math.isfinite(seconds):
        return default
    return min(max(seconds, 0.0), cap)


def post(
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    body: dict,
    settings: HttpSettings,
    deadline: Deadline,
    *,
    sleep: Callable[[float], None] = time.sleep,
    sink: Sink | None = None,
    call: int = 1,
) -> tuple[int, bytes]:
    """One logical call: at most `settings.attempts` transport attempts inside the deadline. Returns status and body
    of the first HTTP 200; every other end raises `ProviderFailure`."""
    raw = encode(body)
    digest = sha256(raw)
    for number in range(1, settings.attempts + 1):
        remaining = deadline.remaining()
        if remaining <= 0:
            raise ProviderFailure("the shared deadline ended before the call", "deadline")
        attempt = Attempt(call, number, datetime.now(UTC).isoformat(), 0.0, digest)
        started = time.monotonic()
        timeout = httpx.Timeout(
            connect=min(settings.connect_timeout_seconds, remaining),
            read=min(settings.response_timeout_seconds, remaining),
            write=min(settings.response_timeout_seconds, remaining),
            pool=remaining,
        )
        try:
            with alarm(remaining):
                response = client.post(url, content=raw, headers=headers, timeout=timeout)
        except DeadlineHit:
            attempt.error = "the shared deadline ended during the call"
            note(attempt, started, raw, None, sink)
            raise ProviderFailure(attempt.error, "deadline") from None
        except httpx.HTTPError as error:
            attempt.error = f"{type(error).__name__}: {error}"[:300]
            note(attempt, started, raw, None, sink)
            if number == settings.attempts:
                raise ProviderFailure(f"{attempt.error} after {number} attempts", "transport") from None
            wait(settings.retry_delay_seconds, "transport", deadline, sleep)
            continue
        answer = response.content
        attempt.http_status, attempt.response_sha256 = response.status_code, sha256(answer)
        attempt.retry_after = response.headers.get("retry-after")
        note(attempt, started, raw, answer, sink)
        status = response.status_code
        if status == 200:
            return status, answer
        if status in settings.retry_statuses:
            if number == settings.attempts:
                raise ProviderFailure(f"HTTP {status} after {number} attempts: {describe(answer)}", "http_transient")
            wait(
                retry_wait(attempt.retry_after, settings.retry_delay_seconds, settings.retry_after_max_seconds),
                f"HTTP {status}",
                deadline,
                sleep,
            )
            continue
        code = {401: "auth", 403: "auth", 402: "balance"}.get(status, f"http_{status}")
        raise ProviderFailure(f"HTTP {status}: {describe(answer)}", code)
    raise ProviderFailure("no transport attempt was made", "transport")  # unreachable with attempts >= 1


def note(attempt: Attempt, started: float, request: bytes, response: bytes | None, sink: Sink | None) -> None:
    attempt.duration_seconds = round(time.monotonic() - started, 3)
    if sink:
        sink(attempt, request, response)


def wait(seconds: float, reason: str, deadline: Deadline, sleep: Callable[[float], None]) -> None:
    """Wait before a retry only while the deadline leaves room for the wait and another attempt."""
    if seconds >= deadline.remaining():
        raise ProviderFailure(f"{reason}: no time left in the shared deadline to retry", "deadline")
    sleep(seconds)
