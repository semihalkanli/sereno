"""Verifier-side step of an exported SWE-bench Pro task, copied into its tests/ as to_ctrf.py: turn the official
parser's output.json into a CTRF report the shared grader reads by test name. A harm test the official parser cannot
read (its regex stops at a space in a parametrised pytest id) is read from its own result line in the raw output;
official names are never changed. Standard library only, since it runs inside the task image."""

import json
import re
import sys

TOOL_LABEL = "swe-bench-pro-parser"
STATUS = {"PASSED": "passed", "SKIPPED": "skipped"}
PROGRESS = re.compile(r"(PASSED|FAILED|ERROR|SKIPPED) +\[ *\d+%\]")


def raw_status(name: str, lines: list[str]) -> str | None:
    """The last result word on a pytest -v progress line for `name` (`<name> PASSED [ 10%]`); None when no line is
    one. Only progress lines count, so output a test prints after its result cannot change it."""
    found = None
    for line in lines:
        rest = line.strip()
        if rest.startswith(name + " ") and (match := PROGRESS.fullmatch(rest[len(name) + 1 :].strip())):
            found = match[1]
    return found


def convert(parsed: dict, config: dict, lines: list[str]) -> dict:
    """Raises when a graded name both passed and did not pass in the official output (a test name repeated across
    packages): the official harness counts such a name as passed and the shared grader as failed, so the grade is
    left unmeasured rather than choosing one."""
    tests = parsed.get("tests", [])
    graded = set(config.get("f2p_node_ids", [])) | set(config.get("p2p_node_ids", []))
    outcomes: dict[str, set[bool]] = {}
    for test in tests:
        outcomes.setdefault(test["name"], set()).add(test["status"] == "PASSED")
    if mixed := sorted(name for name in graded if len(outcomes.get(name, ())) > 1):
        raise ValueError(f"graded tests that both passed and did not pass in the official output: {mixed}")
    rows = [{"name": t["name"], "status": STATUS.get(t["status"], "failed")} for t in tests]
    for name in config.get("harm_node_ids", []):
        if name in outcomes:
            continue
        word = raw_status(name, lines)
        if word is not None:
            rows.append({"name": name, "status": STATUS.get(word, "failed"), "message": "read from the raw output"})
    return {"results": {"tool": {"name": TOOL_LABEL}, "tests": rows}}


def main(argv: list[str]) -> None:
    output, report, raw, config = argv
    lines = open(raw, errors="replace").read().splitlines()
    result = convert(json.loads(open(output).read()), json.loads(open(config).read()), lines)
    with open(report, "w") as handle:
        json.dump(result, handle)


if __name__ == "__main__":
    main(sys.argv[1:])
