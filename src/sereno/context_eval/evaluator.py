"""Grade session patches with each DeepSWE task's own verifier, in a separate container without network.

The verifier image is the task image with the hidden tests baked in (tests/Dockerfile). The patch enters as
/logs/artifacts/model.patch and tests/test.sh writes /logs/verifier/reward.json. A patch that changes test files
existing at the base commit is graded a second time with those files left at base (`test_edits`), except the
ones the task's reference solution also changes.
"""

import hashlib
import json
import os
import re
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
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from sereno.context_eval.contracts import Submission
from sereno.context_eval.dataset import load_task
from sereno.context_eval.engine import write_json

GRADED = {"graded", "apply_failed"}
BASE_LABEL = "sereno.base_id"
COUNTS = ("partial", "f2p", "p2p", "f2p_total", "f2p_passed", "p2p_total", "p2p_passed")
GRADE_SCHEMA = "1.1"
CTRF_ROW = re.compile(r"\[(f2p|p2p)\] (.+)")
# grader.py's own header pattern; group 2 is the path it resets.
DIFF_GIT = re.compile(r'^diff --git (?:"?a/(.*?)"?) (?:"?b/(.*?)"?)$')
TEST_DIRECTORIES = frozenset({"test", "tests", "__tests__", "testdata"})
TEST_FILES = ("test_*.py", "*_test.py", "conftest.py", "*_test.go", "*.test.*", "*.spec.*")
EXTENDED_HEADER_END = ("--- ", "+++ ", "@@", "GIT binary patch", "Binary files ")


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


def is_test_path(path: str) -> bool:
    """Whether a repository path is a test file, by its directories (`TEST_DIRECTORIES`) or its name (`TEST_FILES`):

    - Python: anything under `tests/` or `test/`, `test_*.py`, `*_test.py`, `conftest.py`;
    - Go: `*_test.go` and anything under `testdata/`, the directory only tests read;
    - TypeScript and JavaScript: anything under `__tests__/`, `test/` or `tests/`, `*.test.*`, `*.spec.*`;
    - Rust: anything under `tests/` (integration tests). Unit tests in `#[cfg(test)]` modules share a file with the
      code they test and cannot be separated from it, so those files count as source.
    """
    parts = PurePosixPath(path).parts
    return any(part in TEST_DIRECTORIES for part in parts[:-1]) or any(
        fnmatchcase(PurePosixPath(path).name, pattern) for pattern in TEST_FILES
    )


def patch_paths(text: str) -> set[str]:
    """The paths grader.py resets before applying a patch, by its own rule: each `diff --git` header's b-path and
    every `--- a/` and `+++ b/` path."""
    paths = set()
    for line in text.splitlines():
        header = DIFF_GIT.match(line)
        path = header.group(2) if header else line[6:] if line.startswith(("+++ b/", "--- a/")) else None
        if path and path != "/dev/null":
            paths.add(path)
    return paths


def patch_blocks(patch: bytes) -> list[bytes]:
    """The patch cut before every `diff --git` line; joined, the blocks give back the patch byte for byte."""
    return [block for block in re.split(rb"(?m)^(?=diff --git )", patch) if block]


def existing_test_file(block: bytes, owned: set[str]) -> str | None:
    """The base path of a test file that a `diff --git` block modifies, deletes or renames away. None for any other
    block: a new or copied file, a source file, or a file test.patch owns, which the grader resets anyway."""
    lines = [line.decode(errors="replace") for line in block.split(b"\n")]
    header = DIFF_GIT.match(lines[0])
    if header is None:
        return None
    path = header.group(2)
    for line in lines[1:]:
        if line.startswith(EXTENDED_HEADER_END):
            break
        if line.startswith(("new file mode", "copy from ")):
            return None
        if line.startswith("rename from "):
            path = line.removeprefix("rename from ").strip('"')
    return path if is_test_path(path) and path not in owned else None


def restore_tests(patch: bytes, owned: set[str]) -> tuple[bytes, list[str]]:
    """The patch without its blocks on existing test files, so those files stay at base, and their paths. Every
    other block keeps its exact bytes."""
    kept, files = [], []
    for block in patch_blocks(patch):
        path = existing_test_file(block, owned)
        if path is None:
            kept.append(block)
        else:
            files.append(path)
    return b"".join(kept), files


def existing_test_files(patch: bytes, owned: set[str]) -> set[str]:
    """The existing test files a patch changes, by `existing_test_file`."""
    return {path for block in patch_blocks(patch) if (path := existing_test_file(block, owned))}


def ctrf_passes(path: Path) -> dict[str, bool] | None:
    """Whether each `[f2p] id` and `[p2p] id` row of a verifier's ctrf.json passed; None when the report is missing
    or malformed."""
    report = read_object(path)
    results = report.get("results") if report else None
    tests = results.get("tests") if isinstance(results, dict) else None
    if not isinstance(tests, list):
        return None
    rows = {}
    for row in tests:
        name = row.get("name") if isinstance(row, dict) else None
        if not isinstance(name, str) or not CTRF_ROW.fullmatch(name) or not isinstance(row.get("status"), str):
            return None
        rows[name] = rows.get(name, True) and row["status"] == "passed"
    return rows


def edit_record(
    status: str,
    files: list[str] | None = None,
    reason: str | None = None,
    required_files: list[str] | None = None,
    **measured,
) -> dict:
    return {
        "status": status,
        "files": files,
        "required_files": required_files,
        "reason": reason,
        "restored_reward": None,
        "restored_partial": None,
        "masked_tests": None,
        "unmasked_tests": None,
        "restored_logs": None,
    } | measured


def edit_status(grade) -> str | None:
    """A grade's `test_edits` status; None for a grade written before the second pass or a malformed one."""
    edits = grade.get("test_edits") if isinstance(grade, dict) else None
    return edits.get("status") if isinstance(edits, dict) else None


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
                label = f"{BASE_LABEL}={base['Id']}"
                result = self.docker(["build", "-q", "--label", label, "-t", tag, str(tests)], build_timeout)
                if result.returncode:
                    raise RuntimeError(f"verifier build failed: {result.stderr.strip()[-2000:]}")
                image = self.inspect(tag)
            if image is None or not built_on(image, base):
                raise RuntimeError(f"verifier image {tag} is not built on task image {base['Id']}")
        return {"tag": tag, "id": image["Id"]}

    def verifier_spec(self, task_id: str) -> dict:
        return tomllib.loads((self.root / "tasks" / task_id / "task.toml").read_text()).get("verifier", {})

    def evaluate(self, submission: Submission, output_dir: Path) -> dict:
        started = time.monotonic()
        output_dir = Path(output_dir)
        grade = blank_grade(submission.task_id) | {"task_image_id": submission.image_id}
        patch = None
        try:
            patch = Path(submission.patch).read_bytes()
            grade["patch_sha256"] = hashlib.sha256(patch).hexdigest()
            grade.update(self._grade(submission, patch, output_dir, grade))
        except Exception as error:
            grade["error"] = f"{type(error).__name__}: {error}"
        grade["duration_seconds"] = round(time.monotonic() - started, 3)
        if patch is not None:
            grade["test_edits"] = self.test_edits(submission.task_id, patch, output_dir, grade)
        output_dir.mkdir(parents=True, exist_ok=True)
        write_json(output_dir / "grade.json", grade)
        return grade

    def _grade(self, submission: Submission, patch: bytes, output_dir: Path, grade: dict) -> dict:
        task = load_task(self.root, submission.task_id)
        if submission.base_commit != task.base_commit:
            return {"error": f"base commit {submission.base_commit} does not match the task's {task.base_commit}"}
        spec = self.verifier_spec(task.id)
        resources = spec.get("environment", {})
        timeout = grade["timeout_seconds"] = float(spec.get("timeout_sec", 1800))
        reference = base_image(self.root / "tasks" / task.id / "tests" / "Dockerfile")
        base = self.inspect(reference)
        if base is None:
            return {"error": f"verifier base image {reference} is not available locally"}
        if base["Id"] != submission.image_id:
            return {"error": f"verifier base image {reference} is {base['Id']}, not the graded {submission.image_id}"}
        grade["verifier"] = self.verifier_image(task.id, base, float(resources.get("build_timeout_sec", 1800)))
        outcome = self._verify(grade["verifier"]["id"], patch, output_dir / "grade" / "verifier", resources, timeout)
        return outcome | {"logs": "grade/verifier"}

    def _verify(self, image: str, patch: bytes, destination: Path, resources: dict, timeout: float) -> dict:
        """One verifier run on `patch`, its logs copied to `destination`; the outcome as grade fields."""
        if destination.exists():
            shutil.rmtree(destination)
        with tempfile.TemporaryDirectory(prefix="sereno-grade-", ignore_cleanup_errors=True) as logs:
            logs = Path(logs)
            (logs / "artifacts").mkdir()
            (logs / "artifacts" / "model.patch").write_bytes(patch)
            name = f"sereno-grade-{uuid.uuid4().hex[:16]}"
            command = f"bash /tests/test.sh; status=$?; chown -R {os.getuid()}:{os.getgid()} /logs; exit $status"
            args = ["run", "--rm", "--name", name, "--network", "none", "--cpus", str(resources.get("cpus", 2))]
            args += ["--memory", f"{resources.get('memory_mb', 8192)}m", "-v", f"{logs}:/logs"]
            try:
                result = self.docker([*args, image, "bash", "-c", command], timeout)
                stdout, timed_out = (result.stdout or "") + (result.stderr or ""), False
            except subprocess.TimeoutExpired as error:
                # The client timing out leaves the container running; stop it before it writes further logs.
                self.docker(["rm", "-f", name], 120)
                stdout, timed_out = text(error.stdout) + text(error.stderr), True
            verifier = logs / "verifier"
            if verifier.is_dir() and not verifier.is_symlink():
                shutil.copytree(verifier, destination, ignore=special_files)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "test-stdout.txt").write_text(stdout)
            if timed_out:
                # Strict success counts a timeout as a failure, so the partial score does too.
                error = f"verifier exceeded {timeout:g} s"
                return {"status": "verifier_timeout", "reward": 0, "partial": 0.0, "error": error}
            reward_json, reward_txt = destination / "reward.json", destination / "reward.txt"
            if reward_json.exists():
                data = json.loads(reward_json.read_text())
                outcome = {key: data.get(key) for key in COUNTS} | {"reward": int(data["reward"])}
                if data.get("apply_failed") == 1:
                    return outcome | {"status": "apply_failed", "reward": 0, "apply_failed": True}
                return outcome | {"status": "graded"}
            if reward_txt.exists():
                return {"error": f"verifier infrastructure failure (reward.txt {reward_txt.read_text().strip()})"}
            return {"error": f"verifier wrote no reward file (exit code {result.returncode})"}

    def test_edits(self, task_id: str, patch: bytes, output_dir: Path, grade: dict) -> dict:
        """The second pass: run the official run's verifier image again on the patch with the existing test files
        it changes left at base, logs in grade/restored/, and compare per test. Never changes the official grade.
        Existing test files the reference solution also changes are edits the task requires: they stay in the patch,
        listed as `required_files`."""
        files = required_files = None

        def record(status: str, reason: str | None = None, **measured) -> dict:
            return edit_record(status, files, reason, required_files, **measured)

        try:
            destination = output_dir / "grade" / "restored"
            if destination.exists():
                shutil.rmtree(destination)
            test_patch = self.root / "tasks" / task_id / "tests" / "test.patch"
            owned = patch_paths(test_patch.read_text(errors="replace")) if test_patch.exists() else set()
            solution = self.root / "tasks" / task_id / "solution" / "solution.patch"
            required = existing_test_files(solution.read_bytes(), owned) if solution.exists() else set()
            required_files = sorted(existing_test_files(patch, owned) & required)
            restored, files = restore_tests(patch, owned | required)
            if not files:
                return record("none")
            if grade["status"] != "graded":
                return record("not_applicable", f"the official run is {grade['status']}")
            image = grade["verifier"]["id"]
            if self.inspect(image) is None:
                return record("unknown", f"verifier image {image} is not available locally")
            resources = self.verifier_spec(task_id).get("environment", {})
            outcome = self._verify(image, restored, destination, resources, grade["timeout_seconds"])
            logs = {"restored_logs": "grade/restored"}
            if outcome.get("status") != "graded":
                reason = f"restored run: {outcome.get('error') or outcome.get('status')}"
                return record("unknown", reason, **logs)
            scores = {"restored_reward": outcome["reward"], "restored_partial": outcome["partial"]} | logs
            official = ctrf_passes(output_dir / "grade" / "verifier" / "ctrf.json")
            again = ctrf_passes(destination / "ctrf.json")
            if official is None or again is None:
                return record("unknown", "unreadable ctrf.json", **scores)
            # A row missing from a report counts as failed, as in the grader.
            masked = [name for name, passed in official.items() if passed and not again.get(name, False)]
            unmasked = [name for name, passed in official.items() if not passed and again.get(name, False)]
            status = "masking" if masked else "consistent"
            return record(status, masked_tests=masked, unmasked_tests=unmasked, **scores)
        except Exception as error:
            return record("unknown", f"{type(error).__name__}: {error}")

    def backfill(self, directory: Path, grade: dict) -> dict:
        """`test_edits` for a grade written before the second pass existed, from the model.patch it graded; the
        official run is read, never repeated."""
        if grade.get("patch_sha256") is None:
            return edit_record("not_applicable", reason="the patch was not graded")
        try:
            patch = (directory / "model.patch").read_bytes()
        except OSError as error:
            return edit_record("unknown", reason=f"{type(error).__name__}: {error}")
        if hashlib.sha256(patch).hexdigest() != grade["patch_sha256"]:
            return edit_record("unknown", reason="model.patch is not the graded patch")
        return self.test_edits(grade.get("task_id"), patch, directory, grade)


def special_files(directory: str, names: list[str]) -> list[str]:
    """Symlinks and non-regular files the patched code may leave under /logs; copying them would read the host."""
    return [n for n in names if (p := Path(directory, n)).is_symlink() or not (p.is_file() or p.is_dir())]


def text(value: str | bytes | None) -> str:
    return value.decode(errors="replace") if isinstance(value, bytes) else value or ""


def blank_grade(task_id: str) -> dict:
    return {
        "schema_version": GRADE_SCHEMA,
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
        "test_edits": edit_record("not_applicable", reason="the patch was not graded"),
    }


def built_on(image: dict, base: dict) -> bool:
    """Whether the build labelled the image with this base's ID, which fixes both the base layers and its config
    (ENV, WORKDIR), so a re-pulled base whose layers match but whose config differs still counts as stale."""
    labels = (image.get("Config") or {}).get("Labels") or {}
    return labels.get(BASE_LABEL) == base["Id"]


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


def grade_campaign(
    campaign: Path,
    *,
    dataset: Path | None = None,
    workers: int = 1,
    force: bool = False,
    test_edits_only: bool = False,
    docker=None,
):
    """Grade every complete session. `test_edits_only` adds only the second pass to existing grades that lack it."""
    if force and test_edits_only:
        raise ValueError("--force and --test-edits-only exclude each other")
    campaign = Path(campaign).resolve()
    if dataset is None:
        dataset = Path(json.loads((campaign / "manifest.json").read_text())["config"]["dataset_root"])
    evaluator = DeepSWEEvaluator(Path(dataset).expanduser().resolve(), docker=docker)
    directories = session_dirs(campaign)
    copies = {d: shared_origin(campaign, d) for d in directories}
    written, kept, backfilled, unreadable = [], [], [], {}

    def backfill(directory: Path, origin: Path | None = None) -> None:
        grade = read_object(directory / "grade.json")
        # A missing grade stays ungraded and an unreadable one is reported below.
        if grade is None:
            return
        if "test_edits" in grade:
            kept.append(directory)
            return
        if origin is None:
            edits = evaluator.backfill(directory, grade)
        else:
            shared = read_object(origin / "grade.json")
            if shared is None or "test_edits" not in shared:
                return
            edits = shared["test_edits"]
        write_json(directory / "grade.json", grade | {"schema_version": GRADE_SCHEMA, "test_edits": edits})
        backfilled.append(directory)

    def grade_one(directory: Path) -> None:
        if test_edits_only:
            backfill(directory)
            return
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
        if test_edits_only:
            backfill(directory, origin)
        elif (directory / "grade.json").exists() and not force:
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
    edits = Counter(edit_status(g) or "unmeasured" for g in grades.values())
    return {
        "campaign": str(campaign),
        "sessions": len(directories),
        "graded_files": len(grades),
        "written": len(written),
        "kept": len(kept),
        "backfilled": len(backfilled),
        "unreadable": len(unreadable),
        "statuses": dict(sorted(Counter(g["status"] for g in grades.values()).items())),
        "test_edits": dict(sorted(edits.items())),
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
