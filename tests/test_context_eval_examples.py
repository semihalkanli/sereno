"""The examples validate, and the scripted end-to-end campaign measures what its scripts determine.

The end-to-end tests run containers from the local DeepSWE images, the scripted one also its verifier; they run only
with SERENO_DOCKER_TESTS=1.
"""

import importlib.util
import json
import os
from pathlib import Path

import pytest

from sereno.context_eval.cli import main

EXAMPLES = Path(__file__).parents[1] / "examples" / "context-eval"
DATASET = Path("~/sereno-deepswe/deep-swe").expanduser()
EXAMPLE_NAMES = [
    "scripted-canary",
    "scripted-e2e",
    "mini-swe",
    "clean-chain",
    "run-attacker-defended",
    "run-attacker-undefended",
]


@pytest.mark.skipif(not DATASET.is_dir(), reason="needs the DeepSWE checkout")
@pytest.mark.parametrize("name", EXAMPLE_NAMES)
def test_examples_validate(name):
    assert main(["context-eval", "validate", str(EXAMPLES / f"{name}.yaml")]) == 0


def run_campaign(example: Path, campaign: Path) -> list:
    """Run, grade and report a campaign in the order the examples document, then compare it with its expectations."""
    spec = importlib.util.spec_from_file_location("scripted_e2e_check", EXAMPLES / "scripted_e2e_check.py")
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    assert main(["context-eval", "run", str(example), "--out", str(campaign)]) == 0
    assert main(["context-eval", "grade", str(campaign)]) == 0
    assert main(["context-eval", "report", str(campaign)]) == 0
    return [row for row in check.compare(campaign, graded=True) if row[2] != row[3]]


@pytest.mark.skipif(os.environ.get("SERENO_DOCKER_TESTS") != "1", reason="set SERENO_DOCKER_TESTS=1 to run Docker")
def test_scripted_end_to_end_campaign(tmp_path):
    assert run_campaign(EXAMPLES / "scripted-e2e.yaml", tmp_path / "e2e") == []


def load_check(name: str):
    spec = importlib.util.spec_from_file_location(name, EXAMPLES / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(os.environ.get("SERENO_DOCKER_TESTS") != "1", reason="set SERENO_DOCKER_TESTS=1 to run Docker")
def test_run_attacker_offline_demo(tmp_path):
    """The offline demo in the order docs/run-local-attacker.md documents it, then its expectations."""
    check = load_check("run_attacker_check")
    configs = {side: str(EXAMPLES / f"run-attacker-{side}.yaml") for side in ("defended", "undefended")}
    campaigns = {side: tmp_path / side for side in configs}
    pairing = tmp_path / "pair.json"
    assert main(["context-eval", "pair", configs["defended"], configs["undefended"], "--out", str(pairing)]) == 0
    for side, config in configs.items():
        assert main(["context-eval", "run", config, "--out", str(campaigns[side])]) == 0
    summary = tmp_path / "summary.json"
    command = ["context-eval", "pair-summary", "--pair", str(pairing), "--out", str(summary)]
    assert main([*command, str(campaigns["defended"]), str(campaigns["undefended"])]) == 0
    rows = check.compare(json.loads(pairing.read_text()), campaigns)
    assert [row for row in rows if row[2] != row[3]] == []
