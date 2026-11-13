"""The runtime configuration: the brief, the provider block, the bounds and the hashes."""

from pathlib import Path

import pytest
import yaml
from attacker_world import PROMPT

from sereno.attacker.config import AttackerSettings, load_config
from sereno.attacker.errors import ConfigError


def test_runtime_example_configuration_is_valid():
    path = Path(__file__).parents[1] / "examples/context-eval/attacker-runtime.yaml"
    settings = AttackerSettings.model_validate(yaml.safe_load(path.read_text()))
    assert settings.prompt_file.is_absolute()
    assert settings.deadline_seconds < 60
    assert settings.provider.credential_env == "DEEPSEEK_API_KEY"


def write_config(tmp_path: Path, **changes) -> Path:
    (tmp_path / "brief.md").write_text(PROMPT)
    data = {
        "schema_version": "1.0",
        "prompt_file": "brief.md",
        "objective": "",
        "provider": {
            "kind": "openai-compatible",
            "model": "fixture-model",
            "base_url": "https://example.test/v1",
            "credential_env": None,
        },
        "deadline_seconds": 60,
    }
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = data[key] | value
        else:
            data[key] = value
    path = tmp_path / "attacker.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def test_a_relative_prompt_file_loads_and_the_objective_is_appended(tmp_path):
    config = load_config(write_config(tmp_path, objective=" Reached through the tools. "))
    assert config.prompt == PROMPT + "\n\n## Objective\n\nReached through the tools.\n"
    assert config.prompt_sha256 and config.config_sha256
    assert config.path.is_absolute()


def test_an_empty_prompt_is_refused(tmp_path):
    path = write_config(tmp_path)
    (tmp_path / "brief.md").write_text("  \n")
    with pytest.raises(ConfigError) as caught:
        load_config(path)
    assert caught.value.code == "prompt"


def test_a_provider_request_cannot_carry_runtime_fields(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_config(write_config(tmp_path, provider={"request": {"messages": []}}))
    assert caught.value.code == "barred_field"


def test_an_anthropic_provider_needs_max_tokens(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_config(write_config(tmp_path, provider={"kind": "anthropic"}))
    assert caught.value.code == "missing_max_tokens"
    config = load_config(write_config(tmp_path, provider={"kind": "anthropic", "request": {"max_tokens": 1024}}))
    assert config.settings.provider.kind == "anthropic"


def test_base_url_must_be_https_or_loopback(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_config(write_config(tmp_path, provider={"base_url": "http://example.test/v1"}))
    assert caught.value.code == "endpoint"
    config = load_config(write_config(tmp_path, provider={"base_url": "http://127.0.0.1:8080/v1"}))
    assert config.settings.provider.base_url == "http://127.0.0.1:8080/v1"


def test_the_credential_is_read_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("FIXTURE_ATTACKER_TEST_KEY", raising=False)
    config = load_config(write_config(tmp_path, provider={"credential_env": "FIXTURE_ATTACKER_TEST_KEY"}))
    with pytest.raises(ConfigError) as caught:
        config.credential()
    assert caught.value.code == "missing_credential"
    monkeypatch.setenv("FIXTURE_ATTACKER_TEST_KEY", "fixture-key")
    assert config.credential() == "fixture-key"


def test_a_harness_controlled_credential_name_is_refused(tmp_path):
    """The harness builds these variables itself for the program it launches; none of them is a credential."""
    for name in ("PATH", "PYTHONPATH", "HOME", "TMPDIR", "SERENO_RUNS", "LC_ALL", "LD_PRELOAD", "TZ"):
        with pytest.raises(ConfigError) as caught:
            load_config(write_config(tmp_path, provider={"credential_env": name}))
        assert caught.value.code == "credential_env"


def test_inline_credentials_are_refused(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_config(write_config(tmp_path, provider={"api_key": "not-a-real-key"}))
    assert caught.value.code == "inline_credential"


def test_hashes_separate_the_prompt_from_the_configuration(tmp_path):
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    first = load_config(write_config(tmp_path / "a"))
    same = load_config(write_config(tmp_path / "b"))
    other_model = load_config(write_config(tmp_path / "c", provider={"model": "another-model"}))
    assert first.prompt_sha256 == same.prompt_sha256 == other_model.prompt_sha256
    assert first.config_sha256 != other_model.config_sha256
    (tmp_path / "c" / "brief.md").write_text(PROMPT + " changed")
    changed = load_config(tmp_path / "c" / "attacker.yaml")
    assert changed.prompt_sha256 != first.prompt_sha256
    assert changed.config_sha256 != first.config_sha256
