"""The writer's configuration: the frozen design is the contract, and nothing widens a limit."""

import json
from pathlib import Path

import pytest
from writer_world import ENDPOINT, KEY_ENV, LIMITS, OBJECTIVE, PATTERNS, ROOT, World

from sereno.writer.config import (
    campaign_timeout,
    check_deadline,
    check_limits,
    code_sha256,
    load_config,
)
from sereno.writer.errors import ConfigError


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_loads_the_design_and_records_every_file_hash(world):
    config = world.config()
    assert config.endpoint == ENDPOINT
    assert config.credential_env == KEY_ENV
    assert config.objective == OBJECTIVE
    assert config.memory_root == ROOT
    assert config.forbidden_patterns == tuple(PATTERNS)
    assert config.notebook_max_bytes == 6000
    assert config.deadline_seconds == 1740
    assert config.control.attempts == 2 and config.control.repairs == 2
    assert config.assignment_review == "proposed_pending_user_review"
    names = {Path(path).name for path in config.files}
    assert {"writer.json", "writer-design.json", "api.json", "system.md", "last-write.md", "assignment.json"} <= names
    assert len(config.config_sha256) == len(config.prompt_sha256) == len(code_sha256()) == 64


def test_the_config_hash_follows_what_shapes_a_request(world):
    first = world.config().config_sha256
    assert world.config().config_sha256 == first
    assert world.config(deadline=1700).config_sha256 != first
    assert world.config(notebook_max=5000).config_sha256 != first


@pytest.mark.parametrize(
    "change",
    [
        {"temperature": 0.7},
        {"reasoning": {"effort": "max"}},
        {"reasoning_effort": "max"},
        {"top_p": 0.9},
        {"seed": 1},
        {"n": 2},
        {"frequency_penalty": 0.1},
        {"presence_penalty": 0.1},
        {"messages": []},
        {"max_tokens": 4000},
        {"response_format": {"type": "text"}},
        {"provider": {"order": ["DeepSeek"], "allow_fallbacks": True, "require_parameters": True}},
        {"provider": {"order": ["DeepSeek"], "allow_fallbacks": False, "require_parameters": False}},
        {"provider": {"order": ["DeepSeek", "Other"], "allow_fallbacks": False, "require_parameters": True}},
        {"provider": {"order": ["DeepSeek"], "allow_fallbacks": False, "require_parameters": True, "sort": "price"}},
        {"model": ""},
    ],
)
def test_the_api_body_must_keep_the_frozen_invariants(world, change):
    api = {
        "model": "deepseek/test-model",
        "provider": {"order": ["DeepSeek"], "allow_fallbacks": False, "require_parameters": True},
        "max_tokens": 128000,
        "response_format": {"type": "json_object"},
    } | change
    with pytest.raises(ConfigError):
        world.config(api=api)


def test_a_missing_api_field_is_refused(world):
    api = {"model": "deepseek/test-model", "max_tokens": 128000, "response_format": {"type": "json_object"}}
    with pytest.raises(ConfigError, match="exactly"):
        world.config(api=api)


@pytest.mark.parametrize("flag", ["automatic_effort_escalation", "retry_refusal", "retry_truncation"])
def test_the_design_must_not_escalate_or_retry_refusals(world, flag):
    design = {
        "connect_timeout_seconds": 20,
        "response_timeout_seconds": 600,
        "transport_attempts_per_call": 2,
        "retry_http_statuses": [429, 500],
        "retry_delay_seconds": 5,
        "retry_after_max_seconds": 60,
        "structural_repair_calls": 2,
        "automatic_effort_escalation": False,
        "retry_refusal": False,
        "retry_truncation": False,
        "max_generation_calls_per_transition": 3,
    } | {flag: True}
    with pytest.raises(ConfigError, match=flag):
        world.config(control=design)


def test_the_call_counts_must_agree(world):
    control = {
        "connect_timeout_seconds": 20,
        "response_timeout_seconds": 600,
        "transport_attempts_per_call": 2,
        "retry_http_statuses": [429],
        "retry_delay_seconds": 5,
        "retry_after_max_seconds": 60,
        "structural_repair_calls": 2,
        "automatic_effort_escalation": False,
        "retry_refusal": False,
        "retry_truncation": False,
        "max_generation_calls_per_transition": 5,
    }
    with pytest.raises(ConfigError, match="call counts"):
        world.config(control=control)


@pytest.mark.parametrize("endpoint", ["http://openrouter.example/v1", "ftp://x/y", "https:///nohost", "openrouter"])
def test_the_endpoint_must_be_https_or_loopback(world, endpoint):
    with pytest.raises(ConfigError, match="endpoint"):
        world.config(endpoint=endpoint)


@pytest.mark.parametrize(
    "endpoint", ["http://127.0.0.1:8080/v1/chat/completions", "http://localhost/x", "https://a.b/c"]
)
def test_loopback_http_and_https_endpoints_are_accepted(world, endpoint):
    assert world.config(endpoint=endpoint).endpoint == endpoint


def test_the_credential_is_read_from_the_environment_at_execution_time(world, monkeypatch):
    config = world.config()
    monkeypatch.delenv(KEY_ENV, raising=False)
    with pytest.raises(ConfigError) as caught:
        config.credential()
    assert caught.value.code == "missing_credential"
    monkeypatch.setenv(KEY_ENV, "  ")
    with pytest.raises(ConfigError):
        config.credential()
    monkeypatch.setenv(KEY_ENV, "secret-value")
    assert config.credential() == "secret-value"
    assert "secret-value" not in json.dumps(config.files) and "secret-value" not in config.config_sha256


def test_the_campaign_patterns_must_equal_the_frozen_design_exactly(world):
    config = world.config()
    assert check_limits(config, LIMITS).max_files_touched == 2
    for patterns in ([], PATTERNS[:1], PATTERNS[::-1], [*PATTERNS, "extra"], [p + " " for p in PATTERNS]):
        with pytest.raises(ConfigError) as caught:
            check_limits(config, LIMITS | {"forbidden_patterns": patterns})
        assert caught.value.code == "patterns_differ"
    with pytest.raises(ConfigError, match="do not load"):
        check_limits(config, {"max_files_touched": 0, "max_file_bytes": 10, "forbidden_patterns": PATTERNS})
    with pytest.raises(ConfigError, match="do not load"):
        check_limits(config, {"max_files_touched": 2})


def test_the_numeric_limits_come_from_the_campaign_not_the_design(world):
    config = world.config()
    loaded = check_limits(config, LIMITS | {"max_files_touched": 1, "max_file_bytes": 50})
    assert (loaded.max_files_touched, loaded.max_file_bytes) == (1, 50)


def test_the_hook_timeout_must_exceed_the_writer_deadline(world):
    config = world.config()
    assert campaign_timeout(world.campaign) == 1800
    check_deadline(config, 1800)
    for timeout in (1740, 1000):
        with pytest.raises(ConfigError) as caught:
            check_deadline(config, timeout)
        assert caught.value.code == "deadline"
    (world.campaign / "manifest.json").write_text("{}")
    with pytest.raises(ConfigError, match="does not record"):
        campaign_timeout(world.campaign)


def test_broken_design_packages_fail_the_load(world, tmp_path):
    path = world.write_config()
    package = world.package
    design = json.loads((package / "writer-design.json").read_text())
    for key, value in (("schema_version", "design-2"), ("endpoint", 5), ("credential_env_name", "")):
        (package / "writer-design.json").write_text(json.dumps(design | {key: value}))
        with pytest.raises(ConfigError):
            load_config(path)
    (package / "writer-design.json").write_text(json.dumps(design))
    (package / "system.md").write_text("  \n")
    with pytest.raises(ConfigError, match="prompt is empty"):
        load_config(path)
    (package / "system.md").write_text("You write notes. Return one json object.\n")
    (package / "assignment.json").write_text(json.dumps({"memory": {"objective": "x"}}))
    with pytest.raises(ConfigError, match="assignment"):
        load_config(path)
    (package / "assignment.json").write_text("[]")
    with pytest.raises(ConfigError, match="object"):
        load_config(path)


def test_the_design_needs_patterns_that_load_through_the_harness_schema(world):
    with pytest.raises(ConfigError):
        world.config(patterns=[])
    with pytest.raises(ConfigError, match="limits"):
        world.config(patterns=["a*"])  # matches the empty text, which AdaptiveLimits refuses


def test_settings_reject_unknown_fields_and_other_routes(world):
    path = world.write_config()
    settings = json.loads(path.read_text())
    for change in ({"surprise": 1}, {"route": "environment"}, {"schema_version": "2.0"}, {"deadline_seconds": 0}):
        path.write_text(json.dumps(settings | change))
        with pytest.raises(ConfigError):
            load_config(path)
