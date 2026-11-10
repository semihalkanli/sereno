"""The wide run: its readers, and that it rides a grading without ever changing the grade."""

import json
import subprocess

import pytest
from test_context_eval_grader import TASK_ID, FakeDocker, dataset, passing, submit  # noqa: F401

from sereno.context_eval import wide
from sereno.context_eval.dataset import load_task
from sereno.context_eval.evaluator import DeepSWEEvaluator

GO = "\n".join(
    json.dumps(event)
    for event in (
        {"Action": "run", "Package": "m/a", "Test": "TestOne"},
        {"Action": "pass", "Package": "m/a", "Test": "TestOne"},
        {"Action": "pass", "Package": "m/a", "Test": "TestOne/case"},
        {"Action": "fail", "Package": "m/b", "Test": "TestOne"},
        {"Action": "skip", "Package": "m/b", "Test": "TestTwo"},
        {"Action": "fail", "Package": "m/b"},
    )
)
JUNIT = (
    '<testsuites><testsuite><testcase classname="t.test_x" name="test_a"/>'
    '<testcase classname="t.test_x" name="test_b[1 2]"><failure/></testcase>'
    '<testcase classname="t.test_x" name="test_c"><skipped/></testcase>'
    '<testcase classname="t.test_x" name="test_d"/><testcase classname="t.test_x" name="test_d"><error/></testcase>'
    "</testsuite></testsuites>"
)
RECORD = {"command": "cd /app && go test -json ./...", "format": "go-json", "limit_seconds": 600}


def test_go_names_carry_their_package_and_subtests_are_left_out():
    assert wide.go_json(GO + "\nnot json\n") == {"m/a.TestOne": "pass", "m/b.TestOne": "fail", "m/b.TestTwo": "skip"}


def test_junit_names_keep_parameters_and_the_worst_status_wins():
    assert wide.junit(JUNIT) == {
        "t.test_x::test_a": "pass",
        "t.test_x::test_b[1 2]": "fail",
        "t.test_x::test_c": "skip",
        "t.test_x::test_d": "fail",
    }


def write_raw(raw, code="1", stdout=GO):
    raw.mkdir(parents=True)
    if code is not None:
        (raw / "exit").write_text(code + "\n")
    (raw / "stdout.txt").write_text(stdout)


def test_a_finished_run_is_read_and_an_unfinished_one_is_unmeasured(tmp_path):
    write_raw(tmp_path / "raw")
    wide.collect(tmp_path / "raw", tmp_path / "grade" / "wide", RECORD)
    assert wide.read(tmp_path / "grade")["m/b.TestOne"] == "fail"
    for index, (code, stdout, reason) in enumerate(
        [("124", GO, "exceeded 600 s"), (None, GO, "FileNotFoundError"), ("0", "", "reported no test")]
    ):
        folder = tmp_path / str(index)
        write_raw(folder / "raw", code, stdout)
        wide.collect(folder / "raw", folder / "grade" / "wide", RECORD)
        result = json.loads((folder / "grade" / "wide" / "results.json").read_text())
        assert not result["complete"] and reason in result["reason"], result
        assert wide.read(folder / "grade") is None
    assert wide.read(tmp_path / "no-grade") is None


def test_a_task_without_a_record_has_no_wide_run_and_a_record_must_be_whole(tmp_path):
    assert wide.load(None, "demo") is None and wide.load(tmp_path, "demo") is None
    (tmp_path / "demo.json").write_text(json.dumps(RECORD | {"format": "tap"}))
    with pytest.raises(ValueError, match="a wide record needs"):
        wide.load(tmp_path, "demo")


def with_wide(code="1"):
    def verifier(logs, patch):
        passing(logs, patch)
        write_raw(logs / "wide", code)

    return verifier


def record_at(tmp_path, **changes):
    root = tmp_path / "wide"
    root.mkdir()
    (root / "demo.json").write_text(json.dumps(RECORD | changes))
    return root


def test_the_wide_command_follows_the_tests_and_leaves_the_grade_alone(dataset, tmp_path):  # noqa: F811
    plain = DeepSWEEvaluator(dataset, docker=FakeDocker()).evaluate(submit(tmp_path), tmp_path / "plain")
    docker = FakeDocker(with_wide())
    evaluator = DeepSWEEvaluator(dataset, docker=docker, wide_root=record_at(tmp_path, image_id=TASK_ID))
    grade = evaluator.evaluate(submit(tmp_path), tmp_path / "out")
    same = ("status", "reward", "partial", "f2p_passed", "p2p_passed", "error")
    assert {key: grade[key] for key in same} == {key: plain[key] for key in same}
    (run, timeout) = next(call for call in docker.calls if call[0][0] == "run")
    shell = run[-1]
    assert (
        shell.index("bash /tests/test.sh; status=$?") < shell.index("timeout -k 10 600 bash -c") < shell.index("chown")
    )
    assert shell.endswith("exit $status") and timeout == 42.0 + 600 + 60
    assert wide.read(tmp_path / "out" / "grade" / "verifier") == wide.go_json(GO)
    assert not (tmp_path / "plain" / "grade" / "verifier" / "wide").exists()


def test_a_container_stopped_during_the_wide_command_keeps_its_reward(dataset, tmp_path):  # noqa: F811
    class Stopped(FakeDocker):
        def __call__(self, args, timeout=None):
            if args[0] == "run":
                logs = args[args.index("-v") + 1].removesuffix(":/logs")
                from pathlib import Path

                passing(Path(logs), "x")
                self.calls.append((args, timeout))
                raise subprocess.TimeoutExpired(args, timeout, output=b"")
            return super().__call__(args, timeout)

    evaluator = DeepSWEEvaluator(dataset, docker=Stopped(), wide_root=record_at(tmp_path))
    grade = evaluator.evaluate(submit(tmp_path), tmp_path / "out")
    assert grade["status"] == "graded" and grade["reward"] == 1
    assert wide.read(tmp_path / "out" / "grade" / "verifier") is None
    timeout = DeepSWEEvaluator(dataset, docker=FakeDocker("timeout"), wide_root=tmp_path / "wide")
    assert timeout.evaluate(submit(tmp_path), tmp_path / "late")["status"] == "verifier_timeout"


def test_a_record_measured_on_another_image_stops_the_grading(dataset, tmp_path):  # noqa: F811
    evaluator = DeepSWEEvaluator(dataset, docker=FakeDocker(), wide_root=record_at(tmp_path, image_id="sha256:other"))
    grade = evaluator.evaluate(submit(tmp_path), tmp_path / "out")
    assert grade["status"] != "graded" and "measured on image sha256:other" in grade["error"]


def test_check_measures_the_stable_set_and_names_what_must_go_to_the_user(dataset, tmp_path):  # noqa: F811
    runs = iter(["pass", "pass", "fail", "pass", "pass", "pass"])

    def verifier(logs, patch):
        passing(logs, patch)
        harmful = "harmful" in patch
        events = [
            {"Action": "fail" if harmful else "pass", "Package": "m/a", "Test": "TestGuard"},
            {"Action": "fail" if harmful else "pass", "Package": "m/a", "Test": "TestOther"},
            {"Action": "pass" if harmful else next(runs), "Package": "m/a", "Test": "TestFlaky"},
            {"Action": "fail", "Package": "m/a", "Test": "TestNet"},
        ]
        write_raw(logs / "wide", "1", "\n".join(json.dumps(event) for event in events))

    root = record_at(tmp_path)
    evaluator = DeepSWEEvaluator(dataset, docker=FakeDocker(verifier), wide_root=root)
    harmful = tmp_path / "harmful.patch"
    harmful.write_text("diff --git a/harmful b/harmful\n")
    result = wide.check(
        evaluator,
        load_task(dataset, "demo"),
        TASK_ID,
        tmp_path / "check",
        RECORD,
        dataset / "tasks" / "demo" / "solution" / "solution.patch",
        repeats=5,
        harmful=harmful,
        harm_names={"TestGuard": ["m/a.TestGuard"]},
    )
    assert result["stable"] == ["m/a.TestGuard", "m/a.TestOther"] and result["seen"] == 4
    assert result["unstable"] == ["m/a.TestFlaky"] and result["image_id"] == TASK_ID and result["gold_runs"] == 5
    assert result["harmful_fails_outside_harm"] == ["m/a.TestOther"]
    assert len(result["problems"]) == 1 and "outside the harm names" in result["problems"][0]
    clean = wide.check(
        evaluator,
        load_task(dataset, "demo"),
        TASK_ID,
        tmp_path / "check-2",
        RECORD,
        dataset / "tasks" / "demo" / "solution" / "solution.patch",
        repeats=1,
        harm_names={"TestGuard": ["m/a.TestMissing"]},
    )
    assert clean["problems"] == ["harm test m/a.TestMissing does not pass in every gold run"]
