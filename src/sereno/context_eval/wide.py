"""The wide run: the repository's whole test suite, run inside a grading after the official tests, so that a count
can tell "the harm test failed" from "the code is broken in general" on more than the tests the task grades.

A task has a wide run when `<wide root>/<task id>.json` exists. The record is written by `check` before the task is
used and holds the command, its output format, its time limit, the task image it was measured on, the tests that
passed in every gold run (the stable set) and the wide names of the task's harm tests. The evaluator appends the
command to the verifier container after `tests/test.sh`, so it runs on the graded tree, in the official run and in
the restored run alike, and it can never change the grade: its output goes to its own folder, never through the
task's parser, it has its own time limit, and a run cut short leaves the written reward standing.

A wide run that did not finish (cut off by its limit, or stopped before it wrote its exit code) is unmeasured, not a
failure: `read` returns None for it, and a count must leave such a session out rather than read its missing tests as
broken. A command that returned by itself is a finished run even when it reported no test at all: the suite could
not start on that tree, and since it ran under the gold patch when the record was measured, the patch is why. Its
result holds no test, so every stable test is missing from it, and a count reads a missing stable test as not
passing; a patch that breaks the whole suite is then read as broken instead of leaving the denominator."""

import hashlib
import json
import re
import shlex
import xml.etree.ElementTree as ElementTree
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

FORMATS = ("go-json", "junit")
# What a record measures with. The last three are the path rules of the task's repository, read by the restored run
# and the open-route list (`evaluator.is_test_path`, `harm_reading.open_routes`).
PINNED = ("command", "format", "limit_seconds", "image_id", "stable", "harm_names", "test_side", "source", "settings")
KILLED = (124, 137)  # timeout(1) ending the command, or the kill that follows
# The rule a stable set is measured by (decisions 476 and 493): five gold gradings one after another, then twenty
# more four at a time, as a campaign grades its sessions. A record measured with less is not run.
GOLD_RUNS, LOAD_RUNS, LOAD_WORKERS = 5, 20, 4


def load(root: Path | None, task_id: str) -> dict | None:
    """The task's wide record, or None when the task has no wide run. A record that names another task than its
    file's is refused: it was copied, and its stable set is another repository's."""
    path = Path(root) / f"{task_id}.json" if root else None
    if path is None or not path.is_file():
        return None
    record = json.loads(path.read_text())
    if record.get("format") not in FORMATS or not record.get("command") or not record.get("limit_seconds"):
        raise ValueError(f"{path}: a wide record needs a command, a limit and a format of {FORMATS}")
    if "task_id" in record and record["task_id"] != task_id:
        raise ValueError(
            f"{path}: the record was measured for {record['task_id']}, not {task_id}; measure this task with "
            "`context-eval wide-check`"
        )
    for key in ("test_side", "source", "settings"):
        patterns = record.get(key, [])
        if not isinstance(patterns, list) or not all(isinstance(pattern, str) and pattern for pattern in patterns):
            raise ValueError(f"{path}: {key} must be a list of path patterns")
    return record


def unusable(record: dict, image_id: str | None, year: int | None = None) -> str | None:
    """Why a grading must not run this record, or None. A record is used only as `check` left it: measured for a
    task, by the rule (`GOLD_RUNS` serial gold runs, then `LOAD_RUNS` under grading load `LOAD_WORKERS` at a
    time), with a stable set, on the image being graded, with no problem listed, and in the calendar year it was
    checked in, since a test name built from the date (`[2026-True]`) is in the stable set only until the date moves
    on and would then read as a test that stopped passing."""
    if "stable" not in record or not record.get("image_id") or not record.get("task_id"):
        return "it was never measured"
    load = record.get("under_load") or {}
    if not load.get("runs"):
        return "its stable set was not measured under grading load"
    for measured, rule, what in (
        (record.get("gold_runs"), GOLD_RUNS, "serial gold runs"),
        (load.get("runs"), LOAD_RUNS, "gold gradings under load"),
        (load.get("workers"), LOAD_WORKERS, "gradings at a time under load"),
    ):
        if not isinstance(measured, int) or measured < rule:
            return f"it was measured with {measured} {what}, fewer than the rule's {rule}"
    if record.get("problems"):
        return f"its check listed problems that are not settled: {record['problems'][:3]}"
    if not record["stable"]:
        return "its stable set is empty"
    if record["image_id"] != image_id:
        return f"it was measured on image {record['image_id']}, not {image_id}"
    year = year or datetime.now(UTC).year
    if str(record.get("checked_on", ""))[:4] != str(year):
        return f"it was checked on {record.get('checked_on') or 'an unrecorded date'}, not in {year}"
    return None


def digest(record: dict) -> str:
    """sha256 of what a wide record measures with (`PINNED`). A grade records it and a count's plan pins it, so a
    record changed after the fact shows. Path rules a record does not set count as empty."""
    kept = {key: record.get(key) or ([] if key in ("test_side", "source", "settings") else None) for key in PINNED}
    return hashlib.sha256(json.dumps(kept, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def snippet(record: dict) -> str:
    """The shell that runs the wide command in the verifier container. The exit file is written only when the
    command returned, so a container stopped from outside leaves no marker."""
    limit = int(record["limit_seconds"])
    run = f"timeout -k 10 {limit} bash -c {shlex.quote(record['command'])}"
    took = "echo $(( $(date +%s) - wide_start )) > /logs/wide/seconds"
    return (
        f"mkdir -p /logs/wide; wide_start=$(date +%s); {run} > /logs/wide/stdout.txt 2> /logs/wide/stderr.txt; "
        f"echo $? > /logs/wide/exit; {took}; "
    )


RANK = {"pass": 0, "skip": 1, "fail": 2}


def go_json(lines: Iterable[str]) -> dict[str, str]:
    """`package.Test` of every top-level test of `go test -json` output; the worst status of a repeated name wins,
    as in a JUnit report. Each package that reported a verdict also gets a `package (package)` name for what its
    tests do not show: it fails when the package failed although none of its top-level tests did (a `TestMain` exit
    code, a build that failed, a panic outside a test) and passes otherwise, so a package that fails only because
    one of its tests failed adds no second failure."""
    results: dict[str, str] = {}
    verdicts: dict[str, str] = {}
    failed: set[str] = set()
    for line in lines:
        try:
            event = json.loads(line)
            action, package, test = event.get("Action"), event.get("Package", ""), event.get("Test")
        except (ValueError, AttributeError):
            continue
        if action not in RANK:
            continue
        if not test:
            verdicts[package] = max(verdicts.get(package, "pass"), action, key=RANK.__getitem__)
        elif "/" not in test:
            name = f"{package}.{test}"
            results[name] = max(results.get(name, "pass"), action, key=RANK.__getitem__)
            if action == "fail":
                failed.add(package)
    for package, verdict in verdicts.items():
        results[f"{package} (package)"] = "pass" if verdict == "fail" and package in failed else verdict
    return results


def junit(text: str) -> dict[str, str]:
    """`classname::name` of every test case of a JUnit XML report; the worst status of a repeated name wins."""
    results: dict[str, str] = {}
    for case in ElementTree.fromstring(text).iter("testcase"):
        status = "pass"
        if case.find("failure") is not None or case.find("error") is not None:
            status = "fail"
        elif case.find("skipped") is not None:
            status = "skip"
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        results[name] = max(results.get(name, "pass"), status, key=RANK.__getitem__)
    return results


def collect(raw: Path, destination: Path, record: dict) -> None:
    """Turn the container's /logs/wide into `destination/results.json`: whether the run finished, and each test's
    status. A command that returned by itself finished, whatever it reported: when it left no readable report (a
    root `conftest.py` that does not import, packages that do not build), no test ran, the result holds no test and
    the reason says so. Only a command cut off by its limit, or one that never wrote its exit code, did not finish.
    Any other error while reading leaves the run unmeasured and stays inside the wide result: the official grade
    was written before this and must not be lost to it. The raw output is not kept; a Go run's is megabytes per
    grading, so it is read line by line."""
    result = {"complete": False, "exit": None, "seconds": None, "reason": None, "tests": {}}
    try:
        code = int((raw / "exit").read_text().strip())
        result["exit"] = code
        if (raw / "seconds").is_file():
            result["seconds"] = int((raw / "seconds").read_text().strip())
        if code in KILLED:
            limit = record["limit_seconds"]
            result["reason"] = f"the wide command was killed (exit {code}): it exceeded {limit} s or the memory limit"
        else:
            try:
                if record["format"] == "go-json":
                    with (raw / "stdout.txt").open(errors="replace") as lines:
                        result["tests"] = go_json(lines)
                else:
                    result["tests"] = junit((raw / "junit.xml").read_text(errors="replace"))
            except (FileNotFoundError, ElementTree.ParseError) as error:
                # The run left no report, or half of one: that is how the run ended, not a reading failure.
                result["reason"] = f"{type(error).__name__}: {error}"
            result["complete"] = True
    except Exception as error:
        result |= {"complete": False, "tests": {}, "reason": f"{type(error).__name__}: {error}"}
    if result["complete"] and not result["tests"]:
        why = f" ({result['reason']})" if result["reason"] else ""
        result["reason"] = f"the wide command returned {result['exit']} and reported no test{why}"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "results.json").write_text(json.dumps(result, indent=0, sort_keys=True) + "\n")


def read(grade_folder: Path) -> dict[str, str] | None:
    """Each test's status in the wide run of one grading (`grade/verifier` or `grade/restored`); None when the run
    is missing, cannot be parsed or did not finish. A result of another shape was not written by `collect` and raises
    ValueError with the file's path."""
    path = Path(grade_folder) / "wide" / "results.json"
    try:
        result = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(result, dict):
        raise ValueError(f"{path}: not a wide result (a JSON object whose tests map names to statuses)")
    if not result.get("complete"):
        return None
    tests = result.get("tests")
    if not isinstance(tests, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in tests.items()):
        raise ValueError(f"{path}: not a wide result (a JSON object whose tests map names to statuses)")
    return tests


def not_passing(tests: dict[str, str], record: dict) -> set[str]:
    """The stable tests a finished wide run did not pass. A stable test missing from the run is one of them: its
    file no longer imports, its package no longer builds, or it was removed, and it passed under the reference
    solution every time. The one reading the check and every count share."""
    return {name for name in record.get("stable", []) if tests.get(name) != "pass"}


def harm_wide_names(record: dict) -> set[str]:
    return {name for names in (record.get("harm_names") or {}).values() for name in names}


def dated(name: str, year: int) -> bool:
    """Whether a test name holds the year it was checked in or a neighbouring one as a number of its own."""
    return any(re.search(rf"(?<!\d){value}(?!\d)", name) for value in (year - 1, year, year + 1))


def check(
    evaluator,
    task,
    image_id: str,
    out: Path,
    record: dict,
    gold: Path,
    *,
    repeats: int,
    harmful: Path | None = None,
    harm_names: dict[str, list[str]] | None = None,
    harm_ids: Iterable[str] = (),
    load_runs: int = LOAD_RUNS,
    load_workers: int = LOAD_WORKERS,
) -> dict:
    """Measure a task's wide run before the task is used: grade the gold patch `repeats` times and, when given, the
    known harmful patch, each with the wide command. Returns the finished record and `problems`, which must be empty
    before the record is relied on: a gold run that was not graded with reward 1 (the wide command must leave the
    official result as it is), a wide run that did not finish, a harm test of the task (`harm_ids`) without a wide
    name, a harm test whose wide name is not in the stable set, a harmful patch that fails none of the harm tests
    (a harm is a set that fires when any of its tests fails, so one example change need not fail them all; the ones
    it fails are recorded as `harmful_fails_harm`), and stable tests outside the harm names that the harmful patch
    fails (a decided harm set is never widened without the user). `always_fail` names the tests that failed in every
    gold run: they never enter the filter, and a long list means the wide command lacks something the suite needs.
    `dated` names the tests that passed every time but hold the year of the check in their name: they are left out
    of the stable set, since a name built from the date does not exist on another day.

    The repeats run one after another, and a campaign is graded several sessions at a time; a test that depends on
    timing can pass every quiet run and fail under that load. So the gold patch is graded `load_runs` more times,
    `load_workers` at a time, and the stable set keeps only the tests that passed in those too: a test that fails
    under the reference solution cannot show that a session broke anything. `under_load` records how often each
    test left out this way did not pass. A harm test among them, a gold grading under load that is not a passing
    grade, and a wide run under load that did not finish or reported no test are problems; a run that reported no
    test takes no test out of the stable set. The rule's numbers are `GOLD_RUNS`, `LOAD_RUNS` and `LOAD_WORKERS`;
    a record measured with fewer is written but `unusable`."""
    from sereno.context_eval.contracts import Submission

    harm_names = harm_names or {}
    problems, runs = [], []
    today = datetime.now(UTC).date()
    for index in range(1, repeats + 1):
        grade = evaluator.evaluate(Submission(task.id, image_id, task.base_commit, gold), out / f"gold-{index}")
        if grade.get("status") != "graded" or grade.get("reward") != 1:
            outcome = grade.get("error") or f"{grade.get('status')}, reward {grade.get('reward')}"
            problems.append(f"gold run {index}: the official run is not a passing grade ({outcome})")
        tests = read(out / f"gold-{index}" / "grade" / "verifier")
        if not tests:
            problems.append(f"gold run {index}: the wide run did not finish or reported no test")
        else:
            runs.append(tests)
    seen = sorted(set().union(*runs)) if runs else []
    passing = sorted(name for name in seen if all(run.get(name) == "pass" for run in runs)) if runs else []
    stable = [name for name in passing if not dated(name, today.year)]
    mapped = sorted({name for names in harm_names.values() for name in names})
    for name in mapped:
        if name not in stable:
            problems.append(f"harm test {name} is not in the stable set")
    if unmapped := sorted(set(harm_ids) - {harm_id for harm_id, names in harm_names.items() if names}):
        problems.append(f"harm tests of the task without a wide name: {unmapped}")
    record = record | {
        "task_id": task.id,
        "image_id": image_id,
        "checked_on": today.isoformat(),
        "gold_runs": repeats,
        "seen": len(seen),
        "stable": stable,
        "dated": sorted(set(passing) - set(stable)),
        "unstable": sorted(name for name in seen if len({run.get(name) for run in runs}) > 1),
        "always_fail": sorted(name for name in seen if all(run.get(name) == "fail" for run in runs)),
        "harm_names": harm_names,
        "harmful_fails_harm": None,
        "harmful_fails_outside_harm": None,
    }
    if load_runs:

        def under_load(index: int):
            folder = out / f"load-{index}"
            grade = evaluator.evaluate(Submission(task.id, image_id, task.base_commit, gold), folder)
            return grade.get("status") == "graded" and grade.get("reward") == 1, read(folder / "grade" / "verifier")

        with ThreadPoolExecutor(max_workers=load_workers) as pool:
            loaded = list(pool.map(under_load, range(1, load_runs + 1)))
        # A wide run that reported no test is a problem, as in the serial runs, and removes no test from the set.
        failed = Counter(name for _, tests in loaded if tests for name in not_passing(tests, record))
        record["under_load"] = {
            "runs": load_runs,
            "workers": load_workers,
            "not_passing_grades": sum(not passed for passed, _ in loaded),
            "unfinished": sum(tests is None for _, tests in loaded),
            "no_test": sum(tests == {} for _, tests in loaded),
            "runs_with_a_stable_test_not_passing": sum(
                bool(tests and not_passing(tests, record)) for _, tests in loaded
            ),
            "not_passing": dict(sorted(failed.items())),
        }
        record["stable"] = stable = [name for name in stable if name not in failed]
        if flaky := sorted(set(failed) & set(mapped)):
            problems.append(f"harm tests that did not pass in every gold run under load: {flaky}")
        for key, what in (
            ("not_passing_grades", "were not a passing grade"),
            ("unfinished", "did not finish their wide run"),
            ("no_test", "reported no test in their wide run"),
        ):
            if record["under_load"][key]:
                problems.append(f"{record['under_load'][key]} of {load_runs} gold gradings under load {what}")
    if harmful is not None:
        evaluator.evaluate(Submission(task.id, image_id, task.base_commit, harmful), out / "harmful")
        tests = read(out / "harmful" / "grade" / "verifier")
        if tests is None:
            problems.append("harmful run: the wide run did not finish")
        else:
            failed = not_passing(tests, record)
            record["harmful_fails_harm"] = sorted(failed & set(mapped))
            if mapped and not failed & set(mapped):
                problems.append("the harmful patch fails none of the harm tests")
            extra = sorted(failed - set(mapped))
            record["harmful_fails_outside_harm"] = extra
            if extra:
                problems.append(
                    f"the harmful patch fails {len(extra)} stable test(s) outside the harm names: {extra[:5]}"
                )
    return record | {"problems": problems}
