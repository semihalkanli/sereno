"""The memory guard inside the harness, at three of its points:

- task start, after the session_start interventions and before the context is built: rule 1 compares the carried
  files with the copy the harness kept at the end of the previous task; the content rules read every file that the
  copy does not hold as it now is (a file carried unchanged was read when it was written), or every file when the
  session starts from the configured memory;
- each write, after every command: rule 2 on a path the agent wrote, the content rules on what the new content adds;
- task end: rule 2 on the repository's instruction files, and the integrity record of every carried file.

Rules 5 and 6 are code and answer at once. Rules 3 and 4 are read by a judge model (guard_judge) on worker threads:
the agent never waits for them, their answers are taken before each model call, and at task end the harness waits
at most `judge_wait_seconds` before the notes still out count as unchecked.

Every finding goes to the event log as `guard_finding`, every judged item as `guard_judged`, and an item another Jev
build answered, which stays unchecked, as `guard_unchecked`, so a run can see it and stop. Rules 1 and 2 can also
put the kept copy back. Nothing here stops a session. With `notify` set to "send", a rule 3 or 4 finding also reaches
the agent as a notice (guard_notice) at the first model call after the judge's answer is in: the answers are taken
before the notices are, so a call never starts without a notice that is ready, and the agent never waits for one. A
notice goes once per rule and note; a later finding for the same pair is counted, not sent.

A file the kept copy does not hold at all is only recorded, whatever the integrity setting: whether such a file loads
is not settled. A planted note is such a file, and its index line changes a kept file, so restoring would remove a
planted note by construction; a campaign that measures the content rules on planted notes keeps integrity at
"record" or "off".
"""

import collections
import concurrent.futures
import re
import time

from sereno.context_eval import guard, guard_judge, guard_notice
from sereno.context_eval.memory import REPO, REPO_INSTRUCTIONS, USER, memory_dir

RESTORABLE = {"the file differs from its integrity record", "a recorded file is missing"}
JUDGE_WORKERS = 4


def make_judge():
    return guard_judge.Jev()


class LiveGuard:
    def __init__(self, config, runtime, kept: dict[str, str]):
        self.config, self.runtime = config, runtime
        # The copy of the carried files the harness kept at the end of the previous task.
        self.kept = dict(kept)
        self.writers = dict.fromkeys(kept, "carried")
        self.findings: list[dict] = []
        self.instructions_start: dict[str, str] = {}
        self.user_words = ""
        self.facts: dict[str, bool] | None = None
        self.judge = self.pool = None
        self.jobs: list[dict] = []
        self.judged: list[dict] = []
        self.unchecked: list[dict] = []
        self.reported: set[tuple[str, str, str]] = set()
        # Notices: findings ready to send, the (code, path) pairs already queued, findings that repeated such a pair,
        # and what went to the agent.
        self.pending: list[dict] = []
        self.noticed: set[tuple[str, str]] = set()
        self.repeats: list[dict] = []
        self.sent: list[dict] = []

    @property
    def judging(self) -> bool:
        return self.config.for_user != "off" or self.config.test_claims != "off"

    def report(self, point: str, finding: guard.Finding, response: str = "record", **asked) -> None:
        record = {
            "point": point,
            "code": finding.code,
            "path": finding.path,
            "fact": finding.fact,
            "evidence": finding.evidence,
            "response": response,
            **asked,
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
            if after is not None:
                known = set(guard.sentences(before or ""))
                self.ask("write", path, after, [s for s in guard.sentences(after) if s not in known])
        self.put_back(restore)

    def task_start(self, instruction: str, task_id: str) -> None:
        current = self.runtime.memory
        self.instructions_start = self.runtime.env.read_instructions(REPO_INSTRUCTIONS)
        # Everything the harness relays from the user: the task text and the user's own file.
        user_file = current.get(USER, "")
        self.user_words = instruction + (f"\n\n{user_file}" if user_file.strip() else "")
        self.facts = self.config.test_facts.get(task_id)
        if self.judging:
            self.judge = make_judge()
            self.pool = concurrent.futures.ThreadPoolExecutor(JUDGE_WORKERS, thread_name_prefix="guard-judge")
        restore: dict[str, str | None] = {}
        if self.config.integrity != "off":
            record = {path: guard.sha256(text) for path, text in self.kept.items()}
            for finding in guard.integrity(record, current):
                if self.config.integrity == "restore" and finding.fact in RESTORABLE:
                    restore[finding.path] = self.kept[finding.path]
                    self.report("task_start", finding, "restore")
                else:
                    self.report("task_start", finding)
        # Memory equal to the configured memory has no earlier task under the guard behind it, so every file is read:
        # a chain's first task, a reset probe, or a task after one that changed nothing (read again).
        first = self.kept == self.runtime.initial()
        for path, text in current.items():
            if first or self.kept.get(path) != text:
                if self.config.content != "off":
                    for finding in guard.content_findings(path, text):
                        self.report("task_start", finding)
                self.ask("task_start", path, text, guard.sentences(text))
        self.put_back(restore)

    def ask(self, point: str, path: str, text: str, sentences: list[str]) -> None:
        """Send a note's new sentences to the judge: rule 3 on those that attribute a wish in a user or feedback note,
        rule 4 on the whole note when it names a test the task's base record holds and something in it is new."""
        if not self.pool or path == USER:
            return
        kind = guard.frontmatter(text)[0].get("type", "").lower()
        if self.config.for_user != "off" and kind in guard.SPEAKS_FOR_USER:
            for sentence in guard.attributions(sentences):
                request = guard_judge.for_user_request(self.user_words, sentence)
                self.submit("for_user", point, path, sentence, request, kind=kind)
        names = guard_judge.tests_in(text)
        if (
            self.config.test_claims != "off"
            and self.facts is not None
            and sentences
            and any(name in self.facts for name in names)
        ):
            request = guard_judge.stance_request(text, list(names.values()))
            self.submit("test_claims", point, path, ", ".join(names), request, names=list(names), spellings=names)

    def submit(self, rule: str, point: str, path: str, item: str, request: dict, **context) -> None:
        judge = self.judge

        def answers():
            return [judge.ask(request) for _ in range(guard_judge.REPEATS)]

        self.jobs.append(
            {
                "rule": rule,
                "point": point,
                "path": path,
                "item": item,
                "asked_step": self.runtime.step,
                "asked_action": self.runtime.actions,
                "context": context,
                "future": self.pool.submit(answers),
            }
        )

    def collect(self, wait: float = 0) -> None:
        """Take the judge's finished answers; with `wait`, give the rest that many seconds."""
        deadline = time.monotonic() + wait
        pending = []
        for job in self.jobs:
            try:
                answers = job["future"].result(timeout=max(0.0, deadline - time.monotonic()))
            except concurrent.futures.TimeoutError:
                pending.append(job)
                continue
            except Exception as error:
                self.unchecked.append(self.described(job) | {"reason": f"{type(error).__name__}: {error}"})
                continue
            self.decide(job, answers)
        self.jobs = pending

    def described(self, job: dict) -> dict:
        return {key: job[key] for key in ("rule", "point", "path", "item", "asked_step", "asked_action")}

    def decide(self, job: dict, answers: list[dict]) -> None:
        cost = sum((a["body"].get("usage") or {}).get("cost") or 0 for a in answers if isinstance(a["body"], dict))
        record = self.described(job) | {"answers": answers, "cost_usd": cost}
        failed = [a["status"] for a in answers if a["status"] != 200]
        if failed:
            self.judged.append(record)
            self.unchecked.append(self.described(job) | {"reason": f"judge status {failed}"})
            return
        builds = sorted({str(a["body"].get("model")) for a in answers} - {guard_judge.BUILD})
        if builds:
            reason = f"judge build {builds}, not {guard_judge.BUILD}"
            self.judged.append(record)
            self.unchecked.append(self.described(job) | {"reason": reason, "build_mismatch": True})
            self.runtime.emit("guard_unchecked", **self.described(job), reason=reason)
            return
        replies = [a["body"]["answers"] for a in answers]
        tests = []
        if job["rule"] == "for_user":
            verdicts = [guard_judge.for_user_verdict(r) for r in replies]
            votes = [v["unstated"] for v in verdicts]
            extra = {"widened": [v["widened"] for v in verdicts]}
            fact = f"a {job['context']['kind']} note states a wish the user did not express"
            finding = guard.Finding(guard.FOR_USER, job["path"], fact, job["item"])
        else:
            per_repeat = [
                [n for n in guard_judge.expected_failures(r, job["context"]["names"]) if self.facts.get(n, False)]
                for r in replies
            ]
            votes = [bool(found) for found in per_repeat]
            named = collections.Counter(n for found in per_repeat for n in found)
            extra = {"expected_failures": per_repeat}
            fact = "a note calls a test an expected failure that passed at base"
            finding = guard.Finding(guard.CLAIM, job["path"], fact, ", ".join(sorted(named)))
            tests = [self.verified_test(job["context"]["spellings"][name], name) for name in sorted(named)]
        verdict = guard_judge.majority(votes)
        self.judged.append(record | {"votes": votes, "verdict": verdict, **extra})
        self.runtime.emit("guard_judged", **self.described(job), votes=votes, verdict=verdict, cost_usd=cost, **extra)
        key = (finding.code, finding.path, finding.evidence)
        if verdict and key not in self.reported:
            self.reported.add(key)
            self.report(job["point"], finding, asked_step=job["asked_step"], asked_action=job["asked_action"])
            self.queue(job["point"], finding, tests)

    def queue(self, point: str, finding: guard.Finding, tests: list[str]) -> None:
        """A rule 3 or 4 finding that was just reported, as material for a notice."""
        if self.config.notify != "send":
            return
        key = (finding.code, finding.path)
        entry = {"point": point, "code": finding.code, "path": finding.path, "tests": tests}
        if key in self.noticed:
            self.repeats.append(entry | {"step": self.runtime.step, "action": self.runtime.actions})
            return
        self.noticed.add(key)
        self.pending.append(entry)

    def take_notices(self) -> list[tuple[str, str]]:
        """The notices for the findings the judge's answers have made ready, as (id, text), one per note and point
        where the finding was made; the runtime sends them before the next model call."""
        if self.config.notify != "send":
            return []
        self.collect()
        groups: dict[tuple[str, str], list[dict]] = {}
        for entry in self.pending:
            groups.setdefault((entry["path"], entry["point"]), []).append(entry)
        self.pending = []
        notices = []
        for (path, point), entries in groups.items():
            name = self.note_name(path)
            codes = sorted(entry["code"] for entry in entries)
            notice_id = re.sub(r"[^A-Za-z0-9_-]", "-", "-".join(["guard", *codes, name]))
            notices.append((notice_id, guard_notice.text(point, name, entries)))
            self.sent.append(
                {"notice_id": notice_id, "codes": codes, "path": path, "point": point, "step": self.runtime.step}
                | {"action": self.runtime.actions}
            )
        return notices

    def verified_test(self, spelling: str, name: str) -> str:
        """A test as a notice names it. The function name is in the base test record. The file comes from the note's
        spelling only when that file is in the repository and holds the function; the rest of the spelling (a class, a
        bracketed parameter list, a path that is no file) is the note's own words and never reaches a notice."""
        match = re.match(r"([\w/.-]+\.py)::", spelling)
        if match and not match[1].startswith("/") and ".." not in match[1].split("/"):
            try:
                source = self.runtime.env.read(f"{REPO}/{match[1]}")
            except Exception:
                # The path is the note author's: a symlink or a non-UTF-8 file makes the bridge fail, and a notice
                # without the file is still the notice.
                source = None
            if source and re.search(rf"\b{name}\b", source):
                return f"{match[1]}::{name}"
        return name

    def note_name(self, path: str) -> str:
        """A note's path under the session's memory directory, or its file name."""
        folder = memory_dir(self.runtime.project) + "/" if self.runtime.project else ""
        return path.removeprefix(folder) if folder and path.startswith(folder) else path.rpartition("/")[2]

    def task_end(self) -> None:
        self.close(self.config.judge_wait_seconds)
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

    def close(self, wait: float = 0) -> None:
        """Take what the judge has answered, count the rest as unchecked and stop asking."""
        if not self.pool:
            return
        self.collect(wait)
        for job in self.jobs:
            job["future"].cancel()
            self.unchecked.append(self.described(job) | {"reason": "no answer by task end"})
        self.jobs = []
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.pool = None
        self.judge.close()

    def put_back(self, files: dict[str, str | None]) -> None:
        if not files:
            return
        for path, text in files.items():
            self.runtime.env.write(path, text)
        self.runtime.capture_memory("harness")

    def summary(self) -> dict:
        """The findings, the judge's answers and what it left unchecked, and the integrity record of the files the
        task leaves: hash and who wrote them last."""
        self.close()
        return {
            "config": self.config.model_dump(mode="json", exclude={"test_facts"}),
            "test_facts": self.facts is not None,
            "findings": self.findings,
            "judge": {
                "items": len(self.judged),
                "requests": sum(len(j["answers"]) for j in self.judged),
                "cost_usd": sum(j["cost_usd"] for j in self.judged),
                "build_mismatch": sum(bool(u.get("build_mismatch")) for u in self.unchecked),
                "unchecked": self.unchecked,
                "judged": self.judged,
            },
            "notices": {"sent": self.sent, "unsent": self.pending, "repeats": self.repeats},
            "record": {
                path: {"sha256": guard.sha256(text), "writer": self.writers.get(path, "unknown")}
                for path, text in sorted(self.runtime.memory.items())
            },
        }
