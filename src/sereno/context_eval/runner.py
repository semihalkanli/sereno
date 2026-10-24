"""Run isolated clean, carry, reset and ablation arms, with an optional user repair, and retain reproducible,
grade-ready artifacts."""

import hashlib
import json
import os
import platform
import shlex
import shutil
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import yaml

from sereno.context_eval.adaptive import (
    AdaptiveHook,
    AdaptiveStop,
    FailureStreak,
    adaptive_manifest,
    default_grader,
    read_transition,
)
from sereno.context_eval.agents import MINI_SWE_AGENTS, action_timeout, mini_swe_config
from sereno.context_eval.config import fingerprint, validate
from sereno.context_eval.dataset import image_identity, load_task, provenance
from sereno.context_eval.engine import (
    LINES,
    EventLog,
    InterventionEngine,
    Runtime,
    intervention_catalog,
    message_text,
    separate_patch,
    write_atomic,
    write_json,
)
from sereno.context_eval.environment import DockerEnvironment, sweep
from sereno.context_eval.memory import (
    PROJECT,
    REPO_INSTRUCTIONS,
    USER,
    FileMemory,
    MemoryViolation,
    agent_violation,
    instructions,
    memory_dir,
    project_key,
    repo_instructions,
    resolve_files,
)
from sereno.context_eval.metrics import content_found, content_spans, report
from sereno.context_eval.schema import AgentOutcome

COMPLETE = {
    "Submitted",
    "LimitsExceeded",
    "TimeExceeded",
    "RepeatedFormatError",
    "MemoryViolation",
    "Refused",
    "script_complete",
}
"""Exit statuses that end a whole task as an agent outcome; its patch is collected for grading."""


def ended_by(exit_status: str, steps: int, max_steps: int) -> str | None:
    """The limit that ended a run. mini-swe reports step and cost caps alike as LimitsExceeded."""
    if exit_status == "TimeExceeded":
        return "time"
    if exit_status == "MemoryViolation":
        return "memory"
    if exit_status == "LimitsExceeded":
        return "steps" if 0 < max_steps <= steps else "cost"
    return None


class NotStarted(RuntimeError):
    """A session that was not started: it leaves no result, so reports count it missing and a resume runs it."""


class Budget:
    """Reserve whole per-session budgets across workers; never start a session with a smaller cap."""

    def __init__(self, limit: float):
        self.limit, self.spent, self.reserved = limit, 0.0, 0.0
        # Float sums of reservations need not return to exactly zero; the count of holders does.
        self.holders = 0
        self.unknown_cost = False
        # Why the campaign starts no further session, such as the adaptive arm's stop; None while it runs.
        self.halted: str | None = None
        self.condition = threading.Condition()

    def halt(self, reason: str) -> None:
        """Start no further session in any arm; the sessions already started finish."""
        with self.condition:
            self.halted = self.halted or reason
            self.condition.notify_all()

    def admit(self) -> None:
        """Refuse a session once the campaign is halted, whether or not it reserves a cost budget."""
        with self.condition:
            if self.halted:
                raise NotStarted(self.halted)

    def acquire(self, amount: float) -> float:
        with self.condition:
            while True:
                if self.halted:
                    raise NotStarted(self.halted)
                if self.unknown_cost:
                    raise NotStarted("campaign cost accounting unavailable after a failed model call")
                # A small tolerance keeps float sums such as 0.3 - 0.1 from refusing an exact fit.
                if self.limit - self.spent - self.reserved >= amount - 1e-9:
                    self.reserved += amount
                    self.holders += 1
                    return amount
                if not self.holders:
                    raise NotStarted("campaign cost budget exhausted")
                # A running session may finish under its cap and leave enough for this one.
                self.condition.wait()

    def finish(self, reserved: float, spent: float, *, unknown=False):
        with self.condition:
            self.reserved -= reserved
            self.holders -= 1
            self.spent += spent
            self.unknown_cost |= unknown
            self.condition.notify_all()


def run_session(
    config,
    session,
    task,
    identity,
    memory,
    engine,
    adapter,
    directory,
    budget,
    env_factory,
    opening=(),
    *,
    lineage_start,
):
    """Run one session in a fresh container. `opening` holds events (kind and fields) to log right after the session
    starts, such as the step of the adaptive hook that planted part of its memory. `lineage_start` says that the session
    starts its memory's lineage, for the live guard."""
    project = project_key(task.repository)
    # Configured memory names the project folder as {project}; carried memory already holds this session's paths.
    memory = resolve_files(memory, project)
    directory.mkdir(parents=True, exist_ok=False)
    try:
        budget.admit()
        allowance = None if config.agent == "scripted" else budget.acquire(config.cost_limit_usd)
    except NotStarted:
        directory.rmdir()
        raise
    run_id = str(uuid.uuid4())
    log = EventLog(directory / "events.jsonl", run_id)
    runtime, env = None, None
    started = time.monotonic()
    wall = config.wall_time_limit_seconds or task.agent_timeout_seconds
    result = {
        "schema_version": "1.0",
        "run_id": run_id,
        "session_id": session.id,
        "task_id": task.id,
        "base_commit": task.base_commit,
        "image": identity,
        "exposure_phase": session.exposure,
        "memory_project": project,
        "status": "invalid",
        "exit_status": "not_started",
        "limit": None,
        "wall_time_limit_seconds": wall,
        "action_timeout_seconds": None,
        "has_timeout": None,
        "steps": 0,
        "cost_usd": 0.0,
        "cost_limit_usd": allowance,
        "markers": {e.id: e.marker for e in engine.events if e.marker},
        "interventions": intervention_catalog(engine.events),
        "untracked_interventions": [e.id for e in engine.events if not e.marker],
        "workspace": {},
        "patch_status": "missing",
        "task_success": "not_evaluated",
    }
    try:
        write_json(directory / "memory_start.json", memory)
        if not config.memory.within_limits(memory):
            # Seeds and carried agent memory always fit, so only a repair can break the limits: not an agent outcome.
            raise RuntimeError("restored memory exceeds the memory limits")
        env = env_factory(identity["id"], wall)
        # The runner's own commands, such as patch collection, run under the agent's action limit too.
        env.action_timeout = result["action_timeout_seconds"] = action_timeout(config)
        result["has_timeout"] = getattr(env, "has_timeout", None)
        head = env.execute("git -c safe.directory=/app rev-parse HEAD")["output"].strip()
        # Task metadata may abbreviate the base commit, as DeepSWE does; Git resolves it or rejects an ambiguous one,
        # and the patches are collected against the resolved full id.
        base = env.execute(
            f"git -c safe.directory=/app rev-parse --verify --quiet {shlex.quote(task.base_commit + '^{commit}')}"
        )
        if base["returncode"] or base["output"].strip() != head:
            raise ValueError("image HEAD does not match the task base commit")
        mem = FileMemory(project)
        # The memory instructions say the directory already exists.
        if env.execute(f"mkdir -p {memory_dir(project)}")["returncode"]:
            raise RuntimeError("cannot initialize memory directory")
        if env.snapshot_memory(config.memory.max_files, config.memory.max_bytes):
            raise ValueError("task image must start with an empty memory root")
        mem.restore(env, memory)
        snapshot = env.snapshot_memory

        def checked_snapshot(max_files: int, max_bytes: int) -> dict[str, str]:
            try:
                return snapshot(max_files, max_bytes)
            except Exception as error:
                if agent_violation(error):
                    raise MemoryViolation(str(error)) from error
                raise

        env.snapshot_memory = checked_snapshot
        runtime = Runtime(env, engine, log, session, config.memory, memory, config.checks, project, config.guard)
        log.emit(
            "session_start",
            session_id=session.id,
            task_id=task.id,
            image=identity,
            action_timeout_seconds=env.action_timeout,
            has_timeout=result["has_timeout"],
        )
        for kind, fields in opening:
            log.emit(kind, session_id=session.id, **fields)
        engine.apply(runtime, "session_start")
        instruction = session.instruction or task.instruction
        if runtime.guard:
            # After the interventions, which stand for whatever changed memory between tasks; before the context.
            runtime.guard.task_start(instruction, task.id, lineage_start=lineage_start)
        # Read after session_start interventions, so a planted instruction file loads as the repository's own.
        repo = repo_instructions(env.read_instructions(REPO_INSTRUCTIONS))
        context = mem.context(runtime.memory, repo)
        runtime.initial_memory_sections = mem.sections(runtime.memory)
        bounded = config.model_copy(update={"wall_time_limit_seconds": wall})
        try:
            outcome = adapter.run(runtime, instruction, context, bounded, session)
        except MemoryViolation as error:
            # The agent's own memory writes ended the task; the last valid snapshot carries on.
            outcome = {**getattr(runtime, "partial_agent_result", {}), "exit_status": "MemoryViolation"}
            result["memory_error"] = str(error)
            log.emit("memory_violation", session_id=session.id, message=str(error))
        finally:
            # The exact opening messages the adapter rendered, whether the agent returned or failed.
            opening = getattr(runtime, "initial_messages", [])
            write_json(
                directory / "initial_context.json",
                {
                    "instruction": instruction,
                    "memory": context,
                    "repo_instructions": {path: fingerprint(text) for path, text in repo.items()},
                    "messages": [{"sha256": fingerprint(message_text([m])), "message": m} for m in opening],
                },
            )
        agent_result = AgentOutcome.model_validate(outcome).model_dump()
        result.update({k: v for k, v in agent_result.items() if k != "messages"})
        write_json(directory / "traj.json", getattr(runtime, "agent_trajectory", agent_result))
        status = agent_result["exit_status"]
        result["status"] = "complete" if status in COMPLETE else "invalid"
        result["limit"] = ended_by(status, agent_result["steps"], session.max_steps)
        if status == "MemoryViolation":
            # session_end hooks and a final snapshot would read the rejected directory again.
            memory = dict(runtime.memory)
        else:
            engine.apply(runtime, "session_end")
            memory = runtime.capture_memory("agent")
            if runtime.guard:
                runtime.guard.task_end()
        for check in config.checks:
            if check.source == "workspace" and (not check.sessions or session.id in check.sessions):
                result["workspace"][check.path] = env.read(check.path) or ""
        (directory / "raw.patch").write_bytes(env.collect_patch(head))
        try:
            patch = separate_patch(env, runtime.journal, head)
            (directory / "model.patch").write_bytes(patch)
            result["patch_status"] = "ready"
        except ValueError as error:
            result["patch_status"] = "ambiguous"
            result["patch_error"] = str(error)
    except Exception as error:
        result["status"] = "invalid"
        result["error"] = str(error)
        result["exit_status"] = type(error).__name__
        log.emit("error", session_id=session.id, error_type=type(error).__name__, message=str(error))
        if runtime:
            result.update(getattr(runtime, "partial_agent_result", {}))
            if hasattr(runtime, "agent_trajectory"):
                write_json(directory / "traj.json", runtime.agent_trajectory)
            memory = dict(runtime.memory)
    finally:
        if env:
            try:
                problem = env.close()
            except Exception as error:
                problem = str(error)
            if problem:
                # Removal follows the saved evidence, so a slow daemon leaves a container for the sweep at the end of
                # the campaign, not an invalid session that a resume would pay for again.
                result["cleanup_error"] = problem
                log.emit("cleanup_error", session_id=session.id, message=problem)
        if config.agent != "scripted":
            # A model call that raised logged no model_result, so its cost is unknown even after priced calls.
            unknown = bool(result.get("error")) and result["steps"] > log.model_results
            budget.finish(allowance, result["cost_usd"], unknown=unknown)
            result["cost_status"] = "unknown" if unknown else "reported"
        result["duration_seconds"] = time.monotonic() - started
        write_json(directory / "memory_end.json", memory)
        write_json(directory / "interventions.json", runtime.journal if runtime else [])
        if runtime and runtime.guard:
            write_json(directory / "guard.json", runtime.guard.summary())
        if engine.enabled:
            # Where plugin strategies left the intervention RNG, so a resume continues the same draws.
            write_json(directory / "rng_state.json", engine.rng.getstate())
        write_json(directory / "result.json", result)
        log.emit("session_end", session_id=session.id, status=result["status"], patch_status=result["patch_status"])
    return memory, result


def session_dir(arm_dir: Path, number: int, session) -> Path:
    return arm_dir / "sessions" / f"{number + 1:03d}-{session.id}"


def read(path: Path):
    return json.loads(path.read_text())


def complete(
    directory: Path, copy: bool = False, ablated: bool = False, repaired: bool = False, transitioned: bool = False
) -> bool:
    """A finished session. A copy is finished once its branch.json, written after the copy, parses, and a session
    that started from ablated or repaired memory once its ablation.json or repair.json, written after the session,
    does. A session of the adaptive arm (`transitioned`) also needs the transition.json of the step before it."""
    try:
        status = read(directory / "result.json")["status"]
        for name in ("branch.json",) * copy + ("ablation.json",) * ablated + ("repair.json",) * repaired:
            read(directory / name)
    except (OSError, ValueError):
        # Missing, or cut short when the campaign was interrupted mid-write.
        return False
    if (
        transitioned
        and read_transition(directory.parent.parent / "transitions" / directory.name[:3] / "transition.json") is None
    ):
        return False
    return status == "complete"


def terminator(line: str) -> str:
    return line[len(line.removesuffix("\n").removesuffix("\r")) :] if line.endswith("\n") else ""


def ablate(memory: dict[str, str], events, journal=()) -> tuple[dict[str, str], dict]:
    """Carried memory without every line that a match of an intervention's marker or copy patterns overlaps,
    matched over the whole file text as the metrics detect content, and the record of what was removed. A removed
    line keeps the text a memory append or prepend was joined to only when the journal of the exposure sessions
    shows the intervention landed on a line the file did not end (or start) there; it keeps its line terminator.
    Removal repeats while a removed line joins its neighbours into a new match. A file left with nothing but
    whitespace is deleted; every other byte is kept. Content the metrics would still find is a setup failure."""
    catalog = intervention_catalog(events)
    kept, files, lines_by_event = {}, {}, dict.fromkeys(catalog, 0)
    joins = defaultdict(list)
    for entry in journal:
        event, earlier = entry["event"], entry["before"] or ""
        if event["method"] != "memory" or not earlier or not event["text"]:
            continue
        if event["operation"] == "append" and not earlier.endswith("\n"):
            first = LINES.match(event["text"]).group(0)
            tail = earlier.rpartition("\n")[2]
            joins[event["path"]].append((tail + first[: len(first) - len(terminator(first))], "", tail))
        elif event["operation"] == "prepend" and not event["text"].endswith("\n"):
            head = LINES.match(earlier).group(0)
            head = head[: len(head) - len(terminator(head))]
            joins[event["path"]].append((event["text"].rpartition("\n")[2], head, head))

    def hits(text: str) -> list[str]:
        return [key for key, entry in catalog.items() if content_found(entry, text)]

    def before(path: str, line: str) -> str:
        """The text the journal shows a memory intervention's first appended or last prepended line joined to."""
        content = line[: len(line) - len(terminator(line))]
        for prefix, suffix, rest in joins[path]:
            if content.startswith(prefix) and content.endswith(suffix) and len(content) >= len(prefix + suffix):
                if rest.strip() and not hits(rest):
                    return rest + terminator(line)
        return ""

    for path, text in sorted(memory.items()):
        removed, trimmed, matched, rest = 0, 0, set(), text
        # Removing a line can join its neighbours into a new match, so passes repeat until nothing matches.
        while True:
            spans = {key: content_spans(entry, rest) for key, entry in catalog.items()}
            remaining, removed_before = [], removed
            for match in LINES.finditer(rest):
                line, (start, end) = match.group(0), match.span()
                found = [key for key, ranges in spans.items() if any(a < end and b > start for a, b in ranges)]
                for key in found:
                    lines_by_event[key] += 1
                removed += bool(found)
                matched.update(found)
                if not found:
                    remaining.append(line)
                elif kept_text := before(path, line):
                    remaining.append(kept_text)
                    trimmed += 1
            rest = "".join(remaining)
            if removed == removed_before:
                break
        if rest.strip() or not removed:
            kept[path] = rest
        if removed:
            files[path] = {
                "removed_lines": removed,
                "kept_merged_lines": trimmed,
                "deleted": path not in kept,
                "interventions": sorted(matched),
            }
    if residue := {path: found for path, text in kept.items() if (found := hits(text))}:
        raise RuntimeError(f"ablation left intervention content the metrics detect: {residue}")
    record = {
        "removed_lines": sum(entry["removed_lines"] for entry in files.values()),
        "removed_files": sorted(path for path, entry in files.items() if entry["deleted"]),
        "files": files,
        "interventions": lines_by_event,
    }
    return kept, record


def code_sha256() -> str:
    """sha256 over the name and bytes of every source file of this package, in sorted order."""
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def check_resume(output: Path, manifest: dict) -> None:
    """Refuse to resume unless the config and the frozen inputs are the campaign's own."""
    if not (output / "manifest.json").exists():
        raise ValueError(f"no campaign to resume in {output}")
    previous = read(output / "manifest.json")
    keys = ("config_sha256", "tasks", "memory_instructions_sha256", "versions", "plugins")
    # Campaigns started before code hashing record it on their first resume instead.
    keys += ("code_sha256",) if "code_sha256" in previous else ()
    # The adaptive program is part of the experiment: its argv, files and limits stay what the campaign started with.
    keys += ("adaptive",) if "adaptive" in previous else ()
    changed = [key for key in keys if previous.get(key) != manifest.get(key)]
    if {k: v["id"] for k, v in previous["images"].items()} != {k: v["id"] for k, v in manifest["images"].items()}:
        changed.append("images")
    if changed:
        raise ValueError(f"cannot resume: {', '.join(changed)} changed since the campaign started")


def campaign_tasks(config) -> dict:
    tasks = {t.task_id: load_task(config.dataset_root, t.task_id) for t in config.targets}
    for session in config.sessions:
        if session.task_id and session.task_id not in tasks:
            tasks[session.task_id] = load_task(config.dataset_root, session.task_id)
    return tasks


def campaign_identities(config, tasks: dict) -> dict:
    """The local image of every task, a target's own image where it names one; a missing image fails here."""
    overrides = {t.task_id: t.image for t in config.targets if t.image}
    return {key: image_identity(overrides.get(key, task.image)) for key, task in tasks.items()}


PHASES = ("session_start", "before_action", "after_observation", "session_end")


def written_before(events, index: int, session_id: str, order: dict) -> str | None:
    """An intervention that writes, or may write, the path of `events[index]` before it fires in `session_id`, this
    one in an earlier session included. A session's repository starts from the image, so for a file only the same
    session counts; memory carries every earlier session's writes. Action phases repeat every step, so for an event
    in one any other writer of the session outside session_end may come first."""
    event = events[index]
    repeats = event.phase in {"before_action", "after_observation"}
    position = (order[session_id], PHASES.index(event.phase), index)
    for number, other in enumerate(events):
        if other.method != event.method or other.path != event.path:
            continue
        for session in other.sessions:
            if event.method == "memory" or session == session_id:
                if (order[session], PHASES.index(other.phase), number) < position or (
                    repeats and session == session_id and number != index and other.phase != "session_end"
                ):
                    return other.id
    return None


def dry_check(config, identities: dict | None = None, env_factory=DockerEnvironment) -> list[dict]:
    """Every replace intervention's old_text, per session it fires in and task, against what it will replace,
    without a model: the seed or user memory the config gives a memory path, or the file a task image ships at a
    repository path, read from one container per task. Command output, a memory path without seed content outside
    the first session's start, a path an earlier intervention writes, and a session after the first of an event
    that fires once are uncheckable; earlier writes are not reproduced. A match count other than one is missing at
    session_start, where the runtime raises; in a later phase the agent may have changed the text, so it is
    uncheckable, as the runtime skips and logs it."""
    order = {session.id: number for number, session in enumerate(config.sessions)}
    seed = config.memory.initial()
    items = {}
    for variant, events in config.variants.items():
        for index, event in enumerate(events):
            if event.operation != "replace" or event.old_text is None:
                continue
            for rank, session_id in enumerate(sorted(event.sessions, key=order.get)):
                session = config.sessions[order[session_id]]
                for target in config.targets:
                    task = session.task_id or target.task_id
                    item = {
                        "variant": variant,
                        "intervention": event.id,
                        "method": event.method,
                        "session": session_id,
                        "task": task,
                        "path": event.path,
                        "status": "uncheckable",
                        "reason": None,
                    }
                    first = order[session_id] == 0 and event.phase == "session_start"
                    if event.method == "output":
                        item["reason"] = "command output is unknown before the run"
                    elif rank and event.max_fires == 1:
                        item["reason"] = "it fires once, in the first session where it applies"
                    elif writer := written_before(events, index, session_id, order):
                        item["reason"] = f"{writer} writes {event.path} earlier"
                    elif event.method == "memory" and event.path not in seed and not first:
                        item["reason"] = "no seed or user memory at this path; the agent may write it"
                    else:
                        item["status"] = None
                    items.setdefault((variant, event.id, session_id, task), (item, event))
    needed = defaultdict(set)
    for item, _ in items.values():
        if item["status"] is None and item["method"] == "file":
            needed[item["task"]].add(item["path"])
    shipped, unreadable = {}, {}
    if needed:
        identities = identities or campaign_identities(config, campaign_tasks(config))
    for task, paths in sorted(needed.items()):
        env = env_factory(identities[task]["id"], 60)
        try:
            for path in sorted(paths):
                try:
                    shipped[(task, path)] = env.read(path)
                except RuntimeError as error:
                    # Such as a symlinked CLAUDE.md: the runtime could not plant there either.
                    unreadable[(task, path)] = str(error)
        finally:
            env.close()
    for item, event in items.values():
        if (item["task"], item["path"]) in unreadable:
            item["status"] = "missing"
            item["reason"] = f"the image file cannot be edited: {unreadable[(item['task'], item['path'])]}"
        elif item["status"] is None:
            text = shipped[(item["task"], item["path"])] if item["method"] == "file" else seed.get(item["path"])
            count = (text or "").count(event.old_text)
            item["status"] = "ok" if count == 1 else "missing"
            if count != 1 and event.phase != "session_start":
                item["status"] = "uncheckable"
                item["reason"] = f"the agent may change or create the text before {event.phase}"
            elif text is None:
                item["reason"] = (
                    "the image ships no file at this path"
                    if item["method"] == "file"
                    else "the first session starts with no memory at this path"
                )
            elif count != 1:
                item["reason"] = f"old_text matches {count} times; replace needs exactly one match"
    return [item for item, _ in items.values()]


def run_campaign(
    config, output: Path, registry, *, env_factory=DockerEnvironment, identities=None, resume=False, grader=None
) -> dict:
    """Run a new campaign in `output`, or resume one: complete sessions are kept, and each arm re-runs from its
    first incomplete session. Earlier attempts move to superseded/<UTC time>/ under their own relative path.
    `grader` grades a finished session directory in place for the adaptive hook; it defaults to the DeepSWE
    verifier `grade` uses."""
    validate(config, registry)
    if config.guard and (config.guard.for_user != "off" or config.guard.test_claims != "off"):
        if not os.environ.get("OPENROUTER_API_KEY"):
            raise ValueError("the guard's judge (rules 3 and 4) needs OPENROUTER_API_KEY")
    # Preflight every image and every replace intervention before starting a session or making a paid request.
    tasks = campaign_tasks(config)
    projects = {}
    for key, task in tasks.items():
        try:
            projects[key] = project_key(task.repository)
        except ValueError as error:
            raise ValueError(f"task {key}: {error}") from None
    if any(PROJECT in path for path in config.memory.seed):
        for target in config.targets:
            chain = sorted({projects[session.task_id or target.task_id] for session in config.sessions})
            if len(chain) > 1:
                # Carried memory would hold the seed only in the first repository's folder, the reset arms in the
                # probe's: the arms would no longer start from the same memory.
                raise ValueError(
                    f"memory.seed names {PROJECT}, but the chain of {target.task_id} spans {', '.join(chain)}; "
                    "use concrete seed paths or a chain in one repository"
                )
    identities = identities or campaign_identities(config, tasks)
    for item in dry_check(config, identities, env_factory):
        if item["status"] == "missing":
            raise ValueError(
                f"dry check: {item['variant']}/{item['intervention']} in session {item['session']} of task "
                f"{item['task']} at {item['path'] or 'command output'}: {item['reason']}"
            )
    output = output.resolve()
    frozen_config = config.model_dump(mode="json")
    instructions_snapshot = output / "memory-instructions.md"
    if resume and config.memory.instructions_file:
        # Resumed sessions read the frozen copy, so it, not the source file, must match the manifest.
        memory_instructions = instructions_snapshot.read_text() if instructions_snapshot.exists() else None
    else:
        memory_instructions = instructions(config.memory)
    manifest = {
        "schema_version": "1.0",
        "config": frozen_config,
        "config_sha256": fingerprint(frozen_config),
        "dataset": provenance(config.dataset_root),
        "images": identities,
        "plugins": registry.plugins,
        "python": platform.python_version(),
        "versions": {
            "sereno": version("sereno"),
            "mini-swe-agent": version("mini-swe-agent") if config.agent in MINI_SWE_AGENTS else None,
            **({"anthropic": version("anthropic")} if config.agent == "anthropic" else {}),
        },
        "code_sha256": code_sha256(),
        "model_config": config.model_config_file.read_text() if config.model_config_file else None,
        "memory_instructions_sha256": fingerprint(memory_instructions) if memory_instructions is not None else None,
        "payload_sha256": {
            name: {e.id: fingerprint(e.text) for e in events} for name, events in config.variants.items()
        },
        "tasks": {
            key: {
                "base_commit": task.base_commit,
                "instruction_sha256": fingerprint(task.instruction),
                "agent_timeout_seconds": task.agent_timeout_seconds,
                "memory_project": projects[key],
            }
            for key, task in tasks.items()
        },
        **({"adaptive": adaptive_manifest(config.adaptive)} if config.adaptive else {}),
    }
    if config.adaptive and config.adaptive.grade_before_step and grader is None:
        grader = default_grader(config.dataset_root)
    if resume:
        check_resume(output, manifest)
        previous = read(output / "manifest.json")
        if "code_sha256" not in previous:
            previous |= {
                "code_sha256": manifest["code_sha256"],
                "code_sha256_recorded_on_resume": datetime.now(UTC).isoformat(),
            }
            write_json(output / "manifest.json", previous)
    else:
        output.mkdir(parents=True, exist_ok=False)
        (output / "cases").mkdir()
    # All sessions, resumed ones too, use the model config and memory instructions frozen at the start.
    if config.model_config_file:
        model_snapshot = output / "model-config.yaml"
        if not resume:
            model_snapshot.write_text(manifest["model_config"])
        config = config.model_copy(update={"model_config_file": model_snapshot})
    if config.memory.instructions_file:
        if not resume:
            instructions_snapshot.write_text(memory_instructions)
        config = config.model_copy(
            update={"memory": config.memory.model_copy(update={"instructions_file": instructions_snapshot})}
        )
    if config.agent in MINI_SWE_AGENTS:
        # mini.yaml merged with the model config, read once: resumed sessions never re-read site-packages.
        agent_snapshot = output / "mini-swe-config.yaml"
        previous = read(output / "manifest.json") if resume else {}
        if "agent_config_sha256" in previous:
            if fingerprint(agent_snapshot.read_text()) != previous["agent_config_sha256"]:
                raise ValueError("cannot resume: the frozen mini-swe configuration changed")
        else:
            agent_snapshot.write_text(yaml.safe_dump(mini_swe_config(config.model_config_file, config.agent)))
            if resume:
                # A campaign started before agent configurations were frozen; its versions matched above.
                previous |= {
                    "agent_config_sha256": fingerprint(agent_snapshot.read_text()),
                    "agent_config_frozen_on_resume": datetime.now(UTC).isoformat(),
                }
                write_json(output / "manifest.json", previous)
        manifest["agent_config_sha256"] = fingerprint(agent_snapshot.read_text())
        config._agent_config_file = agent_snapshot
    if not resume:
        write_json(output / "manifest.json", manifest)
    # The cost limit applies to the spending of this run; a resume starts a new budget.
    budget = Budget(config.campaign_cost_limit_usd)
    # The adaptive arm's stop halts the whole campaign, so neither side of the comparison runs on alone.
    streak = FailureStreak(config.adaptive.stop_after_failures, on_stop=budget.halt) if config.adaptive else None
    sessions = config.sessions
    started = datetime.now(UTC)
    superseded = output / "superseded" / started.strftime("%Y%m%dT%H%M%S.%fZ")
    written = []

    def redo(directory: Path) -> Path:
        """Clear the way for a session this run writes; an earlier attempt moves under superseded/."""
        if directory.exists():
            target = superseded / directory.relative_to(output)
            target.parent.mkdir(parents=True, exist_ok=True)
            directory.rename(target)
        written.append(str(directory.relative_to(output)))
        return directory

    exposures = sum(session.exposure for session in sessions)
    repairs = [s.id for s in sessions].index(config.repair.session) if config.repair else None

    def current(copy: Path) -> bool:
        """A copy of the session its origin now holds; a run interrupted after re-running an origin session leaves
        the copies of the replaced attempt behind. Older campaigns name the origin shared_exposure and older
        results carry no run_id."""
        try:
            branch = read(copy / "branch.json")
            origin = output / (branch.get("shared_from") or branch["shared_exposure"])
            return read(copy / "result.json").get("run_id") == read(origin / "result.json").get("run_id")
        except (OSError, ValueError, KeyError):
            return False

    def first_incomplete(
        arm_dir: Path, copied=lambda session: False, ablated: bool = False, transitioned: bool = False
    ) -> int:
        return next(
            (
                n
                for n, s in enumerate(sessions)
                if not complete(
                    directory := session_dir(arm_dir, n, s),
                    copied(s),
                    ablated and n == exposures,
                    n == repairs,
                    transitioned,
                )
                or (copied(s) and not current(directory))
            ),
            len(sessions),
        )

    def engine_for(events, arm_dir: Path, start: int, **options) -> InterventionEngine:
        """An arm's intervention engine, with the fires of the sessions kept before `start` counted and its RNG
        where the last kept session left it."""
        restored = InterventionEngine(events, registry, **options)
        for number, session in enumerate(sessions[:start]):
            for entry in read(session_dir(arm_dir, number, session) / "interventions.json"):
                restored.fires[entry["event"]["id"]] += 1
                restored.session_fires[(entry["event"]["id"], session.id)] += 1
        last = session_dir(arm_dir, start - 1, sessions[start - 1]) if start else None
        # A copied session carries its origin arm's RNG; this arm's own RNG had not drawn yet.
        if last and (last / "rng_state.json").exists() and not (last / "branch.json").exists():
            version, state, gauss = read(last / "rng_state.json")
            restored.rng.setstate((version, tuple(state), gauss))
        return restored

    def run(target, engine):
        """A step that runs the session in a fresh container. A chain's first session starts its memory's lineage, and
        so does a `reset` probe, which starts from the configured memory."""

        def step(number, session, directory, memory, opening=(), reset=False):
            task_id = session.task_id or target.task_id
            try:
                return run_session(
                    config,
                    session,
                    tasks[task_id],
                    identities[task_id],
                    memory,
                    engine,
                    registry.agents[config.agent],
                    directory,
                    budget,
                    env_factory,
                    opening,
                    lineage_start=number == 0 or reset,
                )
            except NotStarted:
                raise
            except Exception as error:
                directory.mkdir(parents=True, exist_ok=True)
                result = {
                    "session_id": session.id,
                    "task_id": task_id,
                    "status": "invalid",
                    "exposure_phase": session.exposure,
                    "error": str(error),
                    "markers": {},
                    "workspace": {},
                }
                write_json(directory / "result.json", result)
                write_json(directory / "memory_end.json", memory)
                return memory, result

        return step

    def share(source_arm: Path, origin_arm: str):
        """A step that copies the session another arm ran; branch.json, written last, names its origin."""

        def step(number, session, directory, memory):
            source = session_dir(source_arm, number, session)
            if not (source / "result.json").exists():
                raise NotStarted(f"{source.relative_to(output)} was not started")
            shutil.copytree(source, directory)
            write_atomic(
                directory / "branch.json",
                {"shared_from": str(source.relative_to(output)), "origin_arm": origin_arm},
            )
            return read(directory / "memory_end.json"), read(directory / "result.json")

        return step

    def stop(arm_dir: Path, start: int) -> None:
        """Leave sessions from `start` on missing; earlier attempts of them descend from replaced sessions."""
        for number, session in enumerate(sessions[start:], start=start):
            redo(session_dir(arm_dir, number, session))

    def exposure_journal(arm_dir: Path) -> list[dict]:
        return [
            entry
            for n, s in enumerate(sessions[:exposures])
            for entry in read(session_dir(arm_dir, n, s) / "interventions.json")
        ]

    def repair(memory: dict[str, str]) -> tuple[dict[str, str], dict]:
        """The user's update to their file, on the host; it is the user's change, so the session starts from it and no
        memory event records it."""
        before = memory.get(USER)
        memory = memory | {USER: config.repair.apply(before or "")}
        return memory, {
            **config.repair.model_dump(),
            "user_file_before_sha256": None if before is None else fingerprint(before),
            "user_file_after_sha256": fingerprint(memory[USER]),
        }

    def advance(arm_dir: Path, target, step, start: int, transition=None) -> None:
        """Run an arm's sessions from `start` on; each starts from the memory the previous one ended with, and the
        repair session from that memory as the user repaired it. A `transition` (the adaptive hook) changes that
        memory first, on the host, and hands the session the events that record it."""
        if start == len(sessions):
            return
        previous = session_dir(arm_dir, start - 1, sessions[start - 1]) if start else None
        memory = read(previous / "memory_end.json") if previous else config.memory.initial()
        for number, session in enumerate(sessions[start:], start=start):
            record, opening = None, ()
            if number == repairs:
                memory, record = repair(memory)
            if transition is not None:
                try:
                    memory, opening = transition(number, memory)
                except AdaptiveStop:
                    # Like an exhausted budget: the sessions from here on stay missing and a resume runs them.
                    stop(arm_dir, number)
                    break
            directory = redo(session_dir(arm_dir, number, session))
            try:
                memory, result = step(number, session, directory, memory, *([opening] if transition else []))
            except NotStarted:
                stop(arm_dir, number + 1)
                break
            if record is not None:
                write_atomic(directory / "repair.json", record)
            if result["status"] != "complete":
                # The remaining planned sessions are explicit missing entries, not silently successful probes.
                for skipped_number, skipped in enumerate(sessions[number + 1 :], start=number + 1):
                    skipped_dir = redo(session_dir(arm_dir, skipped_number, skipped))
                    skipped_dir.mkdir()
                    write_json(
                        skipped_dir / "result.json",
                        {
                            "session_id": skipped.id,
                            "task_id": skipped.task_id or target.task_id,
                            "status": "invalid",
                            "exposure_phase": skipped.exposure,
                            "error": "preceding session invalid",
                            "markers": {},
                            "workspace": {},
                        },
                    )
                    write_json(skipped_dir / "memory_end.json", memory)
                break

    def branch(arm_dir: Path, target, source: Path, source_arm: str, source_start: int, events, ablated, **options):
        """Run an arm that shares the exposure sessions of `source`, then starts its first probe from the initial
        memory or, `ablated`, from the carried memory without the interventions' content. It re-runs from its own
        first incomplete session, or from the first copy whose source this run re-runs; returns where it began."""
        start = min(
            first_incomplete(arm_dir, lambda session: session.exposure, ablated),
            source_start if source_start < exposures else len(sessions),
        )
        exposure = share(source, source_arm)
        probe = run(target, engine_for(events, arm_dir, start, **options))

        def step(number, session, directory, memory):
            if session.exposure:
                return exposure(number, session, directory, memory)
            if number != exposures:
                return probe(number, session, directory, memory)
            if not ablated:
                return probe(number, session, directory, config.memory.initial(), reset=True)
            memory, record = ablate(memory, events, exposure_journal(arm_dir))
            outcome = probe(number, session, directory, memory)
            write_atomic(directory / "ablation.json", record)
            return outcome

        advance(arm_dir, target, step, start)
        return start

    def origin(target, repeat, arm="clean") -> Path:
        return output / arm / f"{target.task_id}--r{repeat + 1:03d}"

    def run_origin(item):
        target, repeat = item
        # Without interventions the clean arms are identical across variants: one run per target and repeat.
        arm_dir = origin(target, repeat)
        start = first_incomplete(arm_dir)
        advance(arm_dir, target, run(target, engine_for([], arm_dir, start, enabled=False)), start)
        starts = {"clean": start}
        if "clean_reset" in config.arms:
            reset_dir = origin(target, repeat, "clean_reset")
            starts["clean_reset"] = branch(reset_dir, target, arm_dir, "clean", start, [], False, enabled=False)
        return starts

    def run_case(item):
        target, (variant, events), repeat = item
        case_id = f"{target.task_id}--{variant}--r{repeat + 1:03d}"
        case_dir = output / "cases" / case_id
        arms = case_dir / "arms"
        arms.mkdir(parents=True, exist_ok=True)
        if not (case_dir / "case.json").exists():
            write_json(
                case_dir / "case.json",
                {"target": target.task_id, "variant": variant, "repeat": repeat, "seed": config.seed + repeat},
            )
        if "attack_adaptive" in config.arms:
            # No interventions: the arm differs from the clean one only by what the program plants between sessions.
            adaptive_dir = arms / "attack_adaptive"
            start = first_incomplete(adaptive_dir, transitioned=True)
            hook = AdaptiveHook(
                config,
                output,
                adaptive_dir,
                case=case_id,
                repeat=repeat,
                target=target.task_id,
                projects=projects,
                locate=lambda number: session_dir(adaptive_dir, number, sessions[number]),
                grader=grader,
                stamp=superseded.name,
                streak=streak,
            )
            engine = engine_for([], adaptive_dir, start, enabled=False)
            advance(adaptive_dir, target, run(target, engine), start, hook.before)
        carry_start, seed = len(sessions), config.seed + repeat
        if "attack_carry" in config.arms:
            carry_start = first_incomplete(arms / "attack_carry")
            notices = [n for n in config.notices if variant in n.variants]
            carry = engine_for(events, arms / "attack_carry", carry_start, seed=seed, notices=notices)
            advance(arms / "attack_carry", target, run(target, carry), carry_start)
        # Reset and ablation share carry's exact exposure artifacts rather than resampling them, so re-running a
        # carry exposure session replaces their copy of it and every session after it.
        for arm in ("attack_reset", "attack_ablate"):
            if arm in config.arms:
                ablated = arm == "attack_ablate"
                branch(
                    arms / arm, target, arms / "attack_carry", "attack_carry", carry_start, events, ablated, seed=seed
                )
        # The clean copies come last: the case's attack arms run beside its origin, and the copies wait for it.
        for arm in ("clean", "clean_reset"):
            if arm not in config.arms:
                continue
            origin_start = origins[(target.task_id, repeat)].result()[arm]
            # A copy follows its origin: re-running an origin session replaces its copy and every later one.
            start = min(first_incomplete(arms / arm, lambda session: True), origin_start)
            for number, session in enumerate(sessions[start:], start=start):
                # A clean_reset exposure copy names the clean session it shares, so it counts and grades once.
                source = arm if not session.exposure else "clean"
                try:
                    share(origin(target, repeat, source), source)(
                        number, session, redo(session_dir(arms / arm, number, session)), None
                    )
                except NotStarted:
                    stop(arms / arm, number + 1)
                    break
        return case_id

    origins, jobs = {}, {}
    try:
        with ThreadPoolExecutor(max_workers=config.workers) as pool:
            # Clean and attack chains alternate: each origin is followed at once by its target's and repeat's cases,
            # in variant order, so a run cut short and the hours between chains fall on both sides alike. The queue is
            # first in, first out, so an origin has started before any case that waits for it.
            for planned in config.targets:
                for repeat in range(config.repeats):
                    if "clean" in config.arms:
                        origins[(planned.task_id, repeat)] = pool.submit(run_origin, (planned, repeat))
                    for variant in config.variants.items():
                        jobs[(planned.task_id, variant[0], repeat)] = pool.submit(run_case, (planned, variant, repeat))
            try:
                done, _ = wait([*origins.values(), *jobs.values()], return_when=FIRST_EXCEPTION)
            except BaseException:
                # An interrupt of the run itself, such as Ctrl-C: the jobs not yet started are dropped and the running
                # ones finish before the sweep; a resume runs the rest.
                pool.shutdown(wait=False, cancel_futures=True)
                raise
            if errors := [error for future in done if (error := future.exception()) is not None]:
                # Jobs not yet started are dropped; a resume runs them.
                pool.shutdown(wait=True, cancel_futures=True)
                raise errors[0]
            # The case list keeps the order of targets, variants and repeats.
            cases = [
                jobs[(planned.task_id, name, repeat)].result()
                for planned in config.targets
                for name in config.variants
                for repeat in range(config.repeats)
            ]
    finally:
        # Also when a session raised out of the pool: no container this run failed to remove is left behind unretried.
        unremoved = sweep()
    spending = {
        "cost_usd": budget.spent,
        "cost_limit_usd": budget.limit,
        "overshoot_usd": max(0, budget.spent - budget.limit),
        "unknown_cost": budget.unknown_cost,
        # Containers still present after the sweep, with the last reason; empty when every removal succeeded.
        "unremoved_containers": unremoved,
        # Why the adaptive arm stopped early and the failed steps, or null; absent without the arm.
        **({"adaptive_stop": streak.stopped} if streak else {}),
    }
    if resume:
        # An interrupted campaign has no campaign.json yet; its own spending then goes unrecorded here.
        campaign = read(output / "campaign.json") if (output / "campaign.json").exists() else {"cases": cases}
        record = {
            "time": started.isoformat(),
            "sessions": sorted(path for path in written if (output / path).exists()),
            "superseded": str(superseded.relative_to(output)) if superseded.exists() else None,
            **spending,
        }
        campaign["resumes"] = [*campaign.get("resumes", []), record]
    else:
        campaign = {"cases": cases, **spending}
    write_json(output / "campaign.json", campaign)
    outcome = report(output, registry)
    return outcome | ({"adaptive_stop": streak.stopped} if streak else {})


def export_submission(root: Path, case: str, arm: str, session: str, output: Path) -> Path:
    if any("/" in value or value in {".", ".."} for value in (case, arm, session)):
        raise ValueError("case, arm and session must be simple identifiers")
    sessions = root / "cases" / case / "arms" / arm / "sessions"
    candidates = [p for p in sessions.iterdir() if p.name.partition("-")[2] == session]
    if len(candidates) != 1:
        raise ValueError("session must identify exactly one artifact directory")
    directory = candidates[0]
    result = json.loads((directory / "result.json").read_text())
    if result["status"] != "complete" or result.get("patch_status") != "ready":
        raise ValueError("cannot export an invalid or ambiguous submission")
    destination = output / result["task_id"]
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(directory / "model.patch", destination / "model.patch")
    write_json(
        destination / "submission.json",
        {
            "task_id": result["task_id"],
            "image_id": result["image"]["id"],
            "base_commit": result["base_commit"],
            "source": str(directory.resolve()),
            "patch_sha256": hashlib.sha256((destination / "model.patch").read_bytes()).hexdigest(),
        },
    )
    return destination
