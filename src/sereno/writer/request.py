"""The hook's request.json, checked and typed.

The request carries more than the provider may see (campaign paths, grades, test records), so the writer reads it as a
host-only object and projects from it. This module only validates its form and its internal consistency; nothing in
it reaches a prompt.
"""

import json
from dataclasses import dataclass
from pathlib import Path

from sereno.context_eval.memory import USER, memory_dir
from sereno.writer.config import WriterConfig
from sereno.writer.errors import ConfigError, InputError

SCHEMA_VERSION = "1.0"
ARM = "attack_adaptive"


@dataclass(frozen=True)
class Request:
    raw: dict
    kind: str
    campaign_dir: Path
    case: str
    repeat: int
    agent_id: str
    arm_dir: Path
    state_dir: Path
    transition_dir: Path
    # Position of the session this step precedes, 0-based, and the hook's transition number (001 is the first).
    number: int
    transition: int
    session_id: str
    task_id: str
    task_dir: Path
    project: str
    memory_dir: str
    memory_files: dict[str, str]
    owned: list[str]
    limits: dict
    finished: dict | None
    last_write: bool

    @property
    def chain(self) -> dict:
        """What names the chain a notebook and a view belong to."""
        return {
            "campaign": self.campaign_dir.name,
            "case": self.case,
            "arm": ARM,
            "repeat": self.repeat,
            "agent_id": self.agent_id,
        }


def read_request(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_bytes().decode("utf-8"))
    except (OSError, ValueError) as error:
        raise InputError(f"cannot read the request {path}: {error}", "bad_request") from None
    if not isinstance(data, dict):
        raise InputError("the request must hold an object", "bad_request")
    return data


def typed[T](data: dict, key: str, kind: type[T], where: str = "request") -> T:
    value = data.get(key)
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise InputError(f"{where}.{key} must be a {kind.__name__}", "bad_request")
    return value


def parse_request(raw: dict, config: WriterConfig) -> Request:
    """Check the request's form and that its paths and positions agree with each other."""
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise InputError(f"the request's schema_version must be {SCHEMA_VERSION!r}", "bad_request")
    if raw.get("arm") != ARM:
        raise InputError(f"the writer serves the {ARM} arm only, not {raw.get('arm')!r}", "bad_request")
    campaign = Path(typed(raw, "campaign_dir", str)).resolve()
    case, agent_id = typed(raw, "case", str), typed(raw, "agent_id", str)
    arm_dir = Path(typed(raw, "arm_dir", str)).resolve()
    if arm_dir != campaign / "cases" / case / "arms" / ARM:
        raise InputError("arm_dir is not the campaign's case and arm", "bad_request")
    state_dir, folder = Path(typed(raw, "state_dir", str)), Path(typed(raw, "transition_dir", str))
    if state_dir.resolve() != arm_dir / "state" or folder.resolve().parent != arm_dir / "transitions":
        raise InputError("state_dir or transition_dir is not under the arm directory", "bad_request")
    session = typed(raw, "next_session", dict)
    number = typed(session, "index", int, "next_session")
    if folder.name != f"{number + 1:03d}":
        raise InputError("transition_dir does not match next_session.index", "bad_request")
    finished = raw.get("finished_session")
    kind = raw.get("kind")
    if (number == 0) != (finished is None) or kind != ("between_sessions" if number else "before_first_session"):
        raise InputError("kind, next_session.index and finished_session disagree", "bad_request")
    if finished is not None and (not isinstance(finished, dict) or finished.get("index") != number - 1):
        raise InputError("finished_session is not the session before the next one", "bad_request")
    project, folder_memory = (
        typed(session, "project", str, "next_session"),
        typed(session, "memory_dir", str, "next_session"),
    )
    if folder_memory != memory_dir(project):
        raise InputError("next_session.memory_dir is not the project's memory directory", "bad_request")
    if folder_memory != config.memory_root:
        raise ConfigError(
            f"the assignment names {config.memory_root} as the memory root, the request {folder_memory}",
            "memory_root",
        )
    memory = typed(raw, "memory", dict)
    files, owned = memory.get("files"), memory.get("owned")
    if not (isinstance(files, dict) and all(isinstance(p, str) and isinstance(t, str) for p, t in files.items())):
        raise InputError("memory.files must map paths to text", "bad_request")
    if not (isinstance(owned, list) and all(isinstance(p, str) for p in owned)):
        raise InputError("memory.owned must be a list of paths", "bad_request")
    if USER in files or any(not p.startswith(folder_memory + "/") for p in files):
        raise InputError("memory.files holds a path outside the project's memory directory", "bad_request")
    last_write = raw.get("last_write", False)
    if not isinstance(last_write, bool):
        raise InputError("last_write must be true or false when present", "bad_request")
    task_dir = Path(typed(session, "task_dir", str, "next_session"))
    task_id = typed(session, "task_id", str, "next_session")
    if task_dir.name != task_id or task_dir.parent.name != "tasks":
        raise InputError("next_session.task_dir is not tasks/<task_id>", "bad_request")
    return Request(
        raw=raw,
        kind=kind,
        campaign_dir=campaign,
        case=case,
        repeat=typed(raw, "repeat", int),
        agent_id=agent_id,
        arm_dir=arm_dir,
        state_dir=state_dir,
        transition_dir=folder,
        number=number,
        transition=number + 1,
        session_id=typed(session, "id", str, "next_session"),
        task_id=task_id,
        task_dir=task_dir,
        project=project,
        memory_dir=folder_memory,
        memory_files=dict(sorted(files.items())),
        owned=sorted(owned),
        limits=typed(raw, "limits", dict),
        finished=finished,
        last_write=last_write,
    )
