"""Intervention catalogs in results, exposure that does not apply to probes, and compact CLI summaries."""

import json

import test_context_eval as base
import yaml

from sereno.context_eval import cli, runner
from sereno.context_eval.config import default_registry
from sereno.context_eval.engine import intervention_catalog
from sereno.context_eval.metrics import report

fixture_world = base.fixture_world
IDENTITIES = {task: {"id": "sha256:fixture", "reference": "fixture:local"} for task in ("first", "second")}
FILES = ("report.json", "report.md", "sessions.csv")


def read(path):
    return json.loads(path.read_text())


def group(summary, arm, session):
    return next(g for g in summary["groups"] if g["arm"] == arm and g["session"] == session)


def test_result_records_the_intervention_catalog(tmp_path, fixture_world):
    config = base.campaign_config(fixture_world[0])
    root, _ = base.run_fixture(tmp_path, fixture_world, config)
    result = read(base.session_dir(root, "attack_carry", "001-exposure") / "result.json")
    assert result["interventions"] == intervention_catalog(config.variants["canary"])
    assert result["interventions"]["source"]["channel"] == "repo_file"
    assert read(root / "clean" / "first--r001" / "sessions" / "001-exposure" / "result.json")["interventions"] == {}


def test_probe_exposure_is_not_applicable(tmp_path, fixture_world):
    root, _ = base.run_fixture(tmp_path, fixture_world, base.campaign_config(fixture_world[0]))
    probe = read(base.session_dir(root, "attack_carry", "002-probe") / "metrics.json")
    assert (probe["exposure_status"], probe["exposed"], probe["first_exposure_step"]) == ("not_applicable", None, None)
    source = probe["interventions"]["source"]
    assert (source["exposed"], source["first_exposure_step"], source["exposures"]) == (None, None, None)
    assert (source["carried"], source["recalled"]) == (True, True)
    exposure = read(base.session_dir(root, "attack_carry", "001-exposure") / "metrics.json")
    assert (exposure["exposure_status"], exposure["exposed"]) == ("measured", True)

    summary = report(root, default_registry(), bootstrap=10)
    for arm in ("clean", "attack_carry", "attack_reset"):
        probes = group(summary, arm, "probe")
        assert probes["attack"]["exposed"] is None and probes["attack"]["asr_exposed"] is None
        assert probes["transport"]["write_exposed"] is None
    exposed = group(summary, "attack_carry", "exposure")["attack"]["exposed"]
    assert (exposed["k"], exposed["n"], exposed["unknown"]) == (1, 1, 0)
    assert (
        "| first | canary | attack_carry | 2. probe | 1/1 = 1.00 [0.21, 1.00] | - | - |"
        in (root / "report.md").read_text()
    )


def test_cli_prints_compact_summaries(tmp_path, fixture_world, monkeypatch, capsys):
    path = tmp_path / "experiment.yaml"
    path.write_text(yaml.safe_dump(base.campaign_config(fixture_world[0]).model_dump(mode="json")))
    run_campaign = runner.run_campaign

    def offline(config, output, registry, *, resume):
        return run_campaign(config, output, registry, env_factory=fixture_world[1], identities=IDENTITIES)

    monkeypatch.setattr(runner, "run_campaign", offline)
    root = tmp_path / "campaign"
    assert cli.main(["context-eval", "run", str(path), "--out", str(root)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "campaign": str(root.resolve()),
        "files": [str(root.resolve() / name) for name in FILES],
        "sessions": {"complete": 6},
    }

    assert cli.main(["context-eval", "report", str(root), "--bootstrap", "10"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert set(printed) == {"files", "sessions", "groups"} and printed["sessions"] == {"complete": 6}
    assert printed["groups"][-1] == {
        "group": "first canary clean 2. probe",
        "valid": "1/1",
        "strict_success": "0/0, 1 unknown",
        "asr": "0/1 = 0.00 [0.00, 0.79]",
        "exposed": "-",
        "carried": "-",
        "recall_carried": "-",
    }
    assert len(printed["groups"]) == len(read(root / "report.json")["groups"]) == 6

    out = tmp_path / "pooled"
    assert cli.main(["context-eval", "summarize", str(root), "--out", str(out), "--bootstrap", "10"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["files"] == [str(out.resolve() / name) for name in FILES]
    assert all((out / name).exists() for name in FILES)


def test_cli_reports_a_plugin_that_cannot_be_imported(tmp_path, capsys):
    assert cli.main(["context-eval", "report", str(tmp_path), "--plugin", "sereno_missing_plugin"]) == 2
    assert capsys.readouterr().out.startswith("context-eval: cannot import plugin module sereno_missing_plugin:")
