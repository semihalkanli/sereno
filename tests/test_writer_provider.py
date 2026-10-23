"""The provider client: the exact request body, explicit failures that are never retried or worked around, transient
retries inside one shared deadline, reasoning fields kept verbatim, and no credential in any record."""

import json

import httpx
import pytest
from writer_world import KEY_ENV, Transport, World, reply

from sereno.writer.errors import ProviderFailure
from sereno.writer.provider import Deadline, Provider, parse_completion, request_body, retry_wait

MESSAGES = [{"role": "system", "content": "system text"}, {"role": "user", "content": "user text"}]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "secret-key-value")
    return World(tmp_path).config()


def provider(config, script, *, seconds=1740, sleeps=None, sink=None, clock=None):
    deadline = Deadline(seconds, clock or Clock())

    def wait(seconds):
        if sleeps is not None:
            sleeps.append(seconds)

    return Provider(config, deadline, transport=script.transport, sleep=wait, sink=sink)


def test_the_body_is_the_api_settings_plus_messages_and_nothing_else(config):
    script = Transport(reply('{"a": 1}'))
    completion = provider(config, script).complete(MESSAGES, 1)
    (body,) = script.bodies()
    assert body == {**config.api, "messages": MESSAGES}
    assert set(body) == {"model", "provider", "max_tokens", "response_format", "messages"}
    for barred in ("reasoning", "reasoning_effort", "temperature", "top_p", "seed", "n", "frequency_penalty"):
        assert barred not in body
    assert body["provider"] == {"order": ["DeepSeek"], "allow_fallbacks": False, "require_parameters": True}
    assert body["max_tokens"] == 128000
    assert completion.content == '{"a": 1}' and completion.provider == "DeepSeek"
    assert completion.model == "deepseek/test-model" and completion.generation_id == "gen-1"
    assert completion.record()["finish_reason"] == "stop" and completion.record()["cost"] == 0.0123
    assert request_body(config.api, MESSAGES) == body


def test_the_request_goes_to_the_endpoint_with_the_key_in_the_header_only(config):
    script = Transport(reply("x"))
    provider(config, script).complete(MESSAGES, 1)
    (request,) = script.requests
    assert str(request.url) == config.endpoint
    assert request.headers["authorization"] == "Bearer secret-key-value"
    assert b"secret-key-value" not in request.content


def test_no_record_holds_the_credential(config):
    records = []
    script = Transport((503, {"error": {"message": "busy"}}, {}), reply("x"))
    provider(
        config, script, sink=lambda attempt, request, response: records.append((attempt, request, response))
    ).complete(MESSAGES, 1)
    assert len(records) == 2
    for attempt, request, response in records:
        assert "secret-key-value" not in json.dumps(attempt.record())
        assert b"secret-key-value" not in request and b"secret-key-value" not in (response or b"")


def test_reasoning_fields_are_kept_exactly_and_replayed_unchanged(config):
    details = [{"type": "reasoning.text", "text": "step one", "signature": "sig==", "format": "unknown"}]
    reasoning = {"reasoning": "plain çğ text", "reasoning_details": details, "reasoning_content": "alias"}
    completion = provider(config, Transport(reply("{}", reasoning=reasoning))).complete(MESSAGES, 1)
    assert completion.reasoning == reasoning
    message = completion.assistant_message()
    assert message == {"role": "assistant", "content": "{}", **reasoning}
    details[0]["text"] = "mutated"
    assert completion.reasoning["reasoning_details"][0]["text"] == "step one"
    again = Transport(reply("{}"))
    provider(config, again).complete([*MESSAGES, message, {"role": "user", "content": "repair"}], 2)
    assert again.bodies()[0]["messages"][2]["reasoning_details"][0]["signature"] == "sig=="


@pytest.mark.parametrize(
    ("item", "code"),
    [
        ((401, {"error": {"message": "no key"}}, {}), "auth"),
        ((403, {"error": {"message": "no"}}, {}), "auth"),
        ((402, {"error": {"message": "balance"}}, {}), "balance"),
        ((404, {}, {}), "http_404"),
        ((400, {}, {}), "http_400"),
        ((501, {}, {}), "http_501"),
        ((307, b"", {"location": "https://elsewhere.test/"}), "http_307"),
        (reply("x", provider="Other"), "unexpected_provider"),
        (reply("x", extra={"provider": None}), "unexpected_provider"),
        (reply("x", extra={"provider": "deepseek"}), "unexpected_provider"),
        (reply("x", finish="length"), "truncated"),
        (reply("x", finish="content_filter"), "refusal"),
        (reply("x", finish="tool_calls"), "unexpected_finish"),
        (reply("x", finish="error"), "unexpected_finish"),
        (reply("x", finish=None), "unexpected_finish"),
        (reply("   "), "empty_completion"),
        (reply(""), "empty_completion"),
        (reply(None), "empty_completion"),
        (reply(["part"]), "empty_completion"),
        ({"error": {"message": "upstream failed", "code": 502}}, "body_error"),
        (reply("x", extra={"error": "boom"}), "body_error"),
        (b"not json at all", "malformed_body"),
        ([1, 2, 3], "malformed_body"),
        ({"choices": []}, "malformed_body"),
        ({"choices": [{"finish_reason": "stop"}], "provider": "DeepSeek"}, "malformed_body"),
    ],
)
def test_explicit_failures_are_never_retried_or_worked_around(config, item, code):
    script = Transport(item, reply("{}"))
    sleeps = []
    with pytest.raises(ProviderFailure) as caught:
        provider(config, script, sleeps=sleeps).complete(MESSAGES, 1)
    assert caught.value.code == code
    assert len(script.requests) == 1 and sleeps == []
    assert len(script.queue) == 1


def test_a_refusal_in_the_message_is_a_refusal_not_an_empty_answer(config):
    body = reply("")
    body["choices"][0]["message"]["refusal"] = "I cannot help with that."
    with pytest.raises(ProviderFailure) as caught:
        provider(config, Transport(body)).complete(MESSAGES, 1)
    assert caught.value.code == "refusal"


def test_a_choice_level_error_is_a_failure(config):
    body = reply("x")
    body["choices"][0]["error"] = {"message": "provider error", "code": 500}
    with pytest.raises(ProviderFailure) as caught:
        provider(config, Transport(body)).complete(MESSAGES, 1)
    assert caught.value.code == "body_error"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_a_transient_status_is_retried_once_with_the_default_delay(config, status):
    records, sleeps = [], []
    script = Transport((status, {"error": {"message": "later"}}, {}), reply("ok"))
    completion = provider(config, script, sleeps=sleeps, sink=lambda *args: records.append(args)).complete(MESSAGES, 1)
    assert completion.content == "ok" and sleeps == [5.0]
    assert [r[0].http_status for r in records] == [status, 200]
    assert [r[0].attempt for r in records] == [1, 2] and {r[0].call for r in records} == {1}


def test_two_transient_failures_end_the_call_after_exactly_two_attempts(config):
    script = Transport((503, {}, {}), (503, {}, {}), reply("never"))
    with pytest.raises(ProviderFailure) as caught:
        provider(config, script).complete(MESSAGES, 1)
    assert caught.value.code == "http_transient" and len(script.requests) == 2


def test_retry_after_is_honored_and_capped(config):
    sleeps = []
    script = Transport((429, {}, {"retry-after": "7"}), reply("ok"))
    provider(config, script, sleeps=sleeps).complete(MESSAGES, 1)
    assert sleeps == [7.0]
    sleeps.clear()
    script = Transport((429, {}, {"retry-after": "500"}), reply("ok"))
    provider(config, script, sleeps=sleeps).complete(MESSAGES, 1)
    assert sleeps == [60.0]


def test_retry_after_is_not_honored_when_the_deadline_leaves_no_room(config):
    sleeps = []
    script = Transport((429, {}, {"retry-after": "50"}), reply("ok"))
    with pytest.raises(ProviderFailure) as caught:
        provider(config, script, seconds=30, sleeps=sleeps).complete(MESSAGES, 1)
    assert caught.value.code == "deadline" and sleeps == [] and len(script.requests) == 1


def test_a_connection_failure_is_retried_and_both_attempts_are_recorded(config):
    records = []
    script = Transport(httpx.ConnectError("refused"), reply("ok"))
    completion = provider(config, script, sink=lambda *args: records.append(args)).complete(MESSAGES, 1)
    assert completion.content == "ok"
    first, second = (record[0] for record in records)
    assert first.error and "ConnectError" in first.error and first.http_status is None
    assert second.http_status == 200 and records[0][2] is None


def test_an_ambiguous_timeout_can_be_billed_twice_and_is_logged_as_two_attempts(config):
    records = []
    script = Transport(httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"))
    with pytest.raises(ProviderFailure) as caught:
        provider(config, script, sink=lambda *args: records.append(args)).complete(MESSAGES, 1)
    assert caught.value.code == "transport" and len(records) == 2
    assert records[0][0].request_sha256 == records[1][0].request_sha256


def test_the_call_fails_when_the_deadline_has_already_ended(config):
    clock = Clock()
    deadline = Deadline(10, clock)
    clock.now = 11
    script = Transport(reply("x"))
    client = Provider(config, deadline, transport=script.transport)
    with pytest.raises(ProviderFailure) as caught:
        client.complete(MESSAGES, 1)
    assert caught.value.code == "deadline" and script.requests == []


def test_timeouts_are_clipped_to_the_remaining_deadline(config):
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json=reply("x"))

    clock = Clock()
    client = Provider(config, Deadline(100, clock), transport=httpx.MockTransport(handler))
    client.complete(MESSAGES, 1)
    assert seen[0]["connect"] == 20 and seen[0]["read"] == 100


def test_no_redirect_is_followed(config):
    script = Transport((302, b"", {"location": "https://elsewhere.test/"}), reply("x"))
    with pytest.raises(ProviderFailure):
        provider(config, script).complete(MESSAGES, 1)
    assert len(script.requests) == 1


def test_retry_wait_reads_seconds_and_dates():
    assert retry_wait(None, 5, 60) == 5
    assert retry_wait("3", 5, 60) == 3
    assert retry_wait("-4", 5, 60) == 0
    assert retry_wait("99999", 5, 60) == 60
    assert retry_wait("not a date", 5, 60) == 5
    assert retry_wait("Wed, 21 Oct 2015 07:28:00 GMT", 5, 60) == 0
    # A zone-less date, a nan and an infinite value are all handled, never an exception.
    assert retry_wait("Wed, 21 Oct 2015 07:28:00 -0000", 5, 60) == 0
    assert retry_wait("nan", 5, 60) == 5
    assert retry_wait("inf", 5, 60) == 5
    assert retry_wait("-inf", 5, 60) == 5
    assert retry_wait("", 5, 60) == 5


def test_parse_completion_keeps_usage_and_native_finish_reason():
    body = reply("x")
    body["choices"][0]["native_finish_reason"] = "stop_sequence"
    completion = parse_completion(body, "DeepSeek")
    assert completion.native_finish_reason == "stop_sequence" and completion.usage["prompt_tokens"] == 10


def test_the_missing_credential_fails_before_any_call(tmp_path, monkeypatch):
    monkeypatch.delenv(KEY_ENV, raising=False)
    config = World(tmp_path).config()
    script = Transport(reply("x"))
    with pytest.raises(Exception) as caught:
        Provider(config, Deadline(10), transport=script.transport)
    assert getattr(caught.value, "code", None) == "missing_credential" and script.requests == []


def test_a_slow_provider_is_cut_at_the_deadline_not_at_the_next_read(config):
    import signal
    import time

    def slow(request):
        time.sleep(5)
        return httpx.Response(200, json=reply("late"))

    records = []
    client = Provider(
        config, Deadline(0.4), transport=httpx.MockTransport(slow), sink=lambda *args: records.append(args)
    )
    handler_before = signal.getsignal(signal.SIGALRM)
    started = time.monotonic()
    with pytest.raises(ProviderFailure) as caught:
        client.complete(MESSAGES, 1)
    assert caught.value.code == "deadline" and time.monotonic() - started < 3
    assert len(records) == 1 and "deadline" in records[0][0].error and records[0][2] is None
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0
    assert signal.getsignal(signal.SIGALRM) == handler_before


def test_the_alarm_is_cancelled_and_the_handler_restored_after_a_normal_call(config):
    import signal

    before = signal.getsignal(signal.SIGALRM)
    provider(config, Transport(reply("ok"))).complete(MESSAGES, 1)
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0 and signal.getsignal(signal.SIGALRM) == before
