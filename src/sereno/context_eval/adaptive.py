"""The adaptive memory writer hook of the attack_adaptive arm.

An external program writes into the carried memory before every session of a chain, after reading what the agent did
in the session that just ended. This module is the generic machinery around such a program: it grades the finished
session, hands the program a request, runs it on the host under a timeout, checks what it wants to write, applies the
writes all or nothing, records everything and replays a recorded decision on resume. Nothing here knows what the program
writes or why.
"""

import bisect
import contextlib
import difflib
import hashlib
import itertools
import json
import os
import posixpath
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from sereno.context_eval.config import fingerprint
from sereno.context_eval.engine import LINES, write_atomic
from sereno.context_eval.evaluator import GRADED, read_object
from sereno.context_eval.memory import USER, index_path, is_index, loaded_by, memory_dir, resolve, resolve_files
from sereno.context_eval.schema import Adaptive, AdaptiveLimits, MemoryConfig, validate_path

STATUSES = ("applied", "no_change", "rejected", "writer_failed", "timeout")
# The statuses of a step the program failed; a run of them stops the arm.
FAILED = frozenset({"writer_failed", "timeout"})
SCHEMA_VERSION = "1.0"
# What the harness keeps of the program's stdout and stderr, each, and the largest response it reads.
CAPTURE_BYTES = 1_000_000
RESPONSE_BYTES = 8_000_000
# More actions than any step can use, since a step touches at most max_files_touched (at most 100) files.
MAX_ACTIONS = 1000
NOTE_CHARS = 20_000
# Longest path and path component Linux accepts.
PATH_BYTES, NAME_BYTES = 4000, 255
LINK = re.compile(r"\]\(([^)\s]+)\)")


def printable(text: str) -> str:
    """Text a UTF-8 file can hold: a lone surrogate, which a program's JSON may carry, becomes an escape."""
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


class ResponseError(ValueError):
    """The program's answer is not in the contract's form: a failure of the program, not a rejected plant."""


@dataclass(frozen=True)
class Action:
    op: str
    path: str
    content: str = ""

    def record(self) -> dict:
        content = {"content": printable(self.content)} if self.op == "write" else {}
        return {"op": self.op, "path": printable(self.path), **content}


@dataclass(frozen=True)
class Change:
    """What one action does to the memory: `created`, `changed` or `deleted`; `unchanged` and `absent` are no-ops."""

    path: str
    op: str
    result: str
    before: str | None
    after: str | None

    @property
    def real(self) -> bool:
        return self.result in {"created", "changed", "deleted"}

    def record(self) -> dict:
        return {
            "path": self.path,
            "op": self.op,
            "result": self.result,
            "before_sha256": None if self.before is None else fingerprint(self.before),
            "after_sha256": None if self.after is None else fingerprint(self.after),
            "before_bytes": None if self.before is None else len(self.before.encode()),
            "after_bytes": None if self.after is None else len(self.after.encode()),
        }


@dataclass
class Decision:
    """How a transition ended: its status and reason, the program's note, its actions as parsed, the violations found
    and the changes the actions make. Only a valid response that changes something is applied."""

    status: str
    reason: str | None = None
    note: str | None = None
    actions: list[Action] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)


def parse_actions(raw: object, project: str) -> list[Action]:
    """The actions of a response in the contract's form, `{project}` resolved; anything else is a ResponseError."""
    if not isinstance(raw, list):
        raise ResponseError("actions must be a list")
    actions = []
    for number, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ResponseError(f"action {number} is not an object")
        op = item.get("op")
        fields = {"write": {"op", "path", "content"}, "delete": {"op", "path"}}.get(op) if isinstance(op, str) else None
        if fields is None:
            raise ResponseError(f"action {number}: op must be write or delete")
        if extra := set(item) - fields:
            raise ResponseError(f"action {number}: unexpected fields {sorted(extra)}")
        path, content = item.get("path"), item.get("content", "")
        if not isinstance(path, str) or not path:
            raise ResponseError(f"action {number}: path must be a non-empty string")
        if not isinstance(content, str) or (item["op"] == "write" and "content" not in item):
            raise ResponseError(f"action {number}: a write needs content, a string")
        actions.append(Action(item["op"], resolve(path, project), content))
    return actions


def path_problem(path: str, root: str) -> str | None:
    """Why a path is not a file the program may name, or None: below `root`, canonical, and a name Linux and the
    memory rules accept."""
    if path == USER:
        return "the user's file is out of reach"
    try:
        validate_path(path, root)
        encoded = path.encode()
    except ValueError:
        return f"{path!r} is not a canonical file path below {root}"
    if (
        "\0" in path
        or len(encoded) > PATH_BYTES
        or any(len(p.encode()) > NAME_BYTES for p in PurePosixPath(path).parts)
    ):
        return f"{path!r} is not a usable file name"
    return None


def content_problem(content: str) -> str | None:
    try:
        content.encode()
    except UnicodeEncodeError:
        return "content is not valid UTF-8"
    return "content holds a NUL byte" if "\0" in content else None


def keeps_lines(old: str, new: str, droppable=lambda line: False) -> bool:
    """Whether every old line still stands, unchanged and in order, among the lines of `new`: `new` only adds lines
    between or around them. A `droppable` old line may change or go. The last old line, when the file does not end
    its line, may gain a terminator, which adding a line after it requires."""
    lines = LINES.findall(old)
    open_end = len(lines) - 1 if old and not old.endswith("\n") else None
    fresh = LINES.findall(new)
    position = 0
    for number, line in enumerate(lines):
        if droppable(line):
            continue
        allowed = {line, line + "\n", line + "\r\n"} if number == open_end else {line}
        while position < len(fresh) and fresh[position] not in allowed:
            position += 1
        if position == len(fresh):
            return False
        position += 1
    return True


def adds_match(pattern: re.Pattern, before: str, content: str) -> bool:
    """Whether `pattern` matches `content` where a write adds or changes text. The lines of `content` are aligned with
    those of `before` (difflib, compared without their terminators); a match is inherited, and does not count, only
    when it lies within one run of old lines that stands unchanged and contiguous in `content` and `before` holds the
    same match at that place. A match that touches an added or changed line, or spans old and new text, counts, and so
    does every match in a new file. A terminator the old last line lacked is new text."""
    old, new = LINES.findall(before), LINES.findall(content)
    old_at = list(itertools.accumulate(map(len, old), initial=0))
    new_at = list(itertools.accumulate(map(len, new), initial=0))
    keys = [[line.removesuffix("\n") for line in lines] for lines in (old, new)]
    runs = []  # (start in content, end in content, start in before), in order
    matcher = difflib.SequenceMatcher(None, *keys, autojunk=False)
    for i, j, size in matcher.get_matching_blocks():
        if size:
            last_old, last_new = old[i + size - 1], new[j + size - 1]
            gained = last_new.endswith("\n") and not last_old.endswith("\n")
            runs.append((new_at[j], new_at[j + size] - gained, old_at[i]))
    starts = [run[0] for run in runs]

    def inherited(match: re.Match) -> bool:
        index = bisect.bisect_right(starts, match.start()) - 1
        if index < 0 or match.end() > runs[index][1]:
            return False
        shift = runs[index][2] - runs[index][0]
        same = pattern.search(before, match.start() + shift)
        return same is not None and same.span() == (match.start() + shift, match.end() + shift)

    position = 0
    # Searching again from the next character, not from the match's end, keeps an inherited match from hiding a new
    # one that overlaps it.
    while position <= len(content) and (match := pattern.search(content, position)) is not None:
        if not inherited(match):
            return True
        position = match.start() + 1
    return False


def links_only_to(line: str, owned: set[str], folder: str) -> bool:
    """Whether an index line links to files and every file it links to is one the program created."""
    targets = LINK.findall(line)
    for target in targets:
        target = target.split("#", 1)[0]
        if not target or "://" in target:
            return False
        if posixpath.normpath(posixpath.join(folder, target)) not in owned:
            return False
    return bool(targets)


def plan(
    memory: dict[str, str],
    actions: list[Action],
    owned: set[str],
    limits: AdaptiveLimits,
    project: str,
    memory_config: MemoryConfig | None = None,
) -> tuple[list[str], list[Change]]:
    """Every violation of the rules in the doc's "Checks on a response" against `memory`, and the change each action
    makes. The first look finds every violation; the changes are meaningful only when there is none."""
    if len(actions) > MAX_ACTIONS:
        return [f"{len(actions)} actions, over the limit of {MAX_ACTIONS}"], []
    root, index = memory_dir(project), index_path(project)
    forbidden = [re.compile(pattern, re.IGNORECASE) for pattern in limits.forbidden_patterns]
    violations, changes, seen, touched = [], [], set(), 0
    for number, action in enumerate(actions):
        who = f"action {number}"
        if problem := path_problem(action.path, root):
            violations.append(f"{who}: {problem}")
            continue
        if action.path in seen:
            violations.append(f"{who}: {action.path} already has an action in this response")
            continue
        seen.add(action.path)
        before = memory.get(action.path)
        # The index is never fully the program's: the agent keeps writing in it.
        free = action.path in owned and action.path != index
        if action.op == "delete":
            if before is None:
                changes.append(Change(action.path, "delete", "absent", None, None))
                continue
            touched += 1
            if free:
                changes.append(Change(action.path, "delete", "deleted", before, None))
            else:
                violations.append(f"{who}: {action.path} is not a file the program created, so it cannot be deleted")
            continue
        content = action.content
        if problem := content_problem(content):
            violations.append(f"{who}: {problem}")
            continue
        result = "created" if before is None else "unchanged" if content == before else "changed"
        changes.append(Change(action.path, "write", result, before, content))
        if result == "unchanged":
            continue
        touched += 1
        if before is not None and not free:
            droppable = (lambda line: links_only_to(line, owned, root)) if action.path == index else (lambda _: False)
            if not keeps_lines(before, content, droppable):
                violations.append(
                    f"{who}: {action.path} is not a file the program created, so a write may only add lines to it"
                )
        if len(content.encode()) > limits.max_file_bytes:
            violations.append(f"{who}: {action.path} is over {limits.max_file_bytes} bytes")
        for position, pattern in enumerate(forbidden):
            if adds_match(pattern, before or "", content):
                violations.append(f"{who}: the content of {action.path} matches forbidden pattern {position}")
    if touched > limits.max_files_touched:
        violations.append(f"{touched} files touched, over the limit of {limits.max_files_touched}")
    resulting = apply_changes(memory, changes)
    for change in changes:
        if change.after is None:
            continue
        parent = posixpath.dirname(change.path)
        while parent != root and parent.startswith(root + "/"):
            if parent in resulting:
                violations.append(f"{change.path}: {parent} is a file, not a directory")
                break
            parent = posixpath.dirname(parent)
        if any(other.startswith(change.path + "/") for other in resulting):
            violations.append(f"{change.path} is a directory with files in it")
    if memory_config is not None and not memory_config.within_limits(resulting):
        violations.append(
            f"the resulting memory holds {len(resulting)} files and "
            f"{sum(len(t.encode()) for t in resulting.values())} bytes, over the memory limits"
        )
    return violations, changes


def apply_changes(memory: dict[str, str], changes: list[Change]) -> dict[str, str]:
    result = dict(memory)
    for change in changes:
        if change.after is None:
            result.pop(change.path, None)
        else:
            result[change.path] = change.after
    return result


def validate_actions(
    memory: dict[str, str],
    actions: list[dict],
    owned: set[str],
    limits: AdaptiveLimits | dict,
    project: str,
    *,
    memory_config: MemoryConfig | None = None,
) -> list[str]:
    """The violations of a response's `actions` against `memory` (path to text), empty when they are valid: the same
    check the harness runs before it plants anything. `owned` holds the paths an earlier applied transition created,
    `limits` the configured limits. The memory limits are checked on `memory` itself, so they are exact only when it
    is the whole carried memory and `memory_config` is given; the harness passes both. Needs no campaign directory
    and no container."""
    try:
        parsed = parse_actions(actions, project)
    except ResponseError as error:
        return [str(error)]
    limits = limits if isinstance(limits, AdaptiveLimits) else AdaptiveLimits.model_validate(limits)
    return plan(memory, parsed, set(owned), limits, project, memory_config)[0]


def load_response(path: Path) -> dict:
    """The program's response.json, checked for its form only."""
    if not path.is_file():
        raise ResponseError("the program wrote no response.json")
    try:
        if path.stat().st_size > RESPONSE_BYTES:
            raise ResponseError("response.json is too large")
        data = json.loads(path.read_bytes().decode())
    except OSError as error:
        raise ResponseError(f"response.json cannot be read: {error}") from None
    except (ValueError, RecursionError) as error:
        if isinstance(error, ResponseError):
            raise
        raise ResponseError(f"response.json is not valid JSON: {error}") from None
    if not isinstance(data, dict):
        raise ResponseError("response.json must hold an object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ResponseError(f"schema_version must be {SCHEMA_VERSION!r}")
    if extra := set(data) - {"schema_version", "actions", "note"}:
        raise ResponseError(f"unexpected fields {sorted(extra)}")
    if "actions" not in data:
        raise ResponseError("actions is missing")
    note = data.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > NOTE_CHARS):
        raise ResponseError(f"note must be a string of at most {NOTE_CHARS} characters")
    return data


class Capture(threading.Thread):
    """Copies a pipe to a file, keeping the first CAPTURE_BYTES and dropping the rest, so a talkative program neither
    blocks on a full pipe nor fills the disk."""

    def __init__(self, stream, path: Path):
        super().__init__(daemon=True)
        self.stream, self.path, self.size = stream, path, 0

    def run(self) -> None:
        with self.path.open("wb") as out:
            while chunk := self.stream.read1(65536):
                out.write(chunk[: max(0, CAPTURE_BYTES - self.size)])
                self.size += len(chunk)


def run_program(argv: list[str], cwd: Path, timeout: int, folder: Path) -> dict:
    """Run the program with the campaign's environment, stdin closed, stdout and stderr captured into `folder`. A
    program that outlives `timeout` is killed with everything it started."""
    started = time.monotonic()
    outcome = {"exit_code": None, "timed_out": False, "error": None}
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        return outcome | {"error": f"cannot start the program: {error}", "seconds": 0.0}
    captures = [Capture(process.stdout, folder / "writer.stdout"), Capture(process.stderr, folder / "writer.stderr")]
    for capture in captures:
        capture.start()
    try:
        outcome["exit_code"] = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # The leader is still unreaped, so its pid still names the group.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        outcome["timed_out"] = True
    # What a program that exited left running dies with it; Linux keeps a pid unused while its group has members.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    for capture in captures:
        capture.join(10)
    seconds = round(time.monotonic() - started, 3)
    return outcome | {
        "seconds": seconds,
        "stdout_bytes": captures[0].size,
        "stderr_bytes": captures[1].size,
        "truncated": any(capture.size > CAPTURE_BYTES for capture in captures),
    }


def transition_dir(arm_dir: Path, number: int) -> Path:
    """The folder of the step before session `number` (0-based): NNN is the session's position, 001 the first."""
    return arm_dir / "transitions" / f"{number + 1:03d}"


def read_transition(path: Path) -> dict | None:
    """A transition.json that parses and names a known status; None when it is missing or was cut short."""
    record = read_object(path)
    return record if record and record.get("status") in STATUSES and isinstance(record.get("index"), int) else None


def owned_paths(arm_dir: Path, before: int) -> set[str]:
    """The paths an applied transition before session `before` created, from the applied.json it recorded. Ownership
    is by path: later edits or deletions by the agent do not end it, and the index is never owned."""
    owned = set()
    for number in range(before):
        folder = transition_dir(arm_dir, number)
        record = read_transition(folder / "transition.json")
        if record is None or record["status"] != "applied":
            continue
        applied = read_object(folder / "applied.json")
        if applied is None:
            raise ValueError(f"cannot read {folder / 'applied.json'}, which a recorded transition needs")
        owned |= {e["path"] for e in applied["files"] if e["result"] == "created" and not is_index(e["path"])}
    return owned


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def adaptive_manifest(adaptive: Adaptive) -> dict:
    """The program as the manifest records it: argv, the sha256 of every word that names an existing file, and the
    limits."""
    files = {word: file_sha256(Path(word)) for word in adaptive.command if os.path.isabs(word) and os.path.isfile(word)}
    return {
        "command": adaptive.command,
        "program_sha256": files,
        "timeout_seconds": adaptive.timeout_seconds,
        "grade_before_step": adaptive.grade_before_step,
        "stop_after_failures": adaptive.stop_after_failures,
        "limits": adaptive.limits.model_dump(),
    }


def default_grader(dataset_root: Path):
    """A grader for one session directory with the DeepSWE verifier, the evaluator `grade` uses. A kept grade that a
    kill left without its second pass gets that pass, as `grade --test-edits-only` adds it; the official run is never
    repeated."""
    from sereno.context_eval.evaluator import GRADE_SCHEMA, DeepSWEEvaluator, grade_session

    evaluator = DeepSWEEvaluator(Path(dataset_root))

    def grade(directory: Path) -> None:
        outcome, reason = grade_session(evaluator, directory)
        if outcome == "unreadable":
            raise RuntimeError(reason)
        kept = read_object(directory / "grade.json") if outcome == "kept" else None
        if kept is not None and "test_edits" not in kept:
            edits = evaluator.backfill(directory, kept)
            write_atomic(directory / "grade.json", kept | {"schema_version": GRADE_SCHEMA, "test_edits": edits})

    return grade


def opening_events(record: dict, applied: dict | None) -> list[tuple[str, dict]]:
    """The events logged at the start of the session after a transition: the step itself, then one memory change per
    file it changed, with sizes and hashes and no contents (those are in applied.json)."""
    events = [
        (
            "adaptive_step",
            {
                "transition": record["index"] + 1,
                "transition_kind": record["kind"],
                "status": record["status"],
                "reason": record.get("reason"),
                "grade_status": record["grade_status"],
                "files_touched": record["files_touched"],
                "durations": record["durations"],
            },
        )
    ]
    for entry in (applied or {}).get("files", []):
        if entry["result"] in {"created", "changed", "deleted"}:
            events.append(
                ("memory_change", {"origin": "adaptive", "owner": "agent", "action_id": None, "action": 0} | entry)
            )
    return events


class AdaptiveStop(RuntimeError):
    """The adaptive arm stops: the program failed too many steps in a row, so no further step or session starts."""


class FailureStreak:
    """Steps that ended as writer_failed or timeout in a row across the campaign's adaptive chains, in the order they
    were decided; a step that ends any other way resets the count. Shared by the chains of every worker. `on_stop`
    is called once with the reason when the streak stops the arm."""

    def __init__(self, limit: int | None, on_stop=None):
        self.limit, self.count, self.steps, self.stopped = limit, 0, [], None
        self.on_stop = on_stop
        self.lock = threading.Lock()

    def check(self) -> None:
        with self.lock:
            if self.stopped:
                raise AdaptiveStop(self.stopped["reason"])

    def record(self, status: str, step: str, aside: str) -> bool:
        """Count one decided step; True when the step moves to `aside` and counts there: it reached the limit, or it
        failed after the arm stopped (a step already in flight when another chain stopped the arm). A step that ends
        any other way after the stop keeps its record in place."""
        failed = status in FAILED
        with self.lock:
            if self.stopped:
                if failed:
                    self.stopped["steps"].append(aside)
                return failed
            if not failed:
                self.count, self.steps = 0, []
                return False
            self.count += 1
            if self.limit is None or self.count < self.limit:
                self.steps.append(step)
                return False
            self.stopped = {
                "reason": f"the adaptive program failed {self.count} steps in a row",
                "steps": [*self.steps, aside],
            }
            if self.on_stop is not None:
                self.on_stop(f"the adaptive arm stopped: {self.stopped['reason']}")
            return True

    def unstarted(self, status: str, step: str, aside: str, move) -> bool:
        """A step decided before the stop and left in place whose session never started: another chain stopped the arm
        between this step's record and its session. A failure then moves aside as one decided after the stop does:
        `move` runs under the lock, and its entry in the stop's steps names `aside` (or joins them, when a later step
        had ended the run of failures it was in). True when it moved."""
        with self.lock:
            if not self.stopped or status not in FAILED:
                return False
            move()
            steps = self.stopped["steps"]
            if step in steps:
                steps[steps.index(step)] = aside
            else:
                steps.append(aside)
            return True


class AdaptiveHook:
    """The step before each session of one attack_adaptive chain. `locate(number)` is the directory of session
    `number`, `projects` maps a task to its memory project, `grader` grades a session directory in place, `stamp`
    names the folder an incomplete attempt is moved to, and `streak` counts program failures across the campaign."""

    def __init__(
        self,
        config,
        output: Path,
        arm_dir: Path,
        *,
        case: str,
        repeat: int,
        target: str,
        projects: dict[str, str],
        locate,
        grader,
        stamp: str,
        streak: FailureStreak | None = None,
    ):
        self.config, self.adaptive = config, config.adaptive
        self.output, self.arm_dir, self.case, self.repeat, self.target = output, arm_dir, case, repeat, target
        self.projects, self.locate, self.grader, self.stamp = projects, locate, grader, stamp
        self.streak = streak or FailureStreak(config.adaptive.stop_after_failures)
        # The step this run just decided and left in place, as (number, status), until its session starts or not.
        self.decided: tuple[int, str] | None = None

    def before(self, number: int, memory: dict[str, str]) -> tuple[dict[str, str], list[tuple[str, dict]]]:
        """The memory session `number` starts from, given the memory the last complete session ended with, and the
        events to log when it starts. A recorded transition is replayed; the program is called only for one that was
        never recorded or was cut short. Raises AdaptiveStop once the program has failed too many steps in a row."""
        self.decided = None
        self.streak.check()
        session = self.config.sessions[number]
        project = self.projects[session.task_id or self.target]
        memory = resolve_files(memory, project)
        folder = transition_dir(self.arm_dir, number)
        record = read_transition(folder / "transition.json")
        if record is None:
            if folder.exists():
                old = self.arm_dir / "transitions" / "superseded" / self.stamp / folder.name
                old.parent.mkdir(parents=True, exist_ok=True)
                folder.rename(old)
            memory, record, applied = self.transit(number, project, memory, folder)
        else:
            memory, applied = self.replay(memory, folder, record)
        return memory, opening_events(record, applied)

    def transit(self, number: int, project: str, memory: dict[str, str], folder: Path):
        started = datetime.now(UTC)
        clock = time.monotonic()
        folder.mkdir(parents=True)
        state = self.arm_dir / "state"
        state.mkdir(exist_ok=True)
        finished, grade_status, grade_error = None, "none", None
        grade_seconds = 0.0
        if number:
            before = self.locate(number - 1)
            if self.adaptive.grade_before_step:
                graded = time.monotonic()
                grade_status, grade_error = self.grade(before)
                grade_seconds = round(time.monotonic() - graded, 3)
            else:
                grade_status = "disabled"
            finished = self.finished(number - 1, before, grade_status)
        owned = owned_paths(self.arm_dir, number)
        request = self.request(number, project, memory, folder, owned, finished)
        write_atomic(folder / "request.json", request)
        request_sha256 = file_sha256(folder / "request.json")
        outcome = run_program(
            [*self.adaptive.command, str(folder / "request.json"), str(folder / "response.json")],
            state,
            self.adaptive.timeout_seconds,
            folder,
        )
        decision = self.decide(outcome, folder, project, memory, owned)
        status = decision.status
        after = apply_changes(memory, decision.changes) if status == "applied" else memory
        done = status in {"applied", "no_change"}
        applied = {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "reason": decision.reason,
            "note": decision.note,
            "violations": decision.violations,
            "actions": [action.record() for action in decision.actions],
            "files": [change.record() for change in decision.changes] if done else [],
            "memory_before_sha256": fingerprint(memory),
            "memory_after_sha256": fingerprint(after),
        }
        write_atomic(folder / "applied.json", applied)
        response = folder / "response.json"
        record = {
            "schema_version": SCHEMA_VERSION,
            "index": number,
            "kind": request["kind"],
            "session_id": request["next_session"]["id"],
            "status": status,
            "reason": decision.reason,
            "grade_status": grade_status,
            "grade_error": grade_error,
            "files_touched": sum(change.real for change in decision.changes) if done else 0,
            "writer": {
                key: outcome.get(key)
                for key in ("exit_code", "timed_out", "error", "stdout_bytes", "stderr_bytes", "truncated")
            },
            "request_sha256": request_sha256,
            "response_sha256": file_sha256(response) if response.is_file() else None,
            "attempt": 1,
            "started_at": started.isoformat(),
            "durations": {
                "grade_seconds": grade_seconds,
                "writer_seconds": outcome.get("seconds"),
                "total_seconds": round(time.monotonic() - clock, 3),
            },
        }
        write_atomic(folder / "transition.json", record)
        # The step that stops the arm, and a step that fails after the stop, moves aside with its record, so a resume
        # decides it again with the program. Once the arm has stopped no session starts, whatever the step's status; a
        # failure left in place whose session then does not start moves aside too (unstarted).
        aside = self.aside(folder)
        if self.streak.record(status, str(folder.relative_to(self.output)), str(aside.relative_to(self.output))):
            aside.parent.mkdir(parents=True, exist_ok=True)
            folder.rename(aside)
        else:
            self.decided = (number, status)
        self.streak.check()
        return after, record, applied

    def aside(self, folder: Path) -> Path:
        return self.arm_dir / "transitions" / "stopped" / self.stamp / folder.name

    def unstarted(self, number: int) -> None:
        """Session `number` does not start. When the arm stopped first and the step this run decided before it failed
        and stayed in place, the step moves aside with its record, so a resume decides it again with the program
        instead of replaying a failure whose session never ran."""
        if self.decided is None or self.decided[0] != number:
            return
        folder = transition_dir(self.arm_dir, number)
        aside = self.aside(folder)

        def move():
            aside.parent.mkdir(parents=True, exist_ok=True)
            folder.rename(aside)

        status = self.decided[1]
        self.decided = None
        self.streak.unstarted(status, str(folder.relative_to(self.output)), str(aside.relative_to(self.output)), move)

    def grade(self, directory: Path) -> tuple[str, str | None]:
        """Grade the session that just ended with the grader behind `grade`; a problem is a status, never a stop.
        `graded` means the verifier decided, `undecided` that grade.json holds no verdict."""
        try:
            self.grader(directory)
        except Exception as error:
            return "error", f"{type(error).__name__}: {error}"
        grade = read_object(directory / "grade.json")
        if grade is None:
            return "error", "no readable grade.json after grading"
        return ("graded" if grade.get("status") in GRADED | {"verifier_timeout"} else "undecided"), None

    def finished(self, number: int, directory: Path, grade_status: str) -> dict:
        result = read_object(directory / "result.json")
        grade = read_object(directory / "grade.json") if grade_status in {"graded", "undecided"} else None
        ctrf = directory / "grade" / "verifier" / "ctrf.json"
        return {
            "index": number,
            "id": self.config.sessions[number].id,
            "task_id": (result or {}).get("task_id"),
            "dir": str(directory),
            "result": result,
            "grade": grade,
            "grade_status": grade_status,
            "ctrf_path": str(ctrf) if grade_status == "graded" and ctrf.is_file() else None,
        }

    def request(self, number: int, project: str, memory: dict, folder: Path, owned: set[str], finished) -> dict:
        session = self.config.sessions[number]
        task_id = session.task_id or self.target
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "between_sessions" if number else "before_first_session",
            "campaign_dir": str(self.output),
            "case": self.case,
            "arm": "attack_adaptive",
            "repeat": self.repeat,
            "agent_id": "main",
            "arm_dir": str(self.arm_dir),
            "state_dir": str(self.arm_dir / "state"),
            "transition_dir": str(folder),
            "finished_session": finished,
            "next_session": {
                "index": number,
                "id": session.id,
                "task_id": task_id,
                "task_dir": str(self.config.dataset_root / "tasks" / task_id),
                "project": project,
                "memory_dir": memory_dir(project),
            },
            "memory": {
                "files": {p: t for p, t in sorted(loaded_by(memory, project).items()) if p != USER},
                "owned": sorted(owned),
            },
            "limits": self.adaptive.limits.model_dump(),
            "attempt": 1,
        }

    def decide(self, outcome: dict, folder: Path, project: str, memory: dict, owned: set[str]) -> Decision:
        timeout = self.adaptive.timeout_seconds
        if outcome["timed_out"]:
            return Decision("timeout", f"the program ran past {timeout} seconds")
        if outcome["error"]:
            return Decision("writer_failed", outcome["error"])
        if outcome["exit_code"] != 0:
            return Decision("writer_failed", f"the program exited with status {outcome['exit_code']}")
        try:
            response = load_response(folder / "response.json")
            actions = parse_actions(response["actions"], project)
        except ResponseError as error:
            return Decision("writer_failed", f"invalid response: {error}")
        violations, changes = plan(memory, actions, owned, self.adaptive.limits, project, self.config.memory)
        note = response.get("note")
        note = None if note is None else printable(note)
        if violations:
            return Decision(
                "rejected", f"{len(violations)} rule violations", note, actions, list(map(printable, violations))
            )
        status = "applied" if any(change.real for change in changes) else "no_change"
        return Decision(status, None, note, actions, [], changes)

    def replay(self, memory: dict[str, str], folder: Path, record: dict):
        """Re-apply a recorded transition without calling the program, and stop when the memory it meets is not the
        memory it was decided on."""
        applied = read_object(folder / "applied.json")
        if applied is None:
            raise ValueError(f"cannot resume: {folder / 'applied.json'} is missing or unreadable")
        if fingerprint(memory) != applied["memory_before_sha256"]:
            raise ValueError(f"cannot resume: the memory before transition {folder.name} differs from the recorded one")
        if record["status"] != "applied":
            return memory, applied
        after = dict(memory)
        for action in applied["actions"]:
            if action["op"] == "write":
                after[action["path"]] = action["content"]
            else:
                after.pop(action["path"], None)
        if fingerprint(after) != applied["memory_after_sha256"]:
            raise ValueError(f"cannot resume: transition {folder.name} no longer gives the recorded memory")
        return after, applied
