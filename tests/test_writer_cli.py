"""The program's command line: the identity hashes that make a writer a campaign-level fact, and the exit status of
every kind of failure."""

import json

import pytest
from writer_world import KEY_ENV, World, answer, reply

from sereno.writer.cli import main
from sereno.writer.config import check_identity, code_sha256, load_config
from sereno.writer.errors import ConfigError


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv(KEY_ENV, "key")
    return World(tmp_path)


def words(config_path) -> list[str]:
    identity = load_config(config_path).identity()
    return ["--config-sha256", identity["config_sha256"], "--code-sha256", identity["code_sha256"]]


def test_the_hashes_command_prints_the_identity_and_the_words_for_the_campaign_command(world, capsys):
    path = world.write_config()
    assert main(["hashes", "--config", str(path)]) == 0
    printed = json.loads(capsys.readouterr().out)
    identity = load_config(path).identity()
    assert printed["config_sha256"] == identity["config_sha256"] and printed["code_sha256"] == identity["code_sha256"]
    assert printed["command_words"] == ["--config", str(path), *words(path)]


def test_the_hashes_command_reports_a_broken_configuration(world, capsys, tmp_path):
    assert main(["hashes", "--config", str(tmp_path / "missing.json")]) == 2
    assert capsys.readouterr().err.startswith("writer: config: ")


def test_a_run_without_the_identity_words_is_a_usage_error(world, tmp_path):
    path = world.write_config()
    with pytest.raises(SystemExit) as caught:
        main(["--config", str(path), str(tmp_path / "request.json"), str(tmp_path / "response.json")])
    assert caught.value.code == 2


def test_a_changed_configuration_or_code_is_refused_before_any_work(world, capsys):
    path = world.write_config()
    request, response = world.write_request(0)
    good = words(path)
    wrong_config = ["--config-sha256", "0" * 64, *good[2:]]
    wrong_code = [*good[:2], "--code-sha256", "1" * 64]
    for extra, name in ((wrong_config, "config_sha256"), (wrong_code, "code_sha256")):
        assert main(["--config", str(path), *extra, str(request), str(response)]) == 2
        error = capsys.readouterr().err
        assert "identity_mismatch" in error and name in error and error.count("\n") == 1
    assert not response.exists() and not (world.state / "writer").exists()


def test_the_identity_follows_the_prompt_the_api_body_the_assignment_and_the_design(world, tmp_path):
    first = world.config().identity()
    assert world.config().identity() == first

    def changed(file: str, text: str) -> str:
        path = world.write_config()
        original = (world.package / file).read_text()
        (world.package / file).write_text(text)
        value = load_config(path).identity()["config_sha256"]
        (world.package / file).write_text(original)
        return value

    assert changed("system.md", "You write notes. Return one json object. \n") != first["config_sha256"]
    assert changed("last-write.md", "Another sentence.\n") != first["config_sha256"]
    assert (
        changed("assignment.json", json.dumps({"memory": {"objective": "Other.", "memory_root": "/x/"}}))
        != first["config_sha256"]
    )
    api = json.loads((world.package / "api.json").read_text()) | {"model": "deepseek/other-model"}
    assert changed("api.json", json.dumps(api)) != first["config_sha256"]
    assert world.config(deadline=1700).identity()["config_sha256"] != first["config_sha256"]
    assert world.config(max_input=3_000_000).identity()["config_sha256"] != first["config_sha256"]
    assert world.config().identity() == first, "restoring the files restores the identity"
    # The same writer on another machine: other paths, same identity.
    other_root = tmp_path / "elsewhere"
    other_root.mkdir()
    other = World(other_root)
    assert other.config().identity() == first and str(other.store) != str(world.store)


def test_check_identity_names_what_differs(world):
    config = world.config()
    check_identity(config, config.config_sha256, code_sha256())
    with pytest.raises(ConfigError) as caught:
        check_identity(config, "f" * 64, code_sha256())
    assert caught.value.code == "identity_mismatch" and "config_sha256" in str(caught.value)


def test_the_code_hash_covers_the_package_and_the_harness_package_it_imports():
    import hashlib
    from pathlib import Path

    import sereno.context_eval
    import sereno.writer

    sources = [
        *Path(sereno.writer.__file__).parent.glob("*.py"),
        *Path(sereno.context_eval.__file__).parent.glob("*.py"),
    ]
    digest = hashlib.sha256()
    for source in sorted(sources):
        digest.update(f"{source.parent.name}/{source.name}".encode() + b"\0" + source.read_bytes() + b"\0")
    assert code_sha256() == digest.hexdigest()
    # Both packages hold a config.py: each is named with its package, so neither can stand in for the other.
    assert {"context_eval/config.py", "writer/config.py"} <= {f"{s.parent.name}/{s.name}" for s in sources}


def test_exit_status_names_the_kind_of_failure_and_stderr_is_one_line(world, capsys, monkeypatch):
    path = world.write_config()
    request, response = world.write_request(0)
    base = ["--config", str(path), *words(path)]
    monkeypatch.delenv(KEY_ENV)
    assert main([*base, str(request), str(response)]) == 2
    assert "writer: missing_credential: " in capsys.readouterr().err
    monkeypatch.setenv(KEY_ENV, "key")
    bad = json.loads(request.read_text()) | {"schema_version": "9"}
    request.write_text(json.dumps(bad))
    assert main([*base, str(request), str(response)]) == 3
    assert capsys.readouterr().err.startswith("writer: bad_request: ") and not response.exists()
    assert main([*base, str(world.root / "no-such-request.json"), str(response)]) == 3
    capsys.readouterr()
    (world.package / "system.md").write_text("changed\n")
    assert main([*base, str(request), str(response)]) == 2


def test_a_run_through_the_command_line_writes_the_hook_response(world, capsys, monkeypatch):
    # The command line uses the real client; a loopback server stands in for the provider.
    from writer_world import StubServer

    with StubServer(reply(answer())) as server:
        path = world.write_config(endpoint=server.url)
        request, response = world.write_request(0)
        assert main(["--config", str(path), *words(path), str(request), str(response)]) == 0
        assert len(server.requests) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["outcome"] == "delivered_ready" and json.loads(response.read_text())["schema_version"] == "1.0"
