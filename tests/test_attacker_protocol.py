"""Protocol 1.0 request validation and response form."""

import pytest
from attacker_world import INERT, NOTE, make_config, make_request, write_request

from sereno.attacker.errors import ConfigError, InputError, OutputError
from sereno.attacker.protocol import check_contract, read_request, usage_record, validate_response
from sereno.attacker.providers.base import Completion


def test_read_request_accepts_a_valid_request(tmp_path):
    request = make_request()
    assert read_request(write_request(tmp_path, request)) == request


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), "2.0"),
        (("contract",), None),
        (("contract", "kind"), "later"),
        (("contract", "stage"), "warmup"),
        (("contract", "writes_open"), "yes"),
        (("strategy", "text"), 3),
        (("notebook",), 0),
        (("contract", "limits"), []),
        (("contract", "timeout_seconds"), 0),
        (("contract", "response_keys"), ["actions"]),
    ],
)
def test_read_request_refuses_malformed_requests(tmp_path, path, value):
    request = make_request()
    target = request
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(InputError):
        read_request(write_request(tmp_path, request))


def test_check_contract_refuses_a_deadline_not_below_the_timeout(tmp_path):
    config = make_config(tmp_path, deadline=300)
    with pytest.raises(ConfigError) as caught:
        check_contract(config, make_request(timeout_seconds=300))
    assert caught.value.code == "deadline"


def test_check_contract_accepts_a_shorter_deadline(tmp_path):
    check_contract(make_config(tmp_path, deadline=60), make_request(timeout_seconds=300))


@pytest.mark.parametrize("reads", [0, 1, 100])
def test_check_contract_accepts_a_non_negative_queued_read_count(tmp_path, reads):
    """A campaign disables queued code reads with max_reads_per_call: 0, so zero must reach the program."""
    check_contract(make_config(tmp_path), make_request(max_reads_per_call=reads))


@pytest.mark.parametrize("reads", [-1, True, "10", None])
def test_check_contract_refuses_a_bad_queued_read_count(tmp_path, reads):
    with pytest.raises(InputError):
        check_contract(make_config(tmp_path), make_request(max_reads_per_call=reads))


@pytest.mark.parametrize("size", [0, -1, True, "200000", None])
def test_check_contract_still_requires_a_positive_read_size(tmp_path, size):
    """max_read_bytes is a size bound, not a count, so zero stays refused."""
    with pytest.raises(InputError):
        check_contract(make_config(tmp_path), make_request(max_read_bytes=size))


def test_usage_record_preserves_reported_totals_when_a_completion_has_no_usage(tmp_path):
    completions = [
        Completion("", (), {}, "stop", {"prompt_tokens": 7, "completion_tokens": 3, "cost": 0.2}),
        Completion("", (), {}, "stop", {}),
        Completion("", (), {}, "stop", {"total_tokens": 4, "cost": 0.1}),
    ]
    record = usage_record(
        completions,
        finish="finished",
        steps=3,
        provider=type("P", (), {"name": "fixture"})(),
        config=make_config(tmp_path),
    )
    assert record["usage"] == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 14}
    assert record["unreported_completions"] == 1
    assert record["cost"] == pytest.approx(0.3)


def test_validate_response_accepts_the_runtime_response():
    response = {
        "schema_version": "1.0",
        "actions": [{"op": "write", "path": NOTE, "content": INERT}],
        "strategy": "a strategy",
        "notebook": "a notebook",
        "reads": [NOTE],
        "writer_usage": {"calls": 1},
    }
    validate_response(response, make_request()["contract"])


def test_validate_response_accepts_a_zero_read_contract():
    """The response of a call under max_reads_per_call: 0 keeps the same form, with an empty reads list."""
    response = {
        "schema_version": "1.0",
        "actions": [{"op": "write", "path": NOTE, "content": INERT}],
        "reads": [],
        "writer_usage": {"calls": 1},
    }
    validate_response(response, make_request(max_reads_per_call=0)["contract"])


@pytest.mark.parametrize(
    "change",
    [
        lambda response: response["actions"][0].update({"op": "move"}),
        lambda response: response["actions"][0].pop("content"),
        lambda response: (
            response["actions"][0].update({"content": "x"}) or response["actions"][0].update({"op": "delete"})
        ),
        lambda response: response.update({"strategy": "far too long"}),
        lambda response: response.update({"reads": [1]}),
        lambda response: response.update({"extra": True}),
    ],
)
def test_validate_response_refuses_a_bad_form(change):
    response = {
        "schema_version": "1.0",
        "actions": [{"op": "write", "path": NOTE, "content": INERT}],
        "strategy": "a",
        "notebook": "a notebook",
        "reads": [NOTE],
        "writer_usage": {"calls": 1},
    }
    change(response)
    contract = make_request()["contract"] | {"strategy_max_bytes": 5}
    with pytest.raises(OutputError):
        validate_response(response, contract)
