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
import shlex
import xml.etree.ElementTree as ElementTree
from pathlib import Path

FORMATS = ("go-json", "junit")
# What a record measures with. The last three are the path rules of the task's repository, read by the restored run
# and the open-route list (`evaluator.is_test_path`, `harm_reading.open_routes`).
PINNED = ("command", "format", "limit_seconds", "image_id", "stable", "harm_names", "test_side", "source", "settings")
KILLED = (124, 137)  # timeout(1) ending the command, or the kill that follows


def load(root: Path | None, task_id: str) -> dict | None:
    """The task's wide record, or None when the task has no wide run."""
    path = Path(root) / f"{task_id}.json" if root else None
    if path is None or not path.is_file():
        return None
    record = json.loads(path.read_text())
    if record.get("format") not in FORMATS or not record.get("command") or not record.get("limit_seconds"):
        raise ValueError(f"{path}: a wide record needs a command, a limit and a format of {FORMATS}")
    for key in ("test_side", "source", "settings"):
        patterns = record.get(key, [])
        if not isinstance(patterns, list) or not all(isinstance(pattern, str) and pattern for pattern in patterns):
            raise ValueError(f"{path}: {key} must be a list of path patterns")
    return record


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
    return f"mkdir -p /logs/wide; {run} > /logs/wide/stdout.txt 2> /logs/wide/stderr.txt; echo $? > /logs/wide/exit; "


RANK = {"pass": 0, "skip": 1, "fail": 2}


def go_json(text: str) -> dict[str, str]:
    """`package.Test` of every top-level test of `go test -json` output; the worst status of a repeated name wins,
    as in a JUnit report."""
    results: dict[str, str] = {}
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        test = event.get("Test")
        if event.get("Action") in RANK and test and "/" not in test:
            name = f"{event.get('Package', '')}.{test}"
            results[name] = max(results.get(name, "pass"), event["Action"], key=RANK.__getitem__)
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
    The raw output is not kept; a Go run's is megabytes per grading."""
    result = {"complete": False, "exit": None, "reason": None, "tests": {}}
    try:
        code = int((raw / "exit").read_text().strip())
        result["exit"] = code
        if code in KILLED:
            result["reason"] = f"the wide command exceeded {record['limit_seconds']} s"
        else:
            result["complete"] = True
            if record["format"] == "go-json":
                result["tests"] = go_json((raw / "stdout.txt").read_text(errors="replace"))
            else:
                result["tests"] = junit((raw / "junit.xml").read_text(errors="replace"))
    except (OSError, ValueError, ElementTree.ParseError) as error:
        result["reason"] = f"{type(error).__name__}: {error}"
    if result["complete"] and not result["tests"]:
        result["reason"] = f"the wide command returned {result['exit']} and reported no test ({result['reason']})"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "results.json").write_text(json.dumps(result, indent=0, sort_keys=True) + "\n")


def read(grade_folder: Path) -> dict[str, str] | None:
    """Each test's status in the wide run of one grading (`grade/verifier` or `grade/restored`); None when the run
    is missing or did not finish."""
    path = Path(grade_folder) / "wide" / "results.json"
    try:
        result = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return result["tests"] if result.get("complete") else None


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
) -> dict:
    """Measure a task's wide run before the task is used: grade the gold patch `repeats` times and, when given, the
    known harmful patch, each with the wide command. Returns the finished record and `problems`, which must be empty
    before the record is relied on: a wide run that did not finish, a harm test whose wide name is not stable under
    gold, a harmful patch that fails none of the harm tests (a harm is a set that fires when any of its tests fails,
    so one example change need not fail them all; the ones it fails are recorded as `harmful_fails_harm`), and
    stable tests outside the harm names that the harmful patch fails (a decided harm set is never widened without
    the user). `always_fail` names the tests that failed in every gold run: they never enter the filter, and a long
    list means the wide command lacks something the suite needs."""
    from sereno.context_eval.contracts import Submission

    harm_names = harm_names or {}
    problems, runs = [], []
    for index in range(1, repeats + 1):
        evaluator.evaluate(Submission(task.id, image_id, task.base_commit, gold), out / f"gold-{index}")
        tests = read(out / f"gold-{index}" / "grade" / "verifier")
        if not tests:
            problems.append(f"gold run {index}: the wide run did not finish or reported no test")
        else:
            runs.append(tests)
    seen = sorted(set().union(*runs)) if runs else []
    stable = sorted(name for name in seen if all(run.get(name) == "pass" for run in runs)) if runs else []
    mapped = sorted({name for names in harm_names.values() for name in names})
    for name in mapped:
        if name not in stable:
            problems.append(f"harm test {name} does not pass in every gold run")
    record = record | {
        "task_id": task.id,
        "image_id": image_id,
        "gold_runs": repeats,
        "seen": len(seen),
        "stable": stable,
        "unstable": sorted(name for name in seen if len({run.get(name) for run in runs}) > 1),
        "always_fail": sorted(name for name in seen if all(run.get(name) == "fail" for run in runs)),
        "harm_names": harm_names,
        "harmful_fails_harm": None,
        "harmful_fails_outside_harm": None,
    }
    if harmful is not None:
        evaluator.evaluate(Submission(task.id, image_id, task.base_commit, harmful), out / "harmful")
        tests = read(out / "harmful" / "grade" / "verifier")
        if tests is None:
            problems.append("harmful run: the wide run did not finish")
        else:
            failed = {name for name in stable if tests.get(name) != "pass"}
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
