"""DeepSWE grading with a fake Docker runner: status mapping, image integrity and campaign bookkeeping."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from sereno.context_eval import evaluator
from sereno.context_eval.cli import main
from sereno.context_eval.contracts import Submission
from sereno.context_eval.evaluator import DeepSWEEvaluator, directory_digest, grade_campaign

BASE = "a" * 40
TASK_IMAGE = "fixture/task:v1"
TASK_ID = "sha256:task"


def passing(logs: Path, patch: str) -> None:
    (logs / "verifier").mkdir()
    reward = int(bool(patch))
    counts = {"f2p_total": 2, "f2p_passed": 2 * reward, "p2p_total": 2, "p2p_passed": 2}
    data = {"reward": reward, **counts, "f2p": float(reward), "p2p": 1.0, "partial": (2 + 2 * reward) / 4}
    (logs / "verifier" / "reward.json").write_text(json.dumps(data))


class FakeDocker:
    def __init__(self, verifier=passing):
        self.verifier = verifier
        self.calls = []
        self.images = {TASK_IMAGE: {"Id": TASK_ID, "RootFS": {"Layers": ["base-layer"]}}}
        self.patches = []

    def __call__(self, args, timeout=None):
        self.calls.append((args, timeout))
        if args[:2] == ["image", "inspect"]:
            image = self.images.get(args[2]) or next((i for i in self.images.values() if i["Id"] == args[2]), None)
            return subprocess.CompletedProcess(args, 1 if image is None else 0, json.dumps([image]), "")
        if args[0] == "build":
            tag = args[args.index("-t") + 1]
            layers = self.images[TASK_IMAGE]["RootFS"]["Layers"]
            key, value = args[args.index("--label") + 1].split("=", 1)
            self.images[tag] = {
                "Id": f"sha256:verifier-{len(self.builds) - 1}",
                "RootFS": {"Layers": [*layers, "tests"]},
                "Config": {"Labels": {key: value}},
            }
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "run":
            logs = Path(args[args.index("-v") + 1].removesuffix(":/logs"))
            patch = (logs / "artifacts" / "model.patch").read_text()
            self.patches.append(patch)
            if self.verifier == "timeout":
                raise subprocess.TimeoutExpired(args, timeout, output=b"partial output")
            self.verifier(logs, patch)
            return subprocess.CompletedProcess(args, 0, "verifier output\n", "")
        if args[:2] == ["rm", "-f"]:
            return subprocess.CompletedProcess(args, 1, "", "No such container")
        raise AssertionError(f"unexpected docker call {args}")

    def of(self, command):
        return [args for args, _ in self.calls if args[0] == command]

    @property
    def builds(self):
        return self.of("build")


@pytest.fixture
def dataset(tmp_path):
    root = tmp_path / "deep-swe"
    task = root / "tasks" / "demo"
    (task / "tests").mkdir(parents=True)
    (task / "solution").mkdir()
    (task / "instruction.md").write_text("Fixture task.")
    (task / "task.toml").write_text(
        f'[metadata]\nbase_commit_hash = "{BASE}"\n[environment]\ndocker_image = "{TASK_IMAGE}"\n'
        "[verifier]\ntimeout_sec = 42.0\n[verifier.environment]\nbuild_timeout_sec = 60.0\ncpus = 2\n"
        "memory_mb = 8192\n"
    )
    (task / "tests" / "Dockerfile").write_text(f"# fixture\nFROM --platform=linux/amd64 {TASK_IMAGE} AS verifier\n")
    (task / "tests" / "test.sh").write_text("echo fixture\n")
    (task / "solution" / "solution.patch").write_text("diff --git a/x b/x\n")
    return root


def submit(tmp_path, text="diff --git a/x b/x\n", image_id=TASK_ID, base=BASE):
    patch = tmp_path / "model.patch"
    patch.write_text(text)
    return Submission("demo", image_id, base, patch)


def test_graded_runs_verifier_offline_and_records_artifacts(dataset, tmp_path):
    docker = FakeDocker()
    grade = DeepSWEEvaluator(dataset, docker=docker).evaluate(submit(tmp_path), tmp_path / "out")
    tag = f"sereno-deepswe-verifier:demo-{directory_digest(dataset / 'tasks' / 'demo' / 'tests')[:12]}"
    assert grade["status"] == "graded" and grade["reward"] == 1 and grade["error"] is None
    assert (grade["f2p_passed"], grade["p2p_total"], grade["partial"]) == (2, 2, 1.0)
    assert grade["verifier"] == {"tag": tag, "id": "sha256:verifier-0"}
    assert grade["task_image_id"] == TASK_ID and grade["timeout_seconds"] == 42.0 and grade["apply_failed"] is False
    assert grade["logs"] == "grade/verifier" and len(grade["patch_sha256"]) == 64
    assert json.loads((tmp_path / "out" / "grade.json").read_text()) == grade
    assert (tmp_path / "out" / "grade" / "verifier" / "reward.json").exists()
    assert (tmp_path / "out" / "grade" / "verifier" / "test-stdout.txt").read_text() == "verifier output\n"
    (run, timeout), build = next(c for c in docker.calls if c[0][0] == "run"), docker.builds[0]
    assert build[-1] == str(dataset / "tasks" / "demo" / "tests") and timeout == 42.0
    assert run[run.index("--network") + 1] == "none" and "--rm" in run and run[run.index("--name") + 1]
    assert run[run.index("--cpus") + 1] == "2" and run[run.index("--memory") + 1] == "8192m"
    assert run[-4:-2] == ["sha256:verifier-0", "bash"] and run[-1].startswith(
        "bash /tests/test.sh; status=$?; chown -R"
    )
    assert docker.patches == ["diff --git a/x b/x\n"]


def test_apply_failed_is_reward_zero(dataset, tmp_path):
    def apply_failed(logs, patch):
        (logs / "verifier").mkdir()
        data = {"reward": 0, "f2p_total": 2, "f2p_passed": 0, "p2p_total": 2, "p2p_passed": 0, "apply_failed": 1}
        (logs / "verifier" / "reward.json").write_text(json.dumps(data | {"f2p": 0.0, "p2p": 0.0, "partial": 0.0}))

    grade = DeepSWEEvaluator(dataset, docker=FakeDocker(apply_failed)).evaluate(submit(tmp_path), tmp_path / "out")
    assert (grade["status"], grade["reward"], grade["apply_failed"], grade["f2p_total"]) == ("apply_failed", 0, True, 2)


@pytest.mark.parametrize("sentinel", [True, False])
def test_missing_reward_is_infrastructure_error(dataset, tmp_path, sentinel):
    def crashed(logs, patch):
        (logs / "verifier").mkdir()
        if sentinel:
            (logs / "verifier" / "reward.txt").write_text("-1\n")

    grade = DeepSWEEvaluator(dataset, docker=FakeDocker(crashed)).evaluate(submit(tmp_path), tmp_path / "out")
    assert grade["status"] == "grader_error" and grade["reward"] is None and grade["partial"] is None
    assert ("reward.txt -1" if sentinel else "no reward file") in grade["error"]


@pytest.mark.parametrize("linked_reward", [False, True])
def test_verifier_links_and_special_files_are_not_copied(dataset, tmp_path, linked_reward):
    host = tmp_path / "host.txt"
    host.write_text(json.dumps({"reward": 1}))

    def planted(logs, patch):
        passing(logs, patch)
        verifier = logs / "verifier"
        (verifier / "leak").symlink_to(host)
        (verifier / "dangling").symlink_to(tmp_path / "missing")
        os.mkfifo(verifier / "pipe")
        if linked_reward:
            (verifier / "reward.json").unlink()
            (verifier / "reward.json").symlink_to(host)

    grade = DeepSWEEvaluator(dataset, docker=FakeDocker(planted)).evaluate(submit(tmp_path), tmp_path / "out")
    copied = tmp_path / "out" / "grade" / "verifier"
    assert sorted(p.name for p in copied.iterdir()) == ([] if linked_reward else ["reward.json"]) + ["test-stdout.txt"]
    if linked_reward:
        assert grade["status"] == "grader_error" and "no reward file" in grade["error"]
    else:
        assert grade["status"] == "graded" and grade["reward"] == 1


def test_run_docker_decodes_invalid_utf8_leniently(tmp_path, monkeypatch):
    (tmp_path / "docker").write_text("#!/bin/sh\nprintf 'raw \\377 byte'\n")
    (tmp_path / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    assert evaluator.run_docker(["run"]).stdout == "raw \ufffd byte"


def test_missing_reward_reports_the_verifier_exit_code(dataset, tmp_path):
    docker = FakeDocker()

    def shell(args, timeout=None):
        if args[0] != "run":
            return docker(args, timeout)
        logs = args[args.index("-v") + 1].removesuffix(":/logs")
        command = args[-1].replace("/tests/test.sh", str(tmp_path / "absent.sh")).replace("/logs", logs)
        return subprocess.run(["bash", "-c", command], capture_output=True, text=True)

    grade = DeepSWEEvaluator(dataset, docker=shell).evaluate(submit(tmp_path), tmp_path / "out")
    assert grade["status"] == "grader_error" and "no reward file (exit code 127)" in grade["error"]


def test_timeout_force_removes_the_named_container(dataset, tmp_path):
    docker = FakeDocker("timeout")
    grade = DeepSWEEvaluator(dataset, docker=docker).evaluate(submit(tmp_path), tmp_path / "out")
    assert (grade["status"], grade["reward"], grade["partial"], grade["f2p_total"]) == (
        "verifier_timeout",
        0,
        0.0,
        None,
    )
    run = docker.of("run")[0]
    assert docker.of("rm") == [["rm", "-f", run[run.index("--name") + 1]]]
    assert (tmp_path / "out" / "grade" / "verifier" / "test-stdout.txt").read_text() == "partial output"


@pytest.mark.parametrize(
    ("image_id", "base", "present", "message"),
    [
        ("sha256:other", BASE, True, "not the graded sha256:other"),
        (TASK_ID, BASE, False, "not available locally"),
        (TASK_ID, "b" * 40, True, "does not match the task's"),
    ],
)
def test_identity_mismatch_never_grades(dataset, tmp_path, image_id, base, present, message):
    docker = FakeDocker()
    if not present:
        docker.images.clear()
    grade = DeepSWEEvaluator(dataset, docker=docker).evaluate(
        submit(tmp_path, image_id=image_id, base=base), tmp_path / "out"
    )
    assert grade["status"] == "grader_error" and message in grade["error"] and grade["reward"] is None
    assert not docker.builds and not docker.of("run")


def test_verifier_tag_is_reused_and_rebuilt_when_stale(dataset, tmp_path):
    docker = FakeDocker()
    grader = DeepSWEEvaluator(dataset, docker=docker)
    first = grader.evaluate(submit(tmp_path), tmp_path / "a")
    second = grader.evaluate(submit(tmp_path), tmp_path / "b")
    assert len(docker.builds) == 1 and first["verifier"] == second["verifier"]
    docker.images[TASK_IMAGE] = {"Id": "sha256:repulled", "RootFS": {"Layers": ["new-layer"]}}
    third = grader.evaluate(submit(tmp_path, image_id="sha256:repulled"), tmp_path / "c")
    assert len(docker.builds) == 2 and third["status"] == "graded" and third["verifier"]["id"] == "sha256:verifier-1"
    (dataset / "tasks" / "demo" / "tests" / "test.sh").write_text("echo changed\n")
    fourth = grader.evaluate(submit(tmp_path, image_id="sha256:repulled"), tmp_path / "d")
    assert len(docker.builds) == 3 and fourth["verifier"]["tag"] != third["verifier"]["tag"]
    docker.images[TASK_IMAGE] = {"Id": "sha256:new-config", "RootFS": {"Layers": ["new-layer"]}}
    fifth = grader.evaluate(submit(tmp_path, image_id="sha256:new-config"), tmp_path / "e")
    assert len(docker.builds) == 4 and fifth["verifier"]["id"] == "sha256:verifier-3"
    assert docker.builds[-1][docker.builds[-1].index("--label") + 1] == "sereno.base_id=sha256:new-config"


def test_build_failure_is_grader_error(dataset, tmp_path):
    docker = FakeDocker()
    original = docker.__call__

    def failing(args, timeout=None):
        if args[0] == "build":
            return subprocess.CompletedProcess(args, 1, "", "build broke")
        return original(args, timeout)

    grade = DeepSWEEvaluator(dataset, docker=failing).evaluate(submit(tmp_path), tmp_path / "out")
    assert grade["status"] == "grader_error" and "build broke" in grade["error"]


def session(campaign, path, status="complete", patch_status="ready", branch=None, **extra):
    directory = campaign / path
    directory.mkdir(parents=True)
    result = {"task_id": "demo", "status": status, "patch_status": patch_status, "image": {"id": TASK_ID}}
    (directory / "result.json").write_text(json.dumps(result | {"base_commit": BASE} | extra))
    (directory / "model.patch").write_text("diff --git a/x b/x\n")
    if branch:
        (directory / "branch.json").write_text(json.dumps(branch))
    return directory


@pytest.fixture
def campaign(tmp_path, dataset):
    root = tmp_path / "campaign"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({"config": {"dataset_root": str(dataset)}}))
    one, two = "cases/demo--v1--r001/arms", "cases/demo--v2--r001/arms"
    session(root, f"{one}/clean/sessions/001-exposure")
    session(root, f"{one}/attack_carry/sessions/001-exposure")
    session(root, f"{one}/attack_carry/sessions/002-probe", patch_status="ambiguous", patch_error="overlap at x")
    session(root, f"{one}/attack_reset/sessions/002-probe", status="invalid", patch_status="missing")
    old = {"shared_exposure": f"{one}/attack_carry/sessions/001-exposure", "origin": "attack_carry"}
    session(root, f"{one}/attack_reset/sessions/001-exposure", branch=old)
    new = {"shared_from": f"{one}/clean/sessions/001-exposure", "origin_arm": "clean"}
    session(root, f"{two}/clean/sessions/001-exposure", branch=new)
    return root


def test_campaign_grades_origins_once_and_shares_copies(campaign):
    docker = FakeDocker()
    summary = grade_campaign(campaign, docker=docker)
    one = campaign / "cases/demo--v1--r001/arms"
    assert len(docker.of("run")) == 2
    assert summary["statuses"] == {"graded": 4, "not_gradable": 1} and summary["written"] == 5
    assert summary["sessions"] == 6 and summary["kept"] == 0
    assert summary["failures"] == [
        {
            "session": "cases/demo--v1--r001/arms/attack_carry/sessions/002-probe",
            "status": "not_gradable",
            "error": "overlap at x",
        }
    ]
    origin = json.loads((one / "attack_carry/sessions/001-exposure/grade.json").read_text())
    copy = one / "attack_reset/sessions/001-exposure"
    assert json.loads((copy / "grade.json").read_text()) == origin | {
        "shared_from": "cases/demo--v1--r001/arms/attack_carry/sessions/001-exposure"
    }
    assert not (copy / "grade").exists()
    clean_copy = campaign / "cases/demo--v2--r001/arms/clean/sessions/001-exposure/grade.json"
    assert json.loads(clean_copy.read_text())["shared_from"] == "cases/demo--v1--r001/arms/clean/sessions/001-exposure"
    ambiguous = json.loads((one / "attack_carry/sessions/002-probe/grade.json").read_text())
    assert ambiguous["status"] == "not_gradable" and ambiguous["reward"] is None
    assert not (one / "attack_reset/sessions/002-probe/grade.json").exists()


def test_existing_grades_are_kept_unless_forced(campaign):
    grade_campaign(campaign, docker=FakeDocker())
    target = campaign / "cases/demo--v1--r001/arms/clean/sessions/001-exposure/grade.json"
    marker = json.loads(target.read_text()) | {"status": "graded", "reward": 0}
    target.write_text(json.dumps(marker))
    docker = FakeDocker()
    summary = grade_campaign(campaign, docker=docker)
    assert not docker.calls and summary["kept"] == 5 and summary["written"] == 0
    assert json.loads(target.read_text())["reward"] == 0
    forced = grade_campaign(campaign, docker=docker, force=True)
    assert len(docker.of("run")) == 2 and forced["written"] == 5
    assert json.loads(target.read_text())["reward"] == 1


def test_branch_outside_campaign_is_rejected(campaign):
    session(campaign, "cases/x/arms/attack_reset/sessions/001-exposure", branch={"shared_from": "../elsewhere"})
    with pytest.raises(ValueError, match="does not point at a session"):
        grade_campaign(campaign, docker=FakeDocker())


def test_cli_grade_uses_manifest_dataset(campaign, monkeypatch, capsys):
    docker = FakeDocker()
    monkeypatch.setattr(evaluator, "run_docker", docker)
    assert main(["context-eval", "grade", str(campaign), "--workers", "2"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["statuses"] == {"graded": 4, "not_gradable": 1} and len(docker.of("run")) == 2
    docker.images.clear()
    assert main(["context-eval", "grade", str(campaign), "--force"]) == 1
    assert json.loads(capsys.readouterr().out)["statuses"]["grader_error"] == 4


def test_cli_grade_check(dataset, tmp_path, monkeypatch, capsys):
    docker = FakeDocker()
    monkeypatch.setattr(evaluator, "run_docker", docker)
    monkeypatch.chdir(tmp_path)
    assert main(["context-eval", "grade-check", "--dataset", str(dataset), "demo", "--workers", "2"]) == 0
    table = json.loads(capsys.readouterr().out)
    assert table["passed"] and [(r["patch"], r["reward"], r["ok"]) for r in table["results"]] == [
        ("gold", 1, True),
        ("empty", 0, True),
    ]
    assert Path(table["out"]).parent == tmp_path / "runs" / "context-eval" / "grade-check"
    assert sorted(docker.patches) == ["", "diff --git a/x b/x\n"]
    monkeypatch.setattr(evaluator, "run_docker", FakeDocker(lambda logs, patch: passing(logs, "x")))
    out = tmp_path / "check"
    assert main(["context-eval", "grade-check", "--dataset", str(dataset), "demo", "--out", str(out)]) == 1
    assert [r["ok"] for r in json.loads(capsys.readouterr().out)["results"]] == [True, False]
    assert main(["context-eval", "grade-check", "--dataset", str(dataset), "demo", "--out", str(out)]) == 2
