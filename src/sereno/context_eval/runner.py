"""Run isolated clean/carry/reset arms and retain reproducible, grade-ready artifacts."""

import itertools
import json
import platform
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import version
from pathlib import Path

from sereno.context_eval.config import fingerprint, validate
from sereno.context_eval.dataset import image_identity, load_task, provenance
from sereno.context_eval.engine import EventLog, InterventionEngine, Runtime, separate_patch, write_json
from sereno.context_eval.environment import DockerEnvironment
from sereno.context_eval.memory import FileMemory
from sereno.context_eval.metrics import report
from sereno.context_eval.schema import AgentOutcome


class Budget:
    """Reserve per-session budgets across workers; stop new calls after reported spend exhausts the cap."""

    def __init__(self, limit: float):
        self.limit, self.spent, self.reserved = limit, 0.0, 0.0
        self.unknown_cost = False
        self.condition = threading.Condition()

    def acquire(self, amount: float) -> float:
        with self.condition:
            if self.unknown_cost:
                raise RuntimeError("campaign cost accounting unavailable after a failed model call")
            while self.limit - self.spent - self.reserved <= 0 and self.reserved > 0:
                self.condition.wait()
                if self.unknown_cost:
                    raise RuntimeError("campaign cost accounting unavailable after a failed model call")
            allowance = min(amount, self.limit - self.spent - self.reserved)
            if allowance <= 0:
                raise RuntimeError("campaign cost budget exhausted")
            self.reserved += allowance
            return allowance

    def finish(self, reserved: float, spent: float, *, unknown=False):
        with self.condition:
            self.reserved -= reserved
            self.spent += spent
            self.unknown_cost |= unknown
            self.condition.notify_all()


def run_session(config, session, task, identity, memory, engine, adapter, directory, budget, env_factory):
    directory.mkdir(parents=True, exist_ok=False)
    run_id = str(uuid.uuid4())
    log = EventLog(directory / "events.jsonl", run_id)
    write_json(directory / "memory_start.json", memory)
    allowance = 0.0
    runtime, env = None, None
    started = time.monotonic()
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
        "steps": 0,
        "cost_usd": 0.0,
        "markers": {e.id: e.marker for e in engine.events if e.marker},
        "untracked_interventions": [e.id for e in engine.events if not e.marker],
        "workspace": {},
        "patch_status": "missing",
        "task_success": "not_evaluated",
    }
    try:
        if config.agent != "scripted":
            allowance = budget.acquire(config.cost_limit_usd)
        env = env_factory(identity["id"], config.wall_time_limit_seconds)
        if env.execute("git -c safe.directory=/app rev-parse HEAD")["output"].strip() != task.base_commit:
            raise ValueError("image HEAD does not match the task base commit")
        mem = FileMemory()
        if config.memory.enabled:
            if env.execute("mkdir -p /memories")["returncode"]:
                raise RuntimeError("cannot initialize memory directory")
            if env.snapshot_memory(config.memory.max_files, config.memory.max_bytes):
                raise ValueError("task image must start with an empty memory directory")
        mem.restore(env, memory)
        runtime = Runtime(env, engine, log, session, config.memory, memory, config.checks)
        log.emit("session_start", session_id=session.id, task_id=task.id, image=identity)
        engine.apply(runtime, "session_start")
        context = mem.context(runtime.memory) if config.memory.enabled else ""
        runtime.initial_memory_context = context
        write_json(
            directory / "initial_context.json",
            {"instruction": session.instruction or task.instruction, "memory": context},
        )
        bounded = config.model_copy(update={"cost_limit_usd": allowance or config.cost_limit_usd})
        agent_result = AgentOutcome.model_validate(
            adapter.run(runtime, session.instruction or task.instruction, context, bounded, session)
        ).model_dump()
        result.update({k: v for k, v in agent_result.items() if k != "messages"})
        write_json(directory / "traj.json", getattr(runtime, "agent_trajectory", agent_result))
        status = agent_result["exit_status"]
        if status == "LimitsExceeded" and agent_result["steps"] >= session.max_steps:
            status = "session_boundary"
        result["status"] = "complete" if status in {"Submitted", "script_complete", "session_boundary"} else "invalid"
        engine.apply(runtime, "session_end")
        memory = runtime.capture_memory("agent")
        for check in config.checks:
            if check.source == "workspace" and (not check.sessions or session.id in check.sessions):
                result["workspace"][check.path] = env.read(check.path) or ""
        (directory / "raw.patch").write_text(env.collect_patch(task.base_commit))
        try:
            patch = separate_patch(env, runtime.journal, task.base_commit)
            (directory / "model.patch").write_text(patch)
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
        write_json(directory / "result.json", result)
        log.emit("session_end", session_id=session.id, status=result["status"], patch_status=result["patch_status"])
    return memory, result


def run_campaign(config, output: Path, registry, *, env_factory=DockerEnvironment, identities=None) -> dict:
    validate(config, registry)
    # Preflight every image before starting a container or making a paid request.
    tasks = {t.task_id: load_task(config.dataset_root, t.task_id) for t in config.targets}
    for session in config.sessions:
        if session.task_id and session.task_id not in tasks:
            tasks[session.task_id] = load_task(config.dataset_root, session.task_id)
    overrides = {t.task_id: t.image for t in config.targets if t.image}
    identities = identities or {key: image_identity(overrides.get(key, task.image)) for key, task in tasks.items()}
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "cases").mkdir()
    frozen_config = config.model_dump(mode="json")
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
        "model_config": config.model_config_file.read_text() if config.model_config_file else None,
        "payload_sha256": {
            name: {e.id: fingerprint(e.text) for e in events} for name, events in config.variants.items()
        },
        "tasks": {
            key: {"base_commit": task.base_commit, "instruction_sha256": fingerprint(task.instruction)}
            for key, task in tasks.items()
        },
    }
    write_json(output / "manifest.json", manifest)
    if config.model_config_file:
        # All sessions use the same frozen model config, even if the source file changes mid-campaign.
        model_snapshot = output / "model-config.yaml"
        model_snapshot.write_text(manifest["model_config"])
        config = config.model_copy(update={"model_config_file": model_snapshot})
    budget = Budget(config.campaign_cost_limit_usd)
    combinations = list(itertools.product(config.targets, config.variants.items(), range(config.repeats)))

    def run_case(item):
        target, (variant, events), repeat = item
        case_id = f"{target.task_id}--{variant}--r{repeat + 1:03d}"
        case_dir = output / "cases" / case_id
        (case_dir / "arms").mkdir(parents=True)
        write_json(
            case_dir / "case.json",
            {"target": target.task_id, "variant": variant, "repeat": repeat, "seed": config.seed + repeat},
        )
        carry_prefix = []
        for arm in (name for name in ("clean", "attack_carry", "attack_reset") if name in config.arms):
            arm_dir = case_dir / "arms" / arm
            (arm_dir / "sessions").mkdir(parents=True)
            memory = config.memory.initial()
            engine = InterventionEngine(events, registry, enabled=arm != "clean", seed=config.seed + repeat)
            for number, session in enumerate(config.sessions):
                directory = arm_dir / "sessions" / f"{number + 1:03d}-{session.id}"
                if arm == "attack_reset" and session.exposure:
                    source = carry_prefix[number]
                    shutil.copytree(source, directory)
                    write_json(
                        directory / "branch.json",
                        {"shared_exposure": str(source.relative_to(output)), "origin": "attack_carry"},
                    )
                    memory = json.loads((directory / "memory_end.json").read_text())
                    result = json.loads((directory / "result.json").read_text())
                else:
                    if (
                        arm == "attack_reset"
                        and not session.exposure
                        and (number == 0 or config.sessions[number - 1].exposure)
                    ):
                        memory = config.memory.initial()
                    task_id = session.task_id or target.task_id
                    try:
                        memory, result = run_session(
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
                if arm == "attack_carry" and session.exposure:
                    carry_prefix.append(directory)
                if not config.memory.enabled:
                    memory = {}
                if result["status"] != "complete":
                    # The remaining planned sessions are explicit missing entries, not silently successful probes.
                    for skipped_number, skipped in enumerate(config.sessions[number + 1 :], start=number + 1):
                        skipped_dir = arm_dir / "sessions" / f"{skipped_number + 1:03d}-{skipped.id}"
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
                        if arm == "attack_carry" and skipped.exposure:
                            carry_prefix.append(skipped_dir)
                    break
        return case_id

    with ThreadPoolExecutor(max_workers=config.workers) as pool:
        cases = list(pool.map(run_case, combinations))
    write_json(
        output / "campaign.json",
        {
            "cases": cases,
            "cost_usd": budget.spent,
            "cost_limit_usd": budget.limit,
            "overshoot_usd": max(0, budget.spent - budget.limit),
            "unknown_cost": budget.unknown_cost,
        },
    )
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
            "patch_sha256": fingerprint((destination / "model.patch").read_text()),
        },
    )
    return destination
