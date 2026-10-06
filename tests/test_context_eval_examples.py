"""The examples validate, and the scripted end-to-end campaigns measure what their scripts determine.

The end-to-end tests run containers from the local DeepSWE image and grade with its verifier; they run only with
SERENO_DOCKER_TESTS=1.
"""

import importlib.util
import os
from pathlib import Path

import pytest
import yaml

from sereno.context_eval.cli import main

EXAMPLES = Path(__file__).parents[1] / "examples" / "context-eval"
DATASET = Path("~/sereno-deepswe/deep-swe").expanduser()
EXAMPLE_NAMES = ["scripted-canary", "scripted-e2e", "scripted-e2e-repair", "mini-swe", "clean-chain"]


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


@pytest.mark.skipif(os.environ.get("SERENO_DOCKER_TESTS") != "1", reason="set SERENO_DOCKER_TESTS=1 to run Docker")
@pytest.mark.parametrize("retract", [True, False])
def test_scripted_repair_campaign(tmp_path, retract):
    """The example retracts the planted content; the same campaign without the retraction leaves it in place."""
    config = yaml.safe_load((EXAMPLES / "scripted-e2e-repair.yaml").read_text())
    config["dataset_root"] = str(DATASET)
    config["repair"]["retract"] = retract
    example = tmp_path / "repair.yaml"
    example.write_text(yaml.safe_dump(config))
    assert run_campaign(example, tmp_path / "repair") == []
