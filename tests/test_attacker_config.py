"""The runtime configuration: the brief, the provider block, the bounds and the hashes."""

from pathlib import Path

import pytest
import yaml
from attacker_world import PROMPT

from sereno.attacker.config import AttackerSettings, check_settings, load_config
from sereno.attacker.errors import ConfigError

EXAMPLES = Path(__file__).parents[1] / "examples/context-eval"


def test_runtime_example_configuration_is_valid():
    path = EXAMPLES / "attacker-runtime.yaml"
    settings = AttackerSettings.model_validate(yaml.safe_load(path.read_text()))
    assert settings.prompt_file.is_absolute()
    assert settings.deadline_seconds < 60
    assert settings.provider.credential_env == "DEEPSEEK_API_KEY"


def test_runtime_opencode_example_configuration_is_valid():
    settings = AttackerSettings.model_validate(
        yaml.safe_load((EXAMPLES / "attacker-runtime-opencode.yaml").read_text())
    )
    check_settings(settings)
    assert settings.provider.kind == "openai-compatible"
    assert settings.provider.model == "deepseek-v4.1-flash"
    assert settings.provider.base_url == "https://opencode.ai/inference/go/openai/v1/chat/completions"
    assert settings.provider.credential_env == "OPENCODE_API_KEY"
    assert settings.provider.user_agent == "sereno-attacker/1.0"
    assert settings.provider.session_header == "x-opencode-session"
    assert settings.provider.request == {}


def test_runtime_openrouter_example_configuration_is_valid():
    settings = AttackerSettings.model_validate(
        yaml.safe_load((EXAMPLES / "attacker-runtime-openrouter.yaml").read_text())
    )
    check_settings(settings)
    assert settings.provider.kind == "openai-compatible"
    assert settings.provider.base_url == "https://openrouter.ai/api/v1/chat/completions"
    assert settings.provider.credential_env == "OPENROUTER_API_KEY"
    assert settings.provider.expected_provider == "DeepSeek"
    assert settings.provider.user_agent is None and settings.provider.session_header is None


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


@pytest.mark.parametrize(
    "url",
    [
        "https://opencode.ai/inference/go/anthropic/v1/messages",
        "https://opencode.ai/inference/go/openai/v1",
        "https://opencode.ai/inference/go/openai/v1/chat/completions/extra",
    ],
)
def test_an_opencode_base_url_must_be_the_chat_completions_route(tmp_path, url):
    """The runtime builds a chat-completions body; another route under the Go API's prefix would accept it and answer
    nothing the adapter can read, so it is refused at configuration time."""
    with pytest.raises(ConfigError, match="chat/completions"):
        load_config(write_config(tmp_path, provider={"base_url": url}))
    accepted = "https://opencode.ai/inference/go/openai/v1/chat/completions"
    assert load_config(write_config(tmp_path, provider={"base_url": accepted})).settings.provider.base_url == accepted


def test_the_credential_is_read_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("FIXTURE_ATTACKER_TEST_KEY", raising=False)
    config = load_config(write_config(tmp_path, provider={"credential_env": "FIXTURE_ATTACKER_TEST_KEY"}))
    with pytest.raises(ConfigError) as caught:
        config.credential()
    assert caught.value.code == "missing_credential"
    monkeypatch.setenv("FIXTURE_ATTACKER_TEST_KEY", "fixture-key")
    assert config.credential() == "fixture-key"
    # The name the configuration carries is the one read: another provider's key in the environment changes nothing,
    # and removing the named variable raises even while that other key stays set.
    monkeypatch.setenv("OPENCODE_API_KEY", "another-key")
    assert config.credential() == "fixture-key"
    monkeypatch.delenv("FIXTURE_ATTACKER_TEST_KEY")
    with pytest.raises(ConfigError) as caught:
        config.credential()
    assert caught.value.code == "missing_credential"


def test_a_product_user_agent_and_a_session_header_are_accepted(tmp_path):
    config = load_config(
        write_config(
            tmp_path,
            provider={"user_agent": "sereno-attacker/1.0", "session_header": "x-opencode-session"},
        )
    )
    assert config.settings.provider.user_agent == "sereno-attacker/1.0"
    assert config.settings.provider.session_header == "x-opencode-session"


@pytest.mark.parametrize(
    "provider",
    [
        {"user_agent": "python-httpx/0.28"},
        {"user_agent": "httpx"},
        {"user_agent": "OpenAI/Python 3.12"},
        {"user_agent": "no-version"},
        {"session_header": "not a header"},
        {"session_header": "Authorization"},
        {"session_header": "Content-Type"},
        {"session_header": "x-custom", "headers": {"x-custom": "value"}},
    ],
)
def test_a_generic_user_agent_or_a_runtime_header_is_refused(tmp_path, provider):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, provider=provider))


def test_the_opencode_credential_variable_is_read_and_forwardable(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "fixture-go-key")
    config = load_config(write_config(tmp_path, provider={"credential_env": "OPENCODE_API_KEY"}))
    assert config.credential() == "fixture-go-key"


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
