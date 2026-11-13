"""The CLI's model-free validation and argument handling."""

import json

from attacker_world import make_request, write_config, write_request

from sereno.attacker.cli import main


def test_check_validates_without_a_credential_or_a_model(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SERENO_ATTACKER_MISSING_KEY", raising=False)
    config = write_config(tmp_path, provider={"credential_env": "SERENO_ATTACKER_MISSING_KEY"})
    request = write_request(tmp_path, make_request())
    code = main(["--check", "--config", str(config), str(request)])
    output = json.loads(capsys.readouterr().out)
    assert code == 0 and output["status"] == "ok"
    assert output["provider"] == "openai-compatible"
    assert output["prompt_sha256"] and output["config_sha256"]
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
