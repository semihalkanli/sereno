"""The memory guard inside the harness, at three of its points:

- task start, after the session_start interventions and before the context is built: rule 1 compares the carried
  files with the copy the harness kept at the end of the previous task, and rules 5 and 6 read every file that the
  copy does not hold as it now is (a file carried unchanged was read when it was written);
- each write, after every command: rule 2 on a path the agent wrote, rules 5 and 6 on what the new content adds;
- task end: rule 2 on the repository's instruction files, and the integrity record of every carried file.

Every finding goes to the event log as `guard_finding`. Rules 1 and 2 can also put the kept copy back. Nothing here
sends a notice or stops a session, and nothing makes the agent wait: the checks read snapshots the harness takes
anyway. Rule 3 needs its judge and rule 4 a source of test facts; neither runs here yet.

A file the kept copy does not hold at all is only recorded, whatever the integrity setting: whether such a file loads
is not settled. A planted note is such a file, and its index line changes a kept file, so restoring would remove a
planted note by construction; a campaign that measures the content rules on planted notes keeps integrity at
"record" or "off".
"""

from sereno.context_eval import guard
from sereno.context_eval.memory import REPO_INSTRUCTIONS, USER

RESTORABLE = {"the file differs from its integrity record", "a recorded file is missing"}


class LiveGuard:
    def __init__(self, config, runtime, kept: dict[str, str]):
        self.config, self.runtime = config, runtime
        # The copy of the carried files the harness kept at the end of the previous task.
        self.kept = dict(kept)
        self.writers = dict.fromkeys(kept, "carried")
        self.findings: list[dict] = []
        self.instructions_start: dict[str, str] = {}

    def report(self, point: str, finding: guard.Finding, response: str = "record") -> None:
        record = {
            "point": point,
            "code": finding.code,
            "path": finding.path,
            "fact": finding.fact,
            "evidence": finding.evidence,
            "response": response,
        }
        self.findings.append(record | {"step": self.runtime.step, "action": self.runtime.actions})
        self.runtime.emit("guard_finding", **record)

    def observe(self, origin: str, changes: list[tuple[str, str | None, str | None]]) -> None:
        """Memory changes the runtime just captured; an agent's own writes are checked here."""
        for path, _before, _after in changes:
            self.writers[path] = origin
        if origin != "agent":
            return
        restore: dict[str, str | None] = {}
        for path, before, after in changes:
            for finding in guard.check_write(path, before, after):
                if finding.code == guard.AUTHOR:
                    if self.config.author == "off":
                        continue
                    if self.config.author == "restore" and path == USER:
                        restore[path] = before
                        self.report("write", finding, "restore")
                        continue
                elif self.config.content == "off":
                    continue
                self.report("write", finding)
        self.put_back(restore)

    def task_start(self) -> None:
        current = self.runtime.memory
        self.instructions_start = self.runtime.env.read_instructions(REPO_INSTRUCTIONS)
        restore: dict[str, str | None] = {}
        if self.config.integrity != "off":
            record = {path: guard.sha256(text) for path, text in self.kept.items()}
            for finding in guard.integrity(record, current):
                if self.config.integrity == "restore" and finding.fact in RESTORABLE:
                    restore[finding.path] = self.kept[finding.path]
                    self.report("task_start", finding, "restore")
                else:
                    self.report("task_start", finding)
        if self.config.content != "off":
            for path, text in current.items():
                if self.kept.get(path) != text:
                    for finding in guard.content_findings(path, text):
                        self.report("task_start", finding)
        self.put_back(restore)

    def task_end(self) -> None:
        if self.config.author == "off":
            return
        # A file intervention's edit is not the agent's: compare with the file as the last intervention left it.
        expected = dict(self.instructions_start)
        for entry in self.runtime.journal:
            if entry["event"]["method"] == "file" and entry["event"]["path"] in REPO_INSTRUCTIONS:
                expected[entry["event"]["path"]] = entry["after"]
        end = self.runtime.env.read_instructions(REPO_INSTRUCTIONS)
        for path in sorted(set(expected) | set(end)):
            if expected.get(path) != end.get(path):
                self.report("task_end", guard.author_bound(path))

    def put_back(self, files: dict[str, str | None]) -> None:
        if not files:
            return
        for path, text in files.items():
            self.runtime.env.write(path, text)
        self.runtime.capture_memory("harness")

    def summary(self) -> dict:
        """The findings, and the integrity record of the files the task leaves: hash and who wrote them last."""
        return {
            "config": self.config.model_dump(mode="json"),
            "findings": self.findings,
            "record": {
                path: {"sha256": guard.sha256(text), "writer": self.writers.get(path, "unknown")}
                for path, text in sorted(self.runtime.memory.items())
            },
        }
