"""Run isolated clean, carry, reset and ablation arms and retain reproducible, grade-ready artifacts."""

import hashlib
import itertools
import json
import platform
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from sereno.context_eval.agents import action_timeout, mini_swe_config
from sereno.context_eval.config import fingerprint, validate
from sereno.context_eval.dataset import image_identity, load_task, provenance
from sereno.context_eval.engine import (
    EventLog,
    InterventionEngine,
    Runtime,
    intervention_catalog,
    message_text,
    separate_patch,
    write_json,
)
from sereno.context_eval.environment import DockerEnvironment
from sereno.context_eval.memory import FileMemory, MemoryViolation, agent_violation, instructions
from sereno.context_eval.metrics import report
from sereno.context_eval.schema import AgentOutcome

COMPLETE = {"Submitted", "LimitsExceeded", "TimeExceeded", "RepeatedFormatError", "MemoryViolation", "script_complete"}
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
        self.unknown_cost = False
        self.condition = threading.Condition()

    def acquire(self, amount: float) -> float:
        with self.condition:
            while True:
                if self.unknown_cost:
                    raise NotStarted("campaign cost accounting unavailable after a failed model call")
                # A small tolerance keeps float sums such as 0.3 - 0.1 from refusing an exact fit.
                if self.limit - self.spent - self.reserved >= amount - 1e-9:
                    self.reserved += amount
                    return amount
                if not self.reserved:
                    raise NotStarted("campaign cost budget exhausted")
                # A running session may finish under its cap and leave enough for this one.
                self.condition.wait()

    def finish(self, reserved: float, spent: float, *, unknown=False):
        with self.condition:
            self.reserved -= reserved
            self.spent += spent
            self.unknown_cost |= unknown
            self.condition.notify_all()


def run_session(config, session, task, identity, memory, engine, adapter, directory, budget, env_factory):
    directory.mkdir(parents=True, exist_ok=False)
    try:
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
        env = env_factory(identity["id"], wall)
        # The runner's own commands, such as patch collection, run under the agent's action limit too.
        env.action_timeout = result["action_timeout_seconds"] = action_timeout(config)
        result["has_timeout"] = getattr(env, "has_timeout", None)
        if env.execute("git -c safe.directory=/app rev-parse HEAD")["output"].strip() != task.base_commit:
            raise ValueError("image HEAD does not match the task base commit")
        mem = FileMemory()
        if config.memory.enabled:
            if env.execute("mkdir -p /memories")["returncode"]:
                raise RuntimeError("cannot initialize memory directory")
            if env.snapshot_memory(config.memory.max_files, config.memory.max_bytes):
                raise ValueError("task image must start with an empty memory directory")
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
        runtime = Runtime(env, engine, log, session, config.memory, memory, config.checks)
        log.emit(
            "session_start",
            session_id=session.id,
            task_id=task.id,
            image=identity,
            action_timeout_seconds=env.action_timeout,
            has_timeout=result["has_timeout"],
        )
        engine.apply(runtime, "session_start")
        instruction = session.instruction or task.instruction
        context = mem.context(runtime.memory) if config.memory.enabled else ""
        runtime.initial_memory_context = context
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
        for check in config.checks:
            if check.source == "workspace" and (not check.sessions or session.id in check.sessions):
                result["workspace"][check.path] = env.read(check.path) or ""
        (directory / "raw.patch").write_bytes(env.collect_patch(task.base_commit))
        try:
            patch = separate_patch(env, runtime.journal, task.base_commit)
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
                env.close()
            except Exception as error:
                result["status"] = "invalid"
                result["cleanup_error"] = str(error)
        if config.agent != "scripted":
            unknown = bool(result.get("error") and result["steps"] > 0 and result["cost_usd"] == 0)
            budget.finish(allowance, result["cost_usd"], unknown=unknown)
            result["cost_status"] = "unknown" if unknown else "reported"
        result["duration_seconds"] = time.monotonic() - started
        write_json(directory / "memory_end.json", memory)
        write_json(directory / "interventions.json", runtime.journal if runtime else [])
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


def complete(directory: Path, copy: bool = False, ablated: bool = False) -> bool:
    """A finished session. A copy is finished once its branch.json, written after the copy, parses, and a session
    that started from ablated memory once its ablation.json, written after the session, does."""
    try:
        status = read(directory / "result.json")["status"]
        for name in ("branch.json",) * copy + ("ablation.json",) * ablated:
            read(directory / name)
    except (OSError, ValueError):
        # Missing, or cut short when the campaign was interrupted mid-write.
        return False
    return status == "complete"


def ablate(memory: dict[str, str], events) -> tuple[dict[str, str], dict]:
    """Carried memory without the lines that hold an intervention's marker or match one of its copy patterns, and
    the record of what was removed. A memory append or prepend lands on the line of a file that does not end (or
    start) a line there; such a merged line keeps the text that was there before. A file left with nothing but
    whitespace is deleted; every other byte is kept."""
    kept, files, lines_by_event = {}, {}, dict.fromkeys((event.id for event in events), 0)

    def hits(line: str) -> list[str]:
        return [e.id for e in events if (e.marker and e.marker in line) or e.copy_match(line) is not None]

    def before(path: str, line: str) -> str:
        """The text a memory intervention's first appended or last prepended line was joined to."""
        for event in events:
            if event.method != "memory" or event.path != path or event.operation == "replace":
                continue
            parts = event.text.splitlines(keepends=True)
            if event.operation == "append" and line.endswith(parts[0]):
                rest = line.removesuffix(parts[0])
            elif event.operation == "prepend" and line.startswith(parts[-1]):
                rest = line.removeprefix(parts[-1])
            else:
                continue
            if rest.strip() and not hits(rest):
                return rest
        return ""

    for path, text in sorted(memory.items()):
        remaining, removed, trimmed, matched = [], 0, 0, set()
        for line in text.splitlines(keepends=True):
            found = hits(line)
            for key in found:
                lines_by_event[key] += 1
            removed += bool(found)
            matched.update(found)
            if not found:
                remaining.append(line)
            elif rest := before(path, line):
                remaining.append(rest)
                trimmed += 1
        rest = "".join(remaining)
        if rest.strip() or not removed:
            kept[path] = rest
        if removed:
            files[path] = {
                "removed_lines": removed,
                "kept_merged_lines": trimmed,
                "deleted": path not in kept,
                "interventions": sorted(matched),
            }
    record = {
        "removed_lines": sum(entry["removed_lines"] for entry in files.values()),
        "removed_files": sorted(path for path, entry in files.items() if entry["deleted"]),
        "files": files,
        "interventions": lines_by_event,
    }
    return kept, record


def write_atomic(path: Path, value) -> None:
    """Write JSON so an interruption leaves the old file or none, never a truncated one."""
    partial = path.with_name(f".{path.name}.partial")
    write_json(partial, value)
    partial.replace(path)


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
    changed = [key for key in keys if previous.get(key) != manifest[key]]
    if {k: v["id"] for k, v in previous["images"].items()} != {k: v["id"] for k, v in manifest["images"].items()}:
        changed.append("images")
    if changed:
        raise ValueError(f"cannot resume: {', '.join(changed)} changed since the campaign started")


def run_campaign(
    config, output: Path, registry, *, env_factory=DockerEnvironment, identities=None, resume=False
) -> dict:
    """Run a new campaign in `output`, or resume one: complete sessions are kept, and each arm re-runs from its
    first incomplete session. Earlier attempts move to superseded/<UTC time>/ under their own relative path."""
    validate(config, registry)
    # Preflight every image before starting a container or making a paid request.
    tasks = {t.task_id: load_task(config.dataset_root, t.task_id) for t in config.targets}
    for session in config.sessions:
        if session.task_id and session.task_id not in tasks:
            tasks[session.task_id] = load_task(config.dataset_root, session.task_id)
    overrides = {t.task_id: t.image for t in config.targets if t.image}
    identities = identities or {key: image_identity(overrides.get(key, task.image)) for key, task in tasks.items()}
    output = output.resolve()
    frozen_config = config.model_dump(mode="json")
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
            "mini-swe-agent": version("mini-swe-agent") if config.agent == "mini-swe" else None,
        },
        "code_sha256": code_sha256(),
        "model_config": config.model_config_file.read_text() if config.model_config_file else None,
        "memory_instructions_sha256": fingerprint(memory_instructions) if config.memory.enabled else None,
        "payload_sha256": {
            name: {e.id: fingerprint(e.text) for e in events} for name, events in config.variants.items()
        },
        "tasks": {
            key: {
                "base_commit": task.base_commit,
                "instruction_sha256": fingerprint(task.instruction),
                "agent_timeout_seconds": task.agent_timeout_seconds,
            }
            for key, task in tasks.items()
        },
    }
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
        instructions_snapshot = output / "memory-instructions.md"
        if not resume:
            instructions_snapshot.write_text(memory_instructions)
        config = config.model_copy(
            update={"memory": config.memory.model_copy(update={"instructions_file": instructions_snapshot})}
        )
    if config.agent == "mini-swe":
        import yaml

        # mini.yaml merged with the model config, read once: resumed sessions never re-read site-packages.
        agent_snapshot = output / "mini-swe-config.yaml"
        previous = read(output / "manifest.json") if resume else {}
        if "agent_config_sha256" in previous:
            if fingerprint(agent_snapshot.read_text()) != previous["agent_config_sha256"]:
                raise ValueError("cannot resume: the frozen mini-swe configuration changed")
        else:
            agent_snapshot.write_text(yaml.safe_dump(mini_swe_config(config.model_config_file)))
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

    def current(copy: Path) -> bool:
        """A copy of the session its origin now holds; a run interrupted after re-running an origin session leaves
        the copies of the replaced attempt behind."""
        try:
            origin = output / read(copy / "branch.json")["shared_from"]
            return read(copy / "result.json")["run_id"] == read(origin / "result.json")["run_id"]
        except (OSError, ValueError, KeyError):
            return False

    def first_incomplete(arm_dir: Path, copied=lambda session: False, ablated: bool = False) -> int:
        return next(
            (
                n
                for n, s in enumerate(sessions)
                if not complete(directory := session_dir(arm_dir, n, s), copied(s), ablated and n == exposures)
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
        """A step that runs the session in a fresh container."""

        def step(number, session, directory, memory):
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

    def advance(arm_dir: Path, target, step, start: int) -> None:
        """Run an arm's sessions from `start` on; each starts from the memory the previous one ended with."""
        if start == len(sessions):
            return
        previous = session_dir(arm_dir, start - 1, sessions[start - 1]) if start else None
        memory = read(previous / "memory_end.json") if previous else config.memory.initial()
        for number, session in enumerate(sessions[start:], start=start):
            if not config.memory.enabled:
                memory = {}
            try:
                memory, result = step(number, session, redo(session_dir(arm_dir, number, session)), memory)
            except NotStarted:
                stop(arm_dir, number + 1)
                break
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
                return probe(number, session, directory, config.memory.initial())
            memory, record = ablate(memory, events)
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
        return (target.task_id, repeat), starts

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
        for arm in ("clean", "clean_reset"):
            if arm not in config.arms:
                continue
            # A copy follows its origin: re-running an origin session replaces its copy and every later one.
            start = min(
                first_incomplete(arms / arm, lambda session: True), origin_starts[(target.task_id, repeat)][arm]
            )
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
        carry_start, seed = len(sessions), config.seed + repeat
        if "attack_carry" in config.arms:
            carry_start = first_incomplete(arms / "attack_carry")
            carry = engine_for(events, arms / "attack_carry", carry_start, seed=seed)
            advance(arms / "attack_carry", target, run(target, carry), carry_start)
        # Reset and ablation share carry's exact exposure artifacts rather than resampling them, so re-running a
        # carry exposure session replaces their copy of it and every session after it.
        for arm in ("attack_reset", "attack_ablate"):
            if arm in config.arms:
                ablated = arm == "attack_ablate"
                branch(
                    arms / arm, target, arms / "attack_carry", "attack_carry", carry_start, events, ablated, seed=seed
                )
        return case_id

    with ThreadPoolExecutor(max_workers=config.workers) as pool:
        origin_starts = {}
        if "clean" in config.arms:
            origin_starts = dict(pool.map(run_origin, itertools.product(config.targets, range(config.repeats))))
        cases = list(
            pool.map(run_case, itertools.product(config.targets, config.variants.items(), range(config.repeats)))
        )
    spending = {
        "cost_usd": budget.spent,
        "cost_limit_usd": budget.limit,
        "overshoot_usd": max(0, budget.spent - budget.limit),
        "unknown_cost": budget.unknown_cost,
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
    return report(output, registry)


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
