"""The examples validate, and the scripted end-to-end campaign measures what its scripts determine.

The end-to-end test runs containers from the local DeepSWE image and grades with its verifier; it runs only with
SERENO_DOCKER_TESTS=1.
"""

import importlib.util
import os
from pathlib import Path

import pytest

from sereno.context_eval.cli import main

EXAMPLES = Path(__file__).parents[1] / "examples" / "context-eval"
DATASET = Path("~/sereno-deepswe/deep-swe").expanduser()


@pytest.mark.skipif(not DATASET.is_dir(), reason="needs the DeepSWE checkout")
@pytest.mark.parametrize("name", ["scripted-canary", "scripted-e2e", "mini-swe", "clean-chain"])
def test_examples_validate(name):
    assert main(["context-eval", "validate", str(EXAMPLES / f"{name}.yaml")]) == 0


@pytest.mark.skipif(os.environ.get("SERENO_DOCKER_TESTS") != "1", reason="set SERENO_DOCKER_TESTS=1 to run Docker")
def test_scripted_end_to_end_campaign(tmp_path):
    spec = importlib.util.spec_from_file_location("scripted_e2e_check", EXAMPLES / "scripted_e2e_check.py")
    check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(check)
    campaign = tmp_path / "e2e"
    assert main(["context-eval", "run", str(EXAMPLES / "scripted-e2e.yaml"), "--out", str(campaign)]) == 0
    assert main(["context-eval", "report", str(campaign)]) == 0
    assert main(["context-eval", "grade", str(campaign)]) == 0
    mismatches = [row for row in check.compare(campaign, graded=True) if row[2] != row[3]]
    assert mismatches == []
