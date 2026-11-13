"""The CLI's model-free validation and argument handling."""

import json

import pytest
from attacker_world import make_request, write_config, write_request

from sereno.attacker.cli import main
from sereno.attacker.config import load_config
from sereno.attacker.identity import code_sha256, code_sources


def test_check_validates_without_a_credential_or_a_model(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SERENO_ATTACKER_MISSING_KEY", raising=False)
    config = write_config(tmp_path, provider={"credential_env": "SERENO_ATTACKER_MISSING_KEY"})
    request = write_request(tmp_path, make_request())
    code = main(["--check", "--config", str(config), str(request)])
    output = json.loads(capsys.readouterr().out)
    assert code == 0 and output["status"] == "ok"
    assert output["provider"] == "openai-compatible"
    assert output["prompt_sha256"] and output["config_sha256"]
    assert output["code_sha256"] == code_sha256()
    for name in ("config", "prompt", "code"):
        flag = f"--{name}-sha256"
        assert output["command_words"][output["command_words"].index(flag) + 1] == output[f"{name}_sha256"]
    assert not (tmp_path / "response.json").exists()


def test_check_accepts_a_deepseek_shaped_openai_compatible_provider(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SERENO_ATTACKER_MISSING_KEY", raising=False)
    config = write_config(
        tmp_path,
        provider={
            "kind": "openai-compatible",
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com/chat/completions",
            "credential_env": "SERENO_ATTACKER_MISSING_KEY",
        },
    )
    request = write_request(tmp_path, make_request())
    code = main(["--check", "--config", str(config), str(request)])
    output = json.loads(capsys.readouterr().out)
    assert code == 0 and output["status"] == "ok"
    assert output["provider"] == "openai-compatible"
    assert output["model"] == "deepseek-chat"
    assert not (tmp_path / "response.json").exists()


def test_check_refuses_a_deadline_not_below_the_harness_timeout(tmp_path, capsys):
    config = write_config(tmp_path, deadline=300)
    request = write_request(tmp_path, make_request(timeout_seconds=300))
    code = main(["--check", "--config", str(config), str(request)])
    assert code == 2 and "deadline" in capsys.readouterr().err


def test_live_needs_a_response_path(tmp_path, capsys):
    config = write_config(tmp_path)
    request = write_request(tmp_path, make_request())
    code = main(["--live", "--config", str(config), str(request)])
    assert code == 3 and "response path" in capsys.readouterr().err


def test_a_bad_request_is_an_input_error(tmp_path, capsys):
    config = write_config(tmp_path)
    request = make_request()
    request["schema_version"] = "2.0"
    path = write_request(tmp_path, request)
    code = main(["--check", "--config", str(config), str(path)])
    assert code == 3 and "schema_version" in capsys.readouterr().err


def test_live_requires_identity_pins_before_provider_initialization(tmp_path, capsys, monkeypatch):
    config = write_config(tmp_path)
    request = write_request(tmp_path, make_request())
    monkeypatch.setattr("sereno.attacker.cli.build_provider", lambda *args: pytest.fail("provider initialized"))
    code = main(["--live", "--config", str(config), str(request), str(tmp_path / "response.json")])
    assert code == 2 and "missing_pin" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["config_sha256", "prompt_sha256", "code_sha256"])
def test_live_refuses_changed_identity_before_provider_initialization(tmp_path, capsys, monkeypatch, name):
    config = write_config(tmp_path)
    request = write_request(tmp_path, make_request())
    pins = load_config(config).identity() | {name: "0" * 64}
    words = [word for key, value in pins.items() for word in (f"--{key.replace('_', '-')}", value)]
    monkeypatch.setattr("sereno.attacker.cli.build_provider", lambda *args: pytest.fail("provider initialized"))
    code = main(["--live", "--config", str(config), *words, str(request), str(tmp_path / "response.json")])
    assert code == 2 and "identity_mismatch" in capsys.readouterr().err


def test_code_identity_covers_transitive_sereno_imports(monkeypatch):
    sources = code_sources()
    dependency = next(source for source in sources if source.name == "usage.py")
    assert any(source.name == "schema.py" and source.parent.name == "context_eval" for source in sources)
    before = code_sha256()
    read_bytes = type(dependency).read_bytes
    monkeypatch.setattr(
        type(dependency), "read_bytes", lambda path: read_bytes(path) + (b"\n" if path == dependency else b"")
    )
    assert code_sha256() != before
