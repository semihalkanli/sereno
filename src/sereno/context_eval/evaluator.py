"""Grade session patches with each DeepSWE task's own verifier, in a separate container without network.

The verifier image is the task image with the hidden tests baked in (tests/Dockerfile). The patch enters as
/logs/artifacts/model.patch and tests/test.sh writes /logs/verifier/reward.json.
"""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import tomllib
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from sereno.context_eval.contracts import Submission
from sereno.context_eval.dataset import load_task
from sereno.context_eval.engine import write_json

GRADED = {"graded", "apply_failed"}
COUNTS = ("partial", "f2p", "p2p", "f2p_total", "f2p_passed", "p2p_total", "p2p_passed")


def run_docker(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    # The verifier prints raw test output, which need not be valid UTF-8.
    return subprocess.run(["docker", *args], capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)


def directory_digest(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        relative = path.relative_to(directory).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big") + relative + len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


def base_image(dockerfile: Path) -> str:
    for line in dockerfile.read_text().splitlines():
        words = line.split()
        if words and words[0].upper() == "FROM":
            images = [w for w in words[1:] if not w.startswith("--")]
            if not images or "$" in images[0]:
                raise ValueError(f"unsupported FROM line in {dockerfile}: {line.strip()}")
            return images[0]
    raise ValueError(f"no FROM line in {dockerfile}")


class DeepSWEEvaluator:
    """EvaluatorAdapter for DeepSWE. Every Docker call goes through `docker(args, timeout)`, `run_docker` by default."""

    def __init__(self, dataset_root: Path, *, docker=None):
        self.root = Path(dataset_root)
        self.docker = docker or run_docker
        self.locks: dict[str, threading.Lock] = {}
        self.guard = threading.Lock()

    def inspect(self, image: str) -> dict | None:
        result = self.docker(["image", "inspect", image])
        return json.loads(result.stdout)[0] if result.returncode == 0 else None

    def verifier_image(self, task_id: str, base: dict, build_timeout: float) -> dict:
        tests = self.root / "tasks" / task_id / "tests"
        tag = f"sereno-deepswe-verifier:{task_id}-{directory_digest(tests)[:12]}"
        with self.guard:
            lock = self.locks.setdefault(tag, threading.Lock())
        with lock:
            image = self.inspect(tag)
            # The tag hashes only tests/; a re-pulled base image makes a cached verifier stale, so rebuild it.
            if image is None or not built_on(image, base):
                result = self.docker(["build", "-q", "-t", tag, str(tests)], build_timeout)
                if result.returncode:
                    raise RuntimeError(f"verifier build failed: {result.stderr.strip()[-2000:]}")
                image = self.inspect(tag)
            if image is None or not built_on(image, base):
                raise RuntimeError(f"verifier image {tag} is not built on task image {base['Id']}")
        return {"tag": tag, "id": image["Id"]}

    def evaluate(self, submission: Submission, output_dir: Path) -> dict:
        started = time.monotonic()
        output_dir = Path(output_dir)
        grade = blank_grade(submission.task_id) | {"task_image_id": submission.image_id}
        try:
            patch = Path(submission.patch).read_bytes()
            grade["patch_sha256"] = hashlib.sha256(patch).hexdigest()
            grade.update(self._grade(submission, patch, output_dir, grade))
        except Exception as error:
            grade["error"] = f"{type(error).__name__}: {error}"
        grade["duration_seconds"] = round(time.monotonic() - started, 3)
        output_dir.mkdir(parents=True, exist_ok=True)
        write_json(output_dir / "grade.json", grade)
        return grade

    def _grade(self, submission: Submission, patch: bytes, output_dir: Path, grade: dict) -> dict:
        task = load_task(self.root, submission.task_id)
        if submission.base_commit != task.base_commit:
            return {"error": f"base commit {submission.base_commit} does not match the task's {task.base_commit}"}
        spec = tomllib.loads((self.root / "tasks" / task.id / "task.toml").read_text()).get("verifier", {})
        resources = spec.get("environment", {})
        timeout = grade["timeout_seconds"] = float(spec.get("timeout_sec", 1800))
        reference = base_image(self.root / "tasks" / task.id / "tests" / "Dockerfile")
        base = self.inspect(reference)
        if base is None:
            return {"error": f"verifier base image {reference} is not available locally"}
        if base["Id"] != submission.image_id:
            return {"error": f"verifier base image {reference} is {base['Id']}, not the graded {submission.image_id}"}
        grade["verifier"] = self.verifier_image(task.id, base, float(resources.get("build_timeout_sec", 1800)))
        destination = output_dir / "grade" / "verifier"
        if destination.exists():
            shutil.rmtree(destination)
        with tempfile.TemporaryDirectory(prefix="sereno-grade-", ignore_cleanup_errors=True) as logs:
            logs = Path(logs)
            (logs / "artifacts").mkdir()
            (logs / "artifacts" / "model.patch").write_bytes(patch)
            name = f"sereno-grade-{uuid.uuid4().hex[:16]}"
            command = f"bash /tests/test.sh; chown -R {os.getuid()}:{os.getgid()} /logs"
            args = ["run", "--rm", "--name", name, "--network", "none", "--cpus", str(resources.get("cpus", 2))]
            args += ["--memory", f"{resources.get('memory_mb', 8192)}m", "-v", f"{logs}:/logs"]
            try:
                result = self.docker([*args, grade["verifier"]["id"], "bash", "-c", command], timeout)
                stdout, timed_out = (result.stdout or "") + (result.stderr or ""), False
            except subprocess.TimeoutExpired as error:
                # The client timing out leaves the container running; stop it before it writes further logs.
                self.docker(["rm", "-f", name], 120)
                stdout, timed_out = text(error.stdout) + text(error.stderr), True
            verifier = logs / "verifier"
            if verifier.is_dir():
                shutil.copytree(verifier, destination)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "test-stdout.txt").write_text(stdout)
            grade["logs"] = "grade/verifier"
            if timed_out:
                # Strict success counts a timeout as a failure, so the partial score does too.
                error = f"verifier exceeded {timeout:g} s"
                return {"status": "verifier_timeout", "reward": 0, "partial": 0.0, "error": error}
            reward_json, reward_txt = verifier / "reward.json", verifier / "reward.txt"
            if reward_json.exists():
                data = json.loads(reward_json.read_text())
                outcome = {key: data.get(key) for key in COUNTS} | {"reward": int(data["reward"])}
                if data.get("apply_failed") == 1:
                    return outcome | {"status": "apply_failed", "reward": 0, "apply_failed": True}
                return outcome | {"status": "graded"}
            if reward_txt.exists():
                return {"error": f"verifier infrastructure failure (reward.txt {reward_txt.read_text().strip()})"}
            return {"error": f"verifier wrote no reward file (exit code {result.returncode})"}


def text(value: str | bytes | None) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else value or ""


def blank_grade(task_id: str) -> dict:
    return {
        "schema_version": "1.0",
        "task_id": task_id,
        "status": "grader_error",
        "reward": None,
        **dict.fromkeys(COUNTS),
        "apply_failed": False,
        "verifier": None,
        "task_image_id": None,
        "patch_sha256": None,
        "timeout_seconds": None,
        "duration_seconds": None,
        "error": None,
        "logs": None,
    }


def built_on(image: dict, base: dict) -> bool:
    layers, prefix = image["RootFS"].get("Layers", []), base["RootFS"].get("Layers", [])
    return layers[: len(prefix)] == prefix


def session_dirs(campaign: Path) -> list[Path]:
    return sorted(p for p in campaign.glob("cases/*/arms/*/sessions/*") if p.is_dir())


def shared_origin(campaign: Path, directory: Path) -> Path | None:
    if not (directory / "branch.json").exists():
        return None
    branch = json.loads((directory / "branch.json").read_text())
    relative = branch.get("shared_from") or branch.get("shared_exposure")
    origin = (campaign / relative).resolve() if relative else None
    if origin is None or not origin.is_relative_to(campaign) or not origin.is_dir():
        raise ValueError(f"{directory}: branch.json does not point at a session in the campaign")
    return origin


def grade_campaign(campaign: Path, *, dataset: Path | None = None, workers: int = 1, force: bool = False, docker=None):
    campaign = Path(campaign).resolve()
    if dataset is None:
        dataset = Path(json.loads((campaign / "manifest.json").read_text())["config"]["dataset_root"])
    evaluator = DeepSWEEvaluator(Path(dataset).expanduser().resolve(), docker=docker)
    directories = session_dirs(campaign)
    copies = {d: shared_origin(campaign, d) for d in directories}
    written, kept, unreadable = [], [], {}

    def grade_one(directory: Path) -> None:
        if (directory / "grade.json").exists() and not force:
            kept.append(directory)
            return
        result = read_object(directory / "result.json")
        if result is None:
            unreadable[directory] = "unreadable result.json"
            return
        if result.get("status") != "complete":
            return
        try:
            if result.get("patch_status") == "ready":
                submission = Submission(
                    result["task_id"], result["image"]["id"], result["base_commit"], directory / "model.patch"
                )
                evaluator.evaluate(submission, directory)
            else:
                error = result.get("patch_error") or f"patch status {result.get('patch_status')}"
                grade = blank_grade(result["task_id"]) | {"status": "not_gradable", "error": error}
                write_json(directory / "grade.json", grade | {"task_image_id": (result.get("image") or {}).get("id")})
        except (KeyError, TypeError) as error:
            unreadable[directory] = f"result.json lacks {error}"
            return
        written.append(directory)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(grade_one, sorted({origin or d for d, origin in copies.items()})))
    # Shared sessions are copies of their origin: one sample, graded once.
    for directory, origin in copies.items():
        if origin is None:
            continue
        if (directory / "grade.json").exists() and not force:
            kept.append(directory)
        elif (origin / "grade.json").exists():
            shared = read_object(origin / "grade.json")
            if shared is None:
                unreadable[directory] = "unreadable grade.json in its origin session"
                continue
            write_json(directory / "grade.json", shared | {"shared_from": str(origin.relative_to(campaign))})
            written.append(directory)
    grades = {}
    for directory in directories:
        if directory in unreadable or not (directory / "grade.json").exists():
            continue
        grade = read_object(directory / "grade.json")
        if grade is None or "status" not in grade:
            unreadable[directory] = "unreadable grade.json"
        else:
            grades[directory] = grade
    # Sessions whose result.json or grade.json cannot be read are skipped so the rest still get graded.
    failures = {d: ("unreadable", error) for d, error in unreadable.items()}
    failures |= {d: (g["status"], g.get("error")) for d, g in grades.items() if g["status"] not in GRADED}
    return {
        "campaign": str(campaign),
        "sessions": len(directories),
        "graded_files": len(grades),
        "written": len(written),
        "kept": len(kept),
        "unreadable": len(unreadable),
        "statuses": dict(sorted(Counter(g["status"] for g in grades.values()).items())),
        "failures": [
            {"session": str(d.relative_to(campaign)), "status": status, "error": error}
            for d, (status, error) in sorted(failures.items())
        ],
    }


def read_object(path: Path) -> dict | None:
    """A JSON object, or null when the file is missing, truncated or not an object."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def grade_check(dataset: Path, task_ids: list[str], *, out: Path | None = None, workers: int = 1, docker=None):
    dataset = Path(dataset).expanduser().resolve()
    out = Path(out or Path("runs/context-eval/grade-check") / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    evaluator = DeepSWEEvaluator(dataset, docker=docker)
    tasks = {}
    for task_id in dict.fromkeys(task_ids):
        task = load_task(dataset, task_id)
        image = evaluator.inspect(task.image)
        if image is None:
            raise ValueError(f"task image {task.image} is not available locally")
        tasks[task_id] = (task, image["Id"])
    out.mkdir(parents=True, exist_ok=False)
    jobs = []
    for task_id, (task, image_id) in tasks.items():
        (out / task_id).mkdir()
        empty = out / task_id / "empty.patch"
        empty.write_bytes(b"")
        gold = dataset / "tasks" / task_id / "solution" / "solution.patch"
        for kind, patch in (("gold", gold), ("empty", empty)):
            jobs.append((task_id, kind, Submission(task_id, image_id, task.base_commit, patch)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        grades = list(pool.map(lambda job: evaluator.evaluate(job[2], out / job[0] / job[1]), jobs))
    expected = {"gold": 1, "empty": 0}
    rows = [
        {
            "task_id": task_id,
            "patch": kind,
            "expected": expected[kind],
            "status": grade["status"],
            "reward": grade["reward"],
            "duration_seconds": grade["duration_seconds"],
            "ok": grade["status"] == "graded" and grade["reward"] == expected[kind],
            "error": grade["error"],
        }
        for (task_id, kind, _), grade in zip(jobs, grades, strict=True)
    ]
    return {"out": str(out.resolve()), "passed": all(r["ok"] for r in rows), "results": rows}
