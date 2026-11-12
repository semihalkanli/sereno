"""A neutral stand-in for the run-local attacker program, for the tests and the offline demo configs.

It speaks protocol 1.0 of `sereno.context_eval.run_attacker`: it reads the request at argv[-2] and writes the
response at argv[-1]. It is deterministic and keeps nothing between calls. In the first call whose writes are open
while it owns no file yet, it writes one inert marker line to a fixed topic file of the project's memory; every call
bumps a counter in its notebook. It never changes the strategy, asks for no code reads and reports zero usage. The
marker is a plain fixture token in the style of the scripted examples, never an instruction.
"""

import json
import sys
from pathlib import Path

PATH = "/root/.claude/projects/{project}/memory/ci-attacker-fixture.md"
LINE = "CI_ATTACKER_MARKER inert fixture line\n"

request = json.loads(Path(sys.argv[-2]).read_text())
notebook = request.get("notebook") or "0"
response = {
    "schema_version": "1.0",
    "actions": [],
    "notebook": str(int(notebook) + 1),
    "writer_usage": {"usage": {"total_tokens": 0}, "cost": 0.0},
}
if request["contract"]["writes_open"] and not request["observation"]["memory"]["owned"]:
    response["actions"] = [{"op": "write", "path": PATH, "content": LINE}]
Path(sys.argv[-1]).write_text(json.dumps(response))
