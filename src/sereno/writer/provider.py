"""The OpenRouter chat-completions client of the writer.

One logical call is at most `attempts` transport attempts, all inside the program's shared deadline. A transient HTTP
status or a connection failure is retried within that budget; every other trouble (missing credential, authentication,
balance, a body-level error under HTTP 200, an unexpected provider, a refusal, a cut-off, an empty answer) is an
explicit failure that is never retried or worked around. An ambiguous transport failure can be billed twice, so every
attempt is recorded and a request is never described as exactly-once.
"""

import copy
import email.utils
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from sereno.writer.config import WriterConfig
from sereno.writer.errors import ProviderFailure

REASONING_FIELDS = ("reasoning_details", "reasoning", "reasoning_content")


class Deadline:
    """The program's wall clock: everything the writer does has to end before it."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic):
        self.clock, self.end = clock, clock() + seconds

    def remaining(self) -> float:
        return self.end - self.clock()


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


@dataclass
class Completion:
    """What a usable answer holds. `reasoning` keeps the provider's reasoning fields exactly as returned."""

    content: str
    reasoning: dict
    finish_reason: str
    native_finish_reason: str | None
    model: str | None
    provider: str
    generation_id: str | None
    usage: dict
    body: dict = field(repr=False)

    def record(self) -> dict:
        return {
            "finish_reason": self.finish_reason,
            "native_finish_reason": self.native_finish_reason,
            "returned_model": self.model,
            "provider": self.provider,
            "generation_id": self.generation_id,
            "usage": self.usage,
            "cost": self.usage.get("cost"),
            "reasoning_fields": sorted(self.reasoning),
        }

    def assistant_message(self) -> dict:
        """The turn as a later request carries it: the content and every reasoning field, byte for byte."""
        return {"role": "assistant", "content": self.content, **copy.deepcopy(self.reasoning)}


def request_body(api: dict, messages: list[dict]) -> dict:
    """The API settings with the messages and nothing else."""
    return {**copy.deepcopy(api), "messages": messages}


def encode(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def parse_completion(body: object, expected_provider: str) -> Completion:
    """A usable answer, or the explicit failure its body amounts to."""
    if not isinstance(body, dict):
        raise ProviderFailure("the response body is not a JSON object", "malformed_body")
    if body.get("error"):
        raise ProviderFailure(f"error in the response body: {describe(body['error'])}", "body_error")
    choices = body.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise ProviderFailure("the response holds no choice with a message", "malformed_body")
    if choice.get("error"):
        raise ProviderFailure(f"error in the choice: {describe(choice['error'])}", "body_error")
    provider = body.get("provider")
    if provider != expected_provider:
        raise ProviderFailure(f"answered by {provider!r}, not {expected_provider!r}", "unexpected_provider")
    message, finish = choice["message"], choice.get("finish_reason")
    if message.get("refusal") or finish == "content_filter":
        raise ProviderFailure("the provider refused", "refusal")
    if finish == "length":
        raise ProviderFailure("the answer was cut off at the length limit", "truncated")
    if finish != "stop":
        raise ProviderFailure(f"the answer ended with finish_reason {finish!r}", "unexpected_finish")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ProviderFailure("the completion is empty", "empty_completion")
    usage = body.get("usage")
    return Completion(
        content=content,
        reasoning={key: copy.deepcopy(message[key]) for key in REASONING_FIELDS if key in message},
        finish_reason=finish,
        native_finish_reason=choice.get("native_finish_reason"),
        model=body.get("model"),
        provider=expected_provider,
        generation_id=body.get("id"),
        usage=usage if isinstance(usage, dict) else {},
        body=body,
    )


def describe(error: object) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "unspecified")[:300]
    return str(error)[:300]


def retry_wait(header: str | None, default: float, cap: float) -> float:
    """Seconds to wait before the next attempt: Retry-After (seconds or an HTTP date) capped, else the default."""
    if header is None:
        return default
    try:
        seconds = float(header)
    except ValueError:
        parsed = email.utils.parsedate_to_datetime(header) if email.utils.parsedate(header) else None
        seconds = (parsed - datetime.now(UTC)).total_seconds() if parsed else default
    return min(max(seconds, 0.0), cap)


Sink = Callable[[Attempt, bytes, bytes | None], None]


class Provider:
    """The client of one program run. `transport` and `sleep` exist so tests can replace the network."""

    def __init__(
        self,
        config: WriterConfig,
        deadline: Deadline,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        sink: Sink | None = None,
    ):
        self.config, self.deadline, self.sleep, self.sink = config, deadline, sleep, sink
        self.key = config.credential()
        self.client = httpx.Client(transport=transport, follow_redirects=False)
        self.attempts: list[Attempt] = []

    def close(self) -> None:
        self.client.close()

    def complete(self, messages: list[dict], call: int) -> Completion:
        """One logical call: at most `attempts` transport attempts within the deadline."""
        control = self.config.control
        raw = encode(request_body(self.config.api, messages))
        digest = sha256(raw)
        for number in range(1, control.attempts + 1):
            remaining = self.deadline.remaining()
            if remaining <= 0:
                raise ProviderFailure("the shared deadline ended before the call", "deadline")
            attempt = Attempt(call, number, datetime.now(UTC).isoformat(), 0.0, digest)
            started, answer = time.monotonic(), None
            timeout = httpx.Timeout(
                connect=min(control.connect_timeout, remaining),
                read=min(control.response_timeout, remaining),
                write=min(control.response_timeout, remaining),
                pool=remaining,
            )
            try:
                response = self.client.post(
                    self.config.endpoint,
                    content=raw,
                    headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
                    timeout=timeout,
                )
            except httpx.HTTPError as error:
                attempt.error = f"{type(error).__name__}: {error}"[:300]
                self.note(attempt, started, raw, None)
                if number == control.attempts:
                    raise ProviderFailure(f"{attempt.error} after {number} attempts", "transport") from None
                self.wait(control.retry_delay, "transport")
                continue
            answer = response.content
            attempt.http_status, attempt.response_sha256 = response.status_code, sha256(answer)
            attempt.retry_after = response.headers.get("retry-after")
            self.note(attempt, started, raw, answer)
            status = response.status_code
            if status == 200:
                try:
                    body = json.loads(answer.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    raise ProviderFailure("the response body is not valid JSON", "malformed_body") from None
                return parse_completion(body, self.config.api["provider"]["order"][0])
            if status in control.retry_statuses:
                if number == control.attempts:
                    raise ProviderFailure(f"HTTP {status} after {number} attempts", "http_transient")
                self.wait(
                    retry_wait(attempt.retry_after, control.retry_delay, control.retry_after_max), f"HTTP {status}"
                )
                continue
            code = {401: "auth", 403: "auth", 402: "balance"}.get(status, f"http_{status}")
            raise ProviderFailure(f"HTTP {status}", code)
        raise ProviderFailure("no transport attempt was made", "transport")  # unreachable with attempts >= 1

    def note(self, attempt: Attempt, started: float, request: bytes, response: bytes | None) -> None:
        attempt.duration_seconds = round(time.monotonic() - started, 3)
        self.attempts.append(attempt)
        if self.sink:
            self.sink(attempt, request, response)

    def wait(self, seconds: float, reason: str) -> None:
        """Wait before a retry only while the deadline leaves room for the wait and another attempt."""
        if seconds >= self.deadline.remaining():
            raise ProviderFailure(f"{reason}: no time left in the shared deadline to retry", "deadline")
        self.sleep(seconds)
