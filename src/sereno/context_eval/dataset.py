"""Read the external DeepSWE checkout and resolve local immutable Docker identities."""

import json
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Task:
    id: str
    image: str
    base_commit: str
    instruction: str
    language: str
    repository: str


def load_task(root: Path, task_id: str) -> Task:
    directory = root / "tasks" / task_id
    spec = tomllib.loads((directory / "task.toml").read_text())
    metadata = spec["metadata"]
    return Task(
        task_id,
        spec["environment"]["docker_image"],
        metadata["base_commit_hash"],
        (directory / "instruction.md").read_text(),
        metadata.get("language", ""),
        metadata.get("repository_url", ""),
    )


def image_identity(image: str) -> dict:
    result = subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)[0]
    return {"reference": image, "id": data["Id"], "digests": data.get("RepoDigests", [])}


def provenance(root: Path) -> dict:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
    return {"root": str(root), "commit": result.stdout.strip() if result.returncode == 0 else None}


def catalog(root: Path) -> list[dict]:
    result = subprocess.run(
        ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True,
        text=True,
        check=True,
    )
    local = set(result.stdout.splitlines())
    rows = []
    for directory in sorted((root / "tasks").iterdir()):
        if (directory / "task.toml").exists():
            task = load_task(root, directory.name)
            rows.append({**vars(task), "instruction": None, "available": task.image in local})
    return rows
