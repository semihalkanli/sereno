"""A neutral stand-in for the adaptive program, for the tests and the example config.

It reads the request, looks up what to do in the JSON plan of the ADAPTIVE_STUB environment variable, keyed by the
case and the index of the session the step precedes (`<case>/<index>`), then by the index alone (`*` for any other),
and answers with canned actions. The plan holds placeholders only. Every call is logged to calls.log in the program's
state folder, so a test can count the calls.

A step is {"mode": ..., ...}: `answer` (the default; `actions` and an optional `note`), `crash` (exit with `code`),
`sleep` (for `seconds`, then answer), `raw` (write `text` as the response), `none` (write no response). A step with
`wait_for`, a glob below the campaign directory, first waits up to 30 seconds for a match.
"""

import json
import os
import sys
import time
from pathlib import Path

request_path, response_path = Path(sys.argv[-2]), Path(sys.argv[-1])
request = json.loads(request_path.read_text())
plan = json.loads(os.environ.get("ADAPTIVE_STUB", "{}"))
index = request["next_session"]["index"]
step = plan.get(f"{request['case']}/{index}", plan.get(str(index), plan.get("*", {})))
finished = request["finished_session"]
with (Path(request["state_dir"]) / "calls.log").open("a") as log:
    log.write(f"{index} {finished['grade_status'] if finished else '-'} {len(request['memory']['files'])}\n")
if "wait_for" in step:
    deadline = time.monotonic() + 30
    while not any(Path(request["campaign_dir"]).glob(step["wait_for"])) and time.monotonic() < deadline:
        time.sleep(0.05)
mode = step.get("mode", "answer")
if mode == "crash":
    sys.exit(step.get("code", 3))
if mode == "sleep":
    time.sleep(step["seconds"])
if mode == "raw":
    response_path.write_text(step["text"])
elif mode != "none":
    response = {"schema_version": "1.0", "actions": step.get("actions", [])}
    if "note" in step:
        response["note"] = step["note"]
    response_path.write_text(json.dumps(response))
