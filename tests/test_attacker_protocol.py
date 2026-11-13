"""Protocol 1.0 request validation and response form."""

import pytest
from attacker_world import INERT, NOTE, make_config, make_request, write_request

from sereno.attacker.errors import ConfigError, InputError, OutputError
from sereno.attacker.protocol import check_contract, read_request, validate_response


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
