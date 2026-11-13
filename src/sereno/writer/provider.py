"""The chat-completions client of the writer, on OpenRouter or the opencode Go API.

One logical call is at most `attempts` transport attempts, all inside the program's shared deadline. A transient HTTP
status or a connection failure is retried within that budget; every other trouble (missing credential, authentication,
balance, a body-level error under HTTP 200, an unexpected provider, a cut-off, an empty answer) is an explicit failure.
Live mode may expose declines for bounded plain resampling; other writer modes still treat refusals as failures.
An ambiguous transport failure can be billed twice, so every attempt is recorded and a request is never described as
exactly-once.

The provider kind is the design's API settings: OpenRouter checks the serving provider and bills `usage.cost`; opencode
sends no routing block, identifies itself with its own user agent, carries one `x-opencode-session` per conversation and
reports its charge as `usage.opencode.allowance` against the subscription.
"""

import contextlib
import copy
import email.utils
import hashlib
import json
import math
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx

from sereno.usage import allowance_of, billing_source_of, usage_charge
from sereno.writer.config import WriterConfig
from sereno.writer.errors import ConfigError, ProviderFailure

REASONING_FIELDS = ("reasoning_details", "reasoning", "reasoning_content")


class DeadlineHit(BaseException):
    """Raised by the alarm when the shared deadline ends inside a provider call. A BaseException, so no library code
    between the alarm and the call swallows it."""


@contextlib.contextmanager
def alarm(seconds: float):
    """End a blocking call at the deadline. httpx's timeouts apply to each read, so a provider that sends bytes slowly
    could otherwise hold one call past the deadline. Needs the main thread; elsewhere only httpx's timeouts apply."""
    if not hasattr(signal, "setitimer") or threading.current_thread() is not threading.main_thread():
        yield
        return

    def fire(signum, frame):
        raise DeadlineHit

    previous = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, max(seconds, 0.001))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


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
    """What a usable answer holds. `reasoning` keeps the provider's reasoning fields exactly as returned; `cost` is the
    charge the usage block reports, the opencode allowance included, and `allowance` says whether it was one."""

    content: str
    reasoning: dict
    finish_reason: str | None
    native_finish_reason: str | None
    model: str | None
    provider: str
    generation_id: str | None
    usage: dict
    body: dict = field(repr=False)
    declined: bool = False
    provider_kind: str = "openrouter"
    requested_model: str | None = None
    billing_source: str | None = None

    def record(self) -> dict:
        return {
            "declined": self.declined,
            "finish_reason": self.finish_reason,
            "native_finish_reason": self.native_finish_reason,
            "provider_kind": self.provider_kind,
            "requested_model": self.requested_model,
            "returned_model": self.model,
            "provider": self.provider,
            "generation_id": self.generation_id,
            "billing_source": self.billing_source,
            "usage": self.usage,
            "cost": usage_charge(self.usage),
            "allowance": allowance_of(self.usage),
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


def conversation_id(payload: dict) -> str:
    """One stable id per writer conversation, for APIs that route and cache by session: the payload's identity, hashed.
    A resumed step rebuilds the same payload, so it reuses the id; no path enters it."""
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def parse_completion(
    body: object,
    expected_provider: str | None,
    *,
    allow_declines: bool = False,
    provider_kind: str = "openrouter",
    provider_name: str | None = None,
    requested_model: str | None = None,
) -> Completion:
    """A usable answer, or the explicit failure its body amounts to. A serving provider is checked only where the API
    names one (OpenRouter); opencode's label comes from the configuration."""
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
    if expected_provider is not None and provider != expected_provider:
        raise ProviderFailure(f"answered by {provider!r}, not {expected_provider!r}", "unexpected_provider")
    message, finish = choice["message"], choice.get("finish_reason")
    declined = bool(message.get("refusal")) or finish == "content_filter"
    if declined and not allow_declines:
        raise ProviderFailure("the provider refused", "refusal")
    if finish == "length" and not declined:
        raise ProviderFailure("the answer was cut off at the length limit", "truncated")
    if finish != "stop" and not declined:
        raise ProviderFailure(f"the answer ended with finish_reason {finish!r}", "unexpected_finish")
    content = message.get("content")
    if declined and content is None:
        content = ""
    if not isinstance(content, str) or (not content.strip() and not declined):
        raise ProviderFailure("the completion is empty", "empty_completion")
    usage = body.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return Completion(
        content=content,
        reasoning={key: copy.deepcopy(message[key]) for key in REASONING_FIELDS if key in message},
        finish_reason=finish,
        native_finish_reason=choice.get("native_finish_reason"),
        model=body.get("model"),
        provider=provider_name or expected_provider or provider_kind,
        generation_id=body.get("id"),
        usage=usage,
        body=body,
        declined=declined,
        provider_kind=provider_kind,
        requested_model=requested_model,
        billing_source=billing_source_of(body),
    )


def describe(error: object) -> str:
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "unspecified")[:300]
    return str(error)[:300]


def retry_wait(header: str | None, default: float, cap: float) -> float:
    """Seconds to wait before the next attempt: Retry-After (seconds or an HTTP date) capped, else the default. A
    header that does not give a finite number of seconds is the default."""
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
        allow_declines: bool = False,
        session_id: str | None = None,
    ):
        self.config, self.deadline, self.sleep, self.sink = config, deadline, sleep, sink
        self.allow_declines = allow_declines
        self.session_id = session_id
        if config.session_header and not session_id:
            raise ConfigError(f"the {config.provider_kind} provider needs a conversation id", "session")
        self.key = config.credential()
        self.client = httpx.Client(transport=transport, follow_redirects=False)
        self.attempts: list[Attempt] = []

    def close(self) -> None:
        self.client.close()

    def headers(self) -> dict[str, str]:
        """What every request of the conversation carries: the key, the content type, the client's own user agent and,
        on a session-routed API, the one conversation id."""
        headers = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        if self.config.user_agent:
            headers["User-Agent"] = self.config.user_agent
        if self.config.session_header and self.session_id:
            headers[self.config.session_header] = self.session_id
        return headers

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
                with alarm(remaining):
                    response = self.client.post(
                        self.config.endpoint, content=raw, headers=self.headers(), timeout=timeout
                    )
            except DeadlineHit:
                attempt.error = "the shared deadline ended during the call"
                self.note(attempt, started, raw, None)
                raise ProviderFailure(attempt.error, "deadline") from None
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
                return parse_completion(
                    body,
                    self.config.expected_provider,
                    allow_declines=self.allow_declines,
                    provider_kind=self.config.provider_kind,
                    provider_name=self.config.provider_name,
                    requested_model=self.config.api["model"],
                )
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
