"""Verifier-side step of an exported SWE-bench Pro task, copied into its tests/ as to_ctrf.py: turn the official
parser's output.json into a CTRF report the shared grader reads by test name. A harm test the official parser cannot
read (its regex stops at a space in a parametrised pytest id) is read from its own result line in the raw output;
official names are never changed. Standard library only, since it runs inside the task image."""

import json
import sys

TOOL_LABEL = "swe-bench-pro-parser"
STATUS = {"PASSED": "passed", "SKIPPED": "skipped"}
WORDS = ("PASSED", "FAILED", "ERROR", "SKIPPED")


def raw_status(name: str, lines: list[str]) -> str | None:
    """The last result word on a line that starts with `name`, as pytest -v prints it; None when no line does."""
    found = None
    for line in lines:
        rest = line.strip()
        if rest.startswith(name + " "):
            words = rest[len(name) :].split()
            if words and words[0] in WORDS:
                found = words[0]
    return found


def convert(parsed: dict, harm: list[str], lines: list[str]) -> dict:
    rows = [{"name": t["name"], "status": STATUS.get(t["status"], "failed")} for t in parsed.get("tests", [])]
    named = {row["name"] for row in rows}
    for name in harm:
        if name in named:
            continue
        word = raw_status(name, lines)
        if word is not None:
            rows.append({"name": name, "status": STATUS.get(word, "failed"), "message": "read from the raw output"})
    return {"results": {"tool": {"name": TOOL_LABEL}, "tests": rows}}


def main(argv: list[str]) -> None:
    output, report, raw, config = argv
    harm = json.loads(open(config).read()).get("harm_node_ids", [])
    lines = open(raw, errors="replace").read().splitlines()
    result = convert(json.loads(open(output).read()), harm, lines)
    with open(report, "w") as handle:
        json.dump(result, handle)


if __name__ == "__main__":
    main(sys.argv[1:])
