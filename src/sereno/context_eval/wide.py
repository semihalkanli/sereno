"""The wide run: the repository's whole test suite, run inside a grading after the official tests, so that a count
can tell "the harm test failed" from "the code is broken in general" on more than the tests the task grades.

A task has a wide run when `<wide root>/<task id>.json` exists. The record is written by `check` before the task is
used and holds the command, its output format, its time limit, the task image it was measured on, the tests that
passed in every gold run (the stable set) and the wide names of the task's harm tests. The evaluator appends the
command to the verifier container after `tests/test.sh`, so it runs on the graded tree, in the official run and in
the restored run alike, and it can never change the grade: its output goes to its own folder, never through the
task's parser, it has its own time limit, and a run cut short leaves the written reward standing.

A wide run that did not finish is unmeasured, not a failure: `read` returns None for it, and a count must leave such
a session out rather than read its missing tests as broken."""

import json
import shlex
import xml.etree.ElementTree as ElementTree
from pathlib import Path

FORMATS = ("go-json", "junit")
KILLED = (124, 137)  # timeout(1) ending the command, or the kill that follows


def load(root: Path | None, task_id: str) -> dict | None:
    """The task's wide record, or None when the task has no wide run."""
    path = Path(root) / f"{task_id}.json" if root else None
    if path is None or not path.is_file():
        return None
    record = json.loads(path.read_text())
    if record.get("format") not in FORMATS or not record.get("command") or not record.get("limit_seconds"):
        raise ValueError(f"{path}: a wide record needs a command, a limit and a format of {FORMATS}")
    return record


def snippet(record: dict) -> str:
    """The shell that runs the wide command in the verifier container. The exit file is written only when the
    command returned, so a container stopped from outside leaves no marker."""
    limit = int(record["limit_seconds"])
    run = f"timeout -k 10 {limit} bash -c {shlex.quote(record['command'])}"
    return f"mkdir -p /logs/wide; {run} > /logs/wide/stdout.txt 2> /logs/wide/stderr.txt; echo $? > /logs/wide/exit; "


def go_json(text: str) -> dict[str, str]:
    """`package.Test` of every top-level test of `go test -json` output."""
    results = {}
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        test = event.get("Test")
        if event.get("Action") in ("pass", "fail", "skip") and test and "/" not in test:
            results[f"{event['Package']}.{test}"] = event["Action"]
    return results


def junit(text: str) -> dict[str, str]:
    """`classname::name` of every test case of a JUnit XML report; the worst status of a repeated name wins."""
    rank = {"pass": 0, "skip": 1, "fail": 2}
    results: dict[str, str] = {}
    for case in ElementTree.fromstring(text).iter("testcase"):
        status = "pass"
        if case.find("failure") is not None or case.find("error") is not None:
            status = "fail"
        elif case.find("skipped") is not None:
            status = "skip"
        name = f"{case.get('classname', '')}::{case.get('name', '')}"
        results[name] = max(results.get(name, "pass"), status, key=rank.__getitem__)
    return results


def collect(raw: Path, destination: Path, record: dict) -> None:
    """Turn the container's /logs/wide into `destination/results.json`: whether the run finished, and each test's
    status. The raw output is not kept; a Go run's is megabytes per grading."""
    result = {"complete": False, "exit": None, "reason": None, "tests": {}}
    try:
        code = int((raw / "exit").read_text().strip())
        result["exit"] = code
        if code in KILLED:
            result["reason"] = f"the wide command exceeded {record['limit_seconds']} s"
        elif record["format"] == "go-json":
            result |= {"complete": True, "tests": go_json((raw / "stdout.txt").read_text(errors="replace"))}
        else:
            result |= {"complete": True, "tests": junit((raw / "junit.xml").read_text(errors="replace"))}
    except (OSError, ValueError, ElementTree.ParseError) as error:
        result["reason"] = f"{type(error).__name__}: {error}"
    if result["complete"] and not result["tests"]:
        result |= {"complete": False, "reason": "the wide command reported no test"}
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
    gold or does not fail under the harmful patch, and stable tests outside the harm names that the harmful patch
    fails (a decided harm set is never widened without the user)."""
    from sereno.context_eval.contracts import Submission

    harm_names = harm_names or {}
    problems, runs = [], []
    for index in range(1, repeats + 1):
        evaluator.evaluate(Submission(task.id, image_id, task.base_commit, gold), out / f"gold-{index}")
        tests = read(out / f"gold-{index}" / "grade" / "verifier")
        if tests is None:
            problems.append(f"gold run {index}: the wide run did not finish")
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
        "harm_names": harm_names,
        "harmful_fails_outside_harm": None,
    }
    if harmful is not None:
        evaluator.evaluate(Submission(task.id, image_id, task.base_commit, harmful), out / "harmful")
        tests = read(out / "harmful" / "grade" / "verifier")
        if tests is None:
            problems.append("harmful run: the wide run did not finish")
        else:
            failed = {name for name in stable if tests.get(name) != "pass"}
            for name in mapped:
                if name in stable and name not in failed:
                    problems.append(f"harm test {name} does not fail under the harmful patch")
            extra = sorted(failed - set(mapped))
            record["harmful_fails_outside_harm"] = extra
            if extra:
                problems.append(
                    f"the harmful patch fails {len(extra)} stable test(s) outside the harm names: {extra[:5]}"
                )
    return record | {"problems": problems}
