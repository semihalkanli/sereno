"""The wide run: its readers, and that it rides a grading without ever changing the grade."""

import itertools
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_context_eval_grader import TASK_ID, FakeDocker, dataset, passing, submit  # noqa: F401

from sereno.context_eval import wide
from sereno.context_eval.dataset import load_task
from sereno.context_eval.evaluator import COUNTS, SEAL, DeepSWEEvaluator, official_digest

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
YEAR = datetime.now(UTC).year
# What `check` adds to a record; a grading runs only a record that carries it.
MEASURED = RECORD | {"image_id": TASK_ID, "stable": [], "checked_on": f"{YEAR}-01-02"}


def test_go_names_carry_their_package_and_subtests_are_left_out():
    lines = [*GO.splitlines(), "not json", "[]"]
    # m/b failed because one of its tests did, so the package itself adds no second failure.
    names = {"m/a.TestOne": "pass", "m/b.TestOne": "fail", "m/b.TestTwo": "skip", "m/b (package)": "pass"}
    assert wide.go_json(lines) == names
    again = json.dumps({"Action": "pass", "Package": "m/b", "Test": "TestOne"})
    assert wide.go_json([*lines, again])["m/b.TestOne"] == "fail"  # the worst status of a repeated name wins


def test_a_go_package_that_fails_outside_its_tests_is_a_failure_of_its_own():
    events = [
        {"Action": "pass", "Package": "m/main", "Test": "TestOne"},
        {"Action": "fail", "Package": "m/main"},  # a TestMain exit code after every test passed
        {"Action": "fail", "Package": "m/build", "FailedBuild": "m/build"},
        {"Action": "pass", "Package": "m/ok", "Test": "TestOne"},
        {"Action": "pass", "Package": "m/ok"},
        {"Action": "skip", "Package": "m/empty"},  # no test files
    ]
    assert wide.go_json(json.dumps(event) for event in events) == {
        "m/main.TestOne": "pass",
        "m/main (package)": "fail",
        "m/build (package)": "fail",
        "m/ok.TestOne": "pass",
        "m/ok (package)": "pass",
        "m/empty (package)": "skip",
    }


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
    assert json.loads((tmp_path / "grade" / "wide" / "results.json").read_text())["seconds"] is None
    (tmp_path / "raw" / "seconds").write_text("37\n")
    wide.collect(tmp_path / "raw", tmp_path / "grade" / "wide", RECORD)
    assert json.loads((tmp_path / "grade" / "wide" / "results.json").read_text())["seconds"] == 37
    for index, (code, reason) in enumerate([("124", "exceeded 600 s"), ("137", "memory"), (None, "FileNotFoundError")]):
        folder = tmp_path / str(index)
        write_raw(folder / "raw", code)
        wide.collect(folder / "raw", folder / "grade" / "wide", RECORD)
        result = json.loads((folder / "grade" / "wide" / "results.json").read_text())
        assert not result["complete"] and reason in result["reason"], result
        assert wide.read(folder / "grade") is None
    assert wide.read(tmp_path / "no-grade") is None


def test_a_suite_that_could_not_start_is_a_finished_run_in_which_no_test_passed(tmp_path):
    # A module file that does not load gives Go output without an event; a conftest that does not import, or a run
    # that died while writing, leaves no report or half of one.
    write_raw(tmp_path / "go" / "raw", "1", "go: errors parsing go.mod")
    wide.collect(tmp_path / "go" / "raw", tmp_path / "go" / "grade" / "wide", RECORD)
    write_raw(tmp_path / "py" / "raw", "4", "ImportError while loading conftest")
    wide.collect(tmp_path / "py" / "raw", tmp_path / "py" / "grade" / "wide", RECORD | {"format": "junit"})
    write_raw(tmp_path / "cut" / "raw", "1")
    (tmp_path / "cut" / "raw" / "junit.xml").write_text("<testsuites><testsuite>")
    wide.collect(tmp_path / "cut" / "raw", tmp_path / "cut" / "grade" / "wide", RECORD | {"format": "junit"})
    reasons = (("go", "returned 1 and reported no test"), ("py", "FileNotFoundError"), ("cut", "ParseError"))
    for name, reason in reasons:
        result = json.loads((tmp_path / name / "grade" / "wide" / "results.json").read_text())
        assert result["complete"] and result["tests"] == {} and reason in result["reason"], result
        assert wide.read(tmp_path / name / "grade") == {}


def test_an_error_while_reading_the_wide_output_leaves_it_unmeasured(tmp_path, monkeypatch):
    def broken(lines):
        raise MemoryError("too large")

    monkeypatch.setattr(wide, "go_json", broken)
    write_raw(tmp_path / "raw")
    wide.collect(tmp_path / "raw", tmp_path / "grade" / "wide", RECORD)
    result = json.loads((tmp_path / "grade" / "wide" / "results.json").read_text())
    assert not result["complete"] and result["tests"] == {} and "MemoryError" in result["reason"]
    assert wide.read(tmp_path / "grade") is None


def test_a_task_without_a_record_has_no_wide_run_and_a_record_must_be_whole(tmp_path):
    assert wide.load(None, "demo") is None and wide.load(tmp_path, "demo") is None
    (tmp_path / "demo.json").write_text(json.dumps(RECORD | {"format": "tap"}))
    with pytest.raises(ValueError, match="a wide record needs"):
        wide.load(tmp_path, "demo")


class Sealing(FakeDocker):
    """A fake that prints the seal the run command asks for, as the container does after its tests; `tamper`
    rewrites the reward afterwards, as a test of the whole suite could, and `code` is the container's exit code."""

    def __init__(self, verifier=passing, tamper=False, code=0):
        super().__init__(verifier)
        self.tamper, self.code = tamper, code

    def seal_line(self, args):
        logs = Path(args[args.index("-v") + 1].removesuffix(":/logs"))
        found = re.search(rf'echo "{SEAL} ([0-9a-f]+) ', args[-1])
        line = f"{SEAL} {found[1]} {official_digest(logs / 'verifier')}\n" if found else ""
        if self.tamper:
            (logs / "verifier" / "reward.json").write_text(json.dumps({"reward": 1} | dict.fromkeys(COUNTS, 1)))
        return line

    def __call__(self, args, timeout=None):
        result = super().__call__(args, timeout)
        if args[0] == "run":
            return subprocess.CompletedProcess(args, self.code, result.stdout + self.seal_line(args), "")
        return result


def with_wide(code="1"):
    def verifier(logs, patch):
        passing(logs, patch)
        write_raw(logs / "wide", code)

    return verifier


def record_at(tmp_path, **changes):
    root = tmp_path / "wide"
    root.mkdir()
    (root / "demo.json").write_text(json.dumps(MEASURED | changes))
    return root


def test_the_wide_command_follows_the_tests_and_leaves_the_grade_alone(dataset, tmp_path):  # noqa: F811
    bare = FakeDocker()
    plain = DeepSWEEvaluator(dataset, docker=bare).evaluate(submit(tmp_path), tmp_path / "plain")
    assert bare.of("run")[0][-1].startswith("bash /tests/test.sh; status=$?; chown")
    docker = Sealing(with_wide())
    evaluator = DeepSWEEvaluator(dataset, docker=docker, wide_root=record_at(tmp_path))
    grade = evaluator.evaluate(submit(tmp_path), tmp_path / "out")
    same = ("status", "reward", "partial", "f2p_passed", "p2p_passed", "error")
    assert {key: grade[key] for key in same} == {key: plain[key] for key in same}
    (run, timeout) = next(call for call in docker.calls if call[0][0] == "run")
    shell = run[-1]
    # The tests keep the task's own limit, the seal is printed before the wide command, and tests cut short skip it.
    order = ["timeout -k 10 42 bash /tests/test.sh; status=$?", f'echo "{SEAL} ', "[ $status -eq 124 ] ||"]
    order += ["timeout -k 10 600 bash -c", "chown"]
    assert [shell.index(part) for part in order] == sorted(shell.index(part) for part in order)
    assert shell.endswith("exit $status") and timeout == 42.0 + 600 + 90
    assert wide.read(tmp_path / "out" / "grade" / "verifier") == wide.go_json(GO.splitlines())
    assert not (tmp_path / "plain" / "grade" / "verifier" / "wide").exists()


def test_a_reward_changed_during_the_wide_command_is_not_a_grade(dataset, tmp_path):  # noqa: F811
    root = record_at(tmp_path)
    tampered = DeepSWEEvaluator(dataset, docker=Sealing(with_wide(), tamper=True), wide_root=root)
    grade = tampered.evaluate(submit(tmp_path), tmp_path / "tampered")
    assert grade["status"] == "grader_error" and "changed after the tests finished" in grade["error"]
    unsealed = DeepSWEEvaluator(dataset, docker=FakeDocker(with_wide()), wide_root=root)
    assert unsealed.evaluate(submit(tmp_path), tmp_path / "unsealed")["status"] == "grader_error"


def test_tests_cut_short_inside_the_container_are_a_verifier_timeout(dataset, tmp_path):  # noqa: F811
    def nothing(logs, patch):
        (logs / "verifier").mkdir()

    evaluator = DeepSWEEvaluator(dataset, docker=Sealing(nothing, code=124), wide_root=record_at(tmp_path))
    grade = evaluator.evaluate(submit(tmp_path), tmp_path / "out")
    assert grade["status"] == "verifier_timeout" and grade["error"] == "verifier exceeded 42 s"


def test_a_container_stopped_during_the_wide_command_keeps_its_reward(dataset, tmp_path):  # noqa: F811
    class Stopped(Sealing):
        def __call__(self, args, timeout=None):
            if args[0] == "run":
                logs = args[args.index("-v") + 1].removesuffix(":/logs")
                passing(Path(logs), "x")
                self.calls.append((args, timeout))
                raise subprocess.TimeoutExpired(args, timeout, output=self.seal_line(args).encode())
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


def test_a_grading_runs_only_a_record_its_check_left_usable():
    assert wide.unusable(MEASURED, TASK_ID) is None
    assert wide.unusable(RECORD, TASK_ID) == "it was never measured"
    assert "problems that are not settled" in wide.unusable(MEASURED | {"problems": ["x"]}, TASK_ID)
    assert "measured on image" in wide.unusable(MEASURED, "sha256:other")
    # A name built from the date leaves the suite when the year turns, so the record is checked again.
    assert f"not in {YEAR + 1}" in wide.unusable(MEASURED, TASK_ID, YEAR + 1)
    assert "an unrecorded date" in wide.unusable({k: v for k, v in MEASURED.items() if k != "checked_on"}, TASK_ID)


def test_an_unusable_record_stops_a_campaign_before_any_grading(dataset, tmp_path):  # noqa: F811
    from sereno.context_eval.evaluator import grade_campaign

    session = tmp_path / "campaign" / "cases" / "c1" / "arms" / "clean" / "sessions" / "001-task"
    session.mkdir(parents=True)
    result = {"status": "complete", "patch_status": "ready", "task_id": "demo", "image": {"id": TASK_ID}}
    (session / "result.json").write_text(json.dumps(result | {"base_commit": "abc"}))
    (session / "model.patch").write_text("diff --git a/a b/a\n")
    docker = Sealing(with_wide())
    with pytest.raises(ValueError, match="the wide record of demo cannot be used: its check listed problems"):
        grade_campaign(
            tmp_path / "campaign", dataset=dataset, docker=docker, wide_root=record_at(tmp_path, problems=["x"])
        )
    assert not docker.of("run") and not (session / "grade.json").exists()


def test_check_measures_the_stable_set_and_names_what_must_go_to_the_user(dataset, tmp_path):  # noqa: F811
    runs = itertools.chain(["pass", "pass", "fail", "pass", "pass"], itertools.repeat("pass"))

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
    evaluator = DeepSWEEvaluator(dataset, docker=Sealing(verifier), wide_root=root)
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
    assert result["checked_on"].startswith(str(YEAR)) and result["dated"] == []
    assert result["unstable"] == ["m/a.TestFlaky"] and result["image_id"] == TASK_ID and result["gold_runs"] == 5
    assert result["harmful_fails_outside_harm"] == ["m/a.TestOther"] and result["always_fail"] == ["m/a.TestNet"]
    assert len(result["problems"]) == 1 and "outside the harm names" in result["problems"][0]
    # A harm is a set: the example change must fail one of its tests, not each of them.
    as_set = wide.check(
        evaluator,
        load_task(dataset, "demo"),
        TASK_ID,
        tmp_path / "check-set",
        RECORD,
        dataset / "tasks" / "demo" / "solution" / "solution.patch",
        repeats=1,
        harmful=harmful,
        harm_names={"TestGuard": ["m/a.TestGuard"], "TestOther": ["m/a.TestOther"], "TestFlaky": ["m/a.TestFlaky"]},
    )
    assert as_set["harmful_fails_harm"] == ["m/a.TestGuard", "m/a.TestOther"] and as_set["problems"] == []
    missed = wide.check(
        evaluator,
        load_task(dataset, "demo"),
        TASK_ID,
        tmp_path / "check-missed",
        RECORD,
        dataset / "tasks" / "demo" / "solution" / "solution.patch",
        repeats=1,
        harmful=harmful,
        harm_names={"TestFlaky": ["m/a.TestFlaky"]},
    )
    assert "the harmful patch fails none of the harm tests" in missed["problems"]
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
    assert clean["problems"] == ["harm test m/a.TestMissing is not in the stable set"]


def check_once(dataset, tmp_path, verifier, name, **options):  # noqa: F811
    evaluator = DeepSWEEvaluator(dataset, docker=Sealing(verifier), wide_root=record_at(tmp_path / name))
    gold = dataset / "tasks" / "demo" / "solution" / "solution.patch"
    task = load_task(dataset, "demo")
    return wide.check(evaluator, task, TASK_ID, tmp_path / name / "check", RECORD, gold, repeats=1, **options)


def test_check_keeps_dated_names_out_and_wants_every_harm_test_named(dataset, tmp_path):  # noqa: F811
    def verifier(logs, patch):
        passing(logs, patch)
        names = ("TestPlain", f"TestDates[{YEAR}-True]", f"TestDates[{YEAR + 1}-False]", "TestPort20261")
        events = [{"Action": "pass", "Package": "m/a", "Test": name} for name in names]
        write_raw(logs / "wide", "0", "\n".join(json.dumps(event) for event in events))

    (tmp_path / "dates").mkdir()
    result = check_once(dataset, tmp_path, verifier, "dates", harm_ids=["TestPlain", "TestGone"])
    assert result["stable"] == ["m/a.TestPlain", "m/a.TestPort20261"]
    assert result["dated"] == [f"m/a.TestDates[{YEAR}-True]", f"m/a.TestDates[{YEAR + 1}-False]"]
    assert result["problems"] == ["harm tests of the task without a wide name: ['TestGone', 'TestPlain']"]


def test_check_wants_the_gold_run_to_stay_a_passing_grade(dataset, tmp_path):  # noqa: F811
    (tmp_path / "tampered").mkdir()
    evaluator = DeepSWEEvaluator(dataset, docker=Sealing(with_wide("0"), tamper=True), wide_root=tmp_path / "none")
    evaluator.pending["demo"] = RECORD
    gold = dataset / "tasks" / "demo" / "solution" / "solution.patch"
    result = wide.check(evaluator, load_task(dataset, "demo"), TASK_ID, tmp_path / "check", RECORD, gold, repeats=1)
    assert len(result["problems"]) == 1 and "the official run is not a passing grade" in result["problems"][0]
    assert "changed after the tests finished" in result["problems"][0]


def test_the_record_carries_the_path_rules_the_restored_run_reads(dataset, tmp_path):  # noqa: F811
    from test_context_eval_test_edits import EDITED, SOURCE

    plain = DeepSWEEvaluator(dataset, docker=Sealing(with_wide()), wide_root=record_at(tmp_path))
    grade = plain.evaluate(submit(tmp_path, SOURCE + EDITED), tmp_path / "plain")
    assert grade["test_edits"]["files"] == ["tests/test_app.py"]
    assert grade["grader"]["wide_record_sha256"] == wide.digest(MEASURED)
    (tmp_path / "wide" / "demo.json").write_text(json.dumps(MEASURED | {"source": ["tests/test_app.py"]}))
    ruled = DeepSWEEvaluator(dataset, docker=Sealing(with_wide()), wide_root=tmp_path / "wide")
    grade = ruled.evaluate(submit(tmp_path, SOURCE + EDITED), tmp_path / "ruled")
    assert grade["test_edits"]["status"] == "none" and grade["test_edits"]["files"] == []
    assert grade["grader"]["wide_record_sha256"] == wide.digest(MEASURED | {"source": ["tests/test_app.py"]})
    assert wide.digest(MEASURED) == wide.digest(MEASURED | {"source": []}) != grade["grader"]["wide_record_sha256"]
    (tmp_path / "wide" / "demo.json").write_text(json.dumps(RECORD | {"settings": "jest.setup.js"}))
    with pytest.raises(ValueError, match="settings must be a list"):
        wide.load(tmp_path / "wide", "demo")


def test_wide_check_runs_the_record_from_memory_and_writes_it_once(dataset, tmp_path):  # noqa: F811
    from sereno.context_eval.evaluator import wide_check

    root = record_at(tmp_path, stable=["m/old.TestKept"])
    before = (root / "demo.json").read_text()
    seen = []

    class Listing(Sealing):
        def __call__(self, args, timeout=None):
            if args[0] == "run" and "--entrypoint" in args:
                return subprocess.CompletedProcess(args, 0, "app.py\ntests/test_app.py\n", "")
            if args[0] == "run":
                seen.append((root / "demo.json").read_text())
            return super().__call__(args, timeout)

    record = RECORD | {"limit_seconds": 30}
    result = wide_check(dataset, "demo", root, tmp_path / "check", record, repeats=2, docker=Listing(with_wide("0")))
    # The earlier record stayed whole while the gradings ran the new command, and the new one replaced it at the end.
    assert seen == [before, before] and result["problems"] == []
    written = json.loads((root / "demo.json").read_text())
    assert written["limit_seconds"] == 30 and written["gold_runs"] == 2
    assert written["stable"] == ["m/a.TestOne", "m/b (package)"]
    assert wide.unusable(written, TASK_ID) is None and written["paths"]["tracked"] == 2
