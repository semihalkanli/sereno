"""The permitted view: the only text that reaches the provider.

The view is built into a new typed envelope from an explicit allowlist; nothing is redacted out of a larger object. The
sources are the task text and three fields of its task.toml, the repository at the base commit read from a verified
object store, the current memory the hook handed over, the chain's private notebook, and the finished sessions of
exactly this chain, rebuilt from their event logs by event kind. Hidden tests, the reference solution, the verifier
settings, grading detail, the guard's records, the clean arm, other repeats and future tasks have no accessor here, and
every source is entered into a provenance manifest with its hash before anything is serialized.
"""

import hashlib
import json
import os
import posixpath
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sereno.context_eval.engine import message_text, reasoning_text
from sereno.context_eval.memory import USER, loaded_by
from sereno.context_eval.schema import AdaptiveLimits
from sereno.writer.config import WriterConfig
from sereno.writer.errors import ConfigError, InputError
from sereno.writer.outcomes import outcome_sources, session_outcomes
from sereno.writer.provider import encode, request_body
from sereno.writer.request import Request

EDGE = 5
TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
COMMIT = re.compile(r"[0-9a-f]{40}")
# The only task.toml fields the view reads (decision 407): the repository address, the base commit and the title.
TASK_FIELDS = ["repository_url", "base_commit_hash", "display_title"]
# The closed list of source kinds, and for a file kind the only names it may have. A finished session contributes its
# event log, its final memory and its result record (read for the task id only); the grading files feed two booleans.
FILE_KINDS = {
    "task_instruction": ("instruction.md",),
    "task_metadata": ("task.toml",),
    "session": ("events.jsonl", "memory_end.json", "result.json"),
    "outcome_source": ("grade.json", "ctrf.json"),
}
OTHER_KINDS = frozenset(
    {"repository_tree", "repository_file", "repository_history", "memory_file", "notebook", "assignment"}
)
GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "LC_ALL": "C",
}


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8", "surrogatepass"))


def audit_sources(entries: list[dict]) -> None:
    """The closed-world check of a provenance manifest: a known kind, and a file source with an allowed name. Tests,
    solution, environment, guard records, transitions and other arms have no kind, so none can pass."""
    for entry in entries:
        kind = entry.get("kind")
        if kind in FILE_KINDS:
            if Path(str(entry.get("ref"))).name not in FILE_KINDS[kind]:
                raise InputError(f"{entry.get('ref')} is not an allowed {kind} source", "forbidden_source")
        elif kind not in OTHER_KINDS:
            raise InputError(f"unknown source kind {kind!r}", "forbidden_source")


@dataclass
class View:
    envelope: dict
    messages: list[dict]
    sources: list[dict]
    omitted: list[dict]
    messages_sha256: str
    # Files read from disk and the hash they had when read, to detect a change before dispatch.
    files: dict[str, str] = field(default_factory=dict)

    def manifest(self) -> dict:
        return {"sources": self.sources, "omitted": self.omitted, "messages_sha256": self.messages_sha256}

    def reverify(self) -> None:
        """Refuse to dispatch when the manifest is outside the allowlist or a source file changed after it was read."""
        audit_sources(self.sources)
        for path, digest in self.files.items():
            try:
                current = sha256_bytes(Path(path).read_bytes())
            except OSError as error:
                raise InputError(f"source {path} cannot be read again: {error}", "source_changed") from None
            if current != digest:
                raise InputError(f"source {path} changed after it was read", "source_changed")


class Sources:
    """The provenance manifest being built. Every disk source goes through `read`, which checks the path against the
    allowlist of its kind before it opens anything."""

    def __init__(self):
        self.entries: list[dict] = []
        self.files: dict[str, str] = {}

    def add(self, kind: str, ref: str, digest: str, **extra) -> None:
        self.entries.append({"kind": kind, "ref": ref, "sha256": digest, **extra})
        audit_sources(self.entries[-1:])

    def read(self, kind: str, path: Path, parent: Path, **extra) -> bytes:
        """A regular, non-symlink file `parent/<name>` with a name its kind allows, read once and entered with its
        hash."""
        if path.parent != parent or path.name not in FILE_KINDS[kind] or path.is_symlink() or not path.is_file():
            raise InputError(f"{path} is not an allowed {kind} source", "forbidden_source")
        raw = path.read_bytes()
        self.files[str(path)] = sha256_bytes(raw)
        self.add(kind, str(path), sha256_bytes(raw), bytes=len(raw), **extra)
        return raw


def task_info(task_dir: Path, sources: Sources, *, full: bool) -> dict:
    """The task text and, from task.toml, the repository address, the base commit and the title: nothing else."""
    instruction = sources.read("task_instruction", task_dir / "instruction.md", task_dir).decode("utf-8")
    raw = sources.read(
        "task_metadata",
        task_dir / "task.toml",
        task_dir,
        fields_used=["repository_url", "base_commit_hash", "display_title"],
    )
    try:
        metadata = tomllib.loads(raw.decode("utf-8")).get("metadata") or {}
    except ValueError as error:
        raise InputError(f"{task_dir}/task.toml cannot be read: {error}", "bad_task") from None
    fields = {key: metadata.get(key) for key in TASK_FIELDS}
    if not all(isinstance(value, str) and value for value in fields.values()):
        raise InputError(f"{task_dir}/task.toml lacks repository_url, base_commit_hash or display_title", "bad_task")
    if full:
        return {
            "title": fields["display_title"],
            "repository": fields["repository_url"],
            "base_commit": fields["base_commit_hash"],
            "instruction": instruction,
        }
    return {"title": fields["display_title"], "instruction": instruction}


def git(store: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "--git-dir", str(store), *args],
            capture_output=True,
            check=True,
            timeout=120,
            env={"PATH": os.environ.get("PATH", ""), **GIT_ENV},
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as error:
        detail = getattr(error, "stderr", b"") or b""
        raise InputError(
            f"git {args[0]} failed in {store}: {error} {detail.decode(errors='replace')[:200]}", "bad_store"
        ) from None


def named_paths(text: str, paths: list[str]) -> list[str]:
    """Tree paths the task text names: a full path, a file name that is unique in the tree, or a dotted module name
    (`pkg.mod` for `pkg/mod.py` or `pkg/mod/__init__.py`). Sorted, so the choice is deterministic."""
    known, found = set(paths), set()
    by_name: dict[str, list[str]] = {}
    for path in paths:
        by_name.setdefault(posixpath.basename(path), []).append(path)
    for token in {t.strip(".,;:()/") for t in TOKEN.findall(text)}:
        if token in known:
            found.add(token)
        if "." in token and "/" not in token and len(by_name.get(token, [])) == 1:
            found.add(by_name[token][0])
        if "." in token and "/" not in token:
            stem = token.replace(".", "/")
            found.update(path for path in (f"{stem}.py", f"{stem}/__init__.py") if path in known)
    return sorted(found)


def repository_view(info: dict, store: Path, config: WriterConfig, sources: Sources, omitted: list[dict]) -> dict:
    """The repository at the base commit from a bare store, by one deterministic rule: the full tree (path and size),
    the files the task text names, every `__init__.py`, and the subjects of the reachable history. A file over the
    size limit or not UTF-8 is listed in `omitted`, never cut."""
    commit = info["base_commit"]
    if not COMMIT.fullmatch(commit):
        raise InputError(f"the base commit {commit!r} is not a full commit id", "bad_task")
    if git(store, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip() != commit:
        raise InputError(f"{store} does not resolve {commit} to itself", "bad_store")
    tree, blobs = {}, {}
    for record in git(store, "ls-tree", "-r", "-l", "-z", commit).split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        mode, kind, blob, size = meta.decode().split()
        tree[path.decode("utf-8", "replace")] = int(size) if size.isdigit() else 0
        if kind == "blob" and mode != "120000":
            blobs[path.decode("utf-8", "replace")] = blob
    ref = f"{info['repository']}@{commit}"
    sources.add("repository_tree", ref, sha256_text(json.dumps(tree, sort_keys=True)), entries=len(tree))
    chosen = named_paths(info["instruction"], sorted(blobs))
    inits = sorted((p for p in blobs if posixpath.basename(p) == "__init__.py"), key=lambda p: (p.count("/"), p))
    files = []
    for path in dict.fromkeys([*chosen, *inits]):
        if tree[path] > config.settings.view.max_file_bytes:
            omitted.append({"path": path, "reason": "over max_file_bytes"})
            continue
        raw = git(store, "cat-file", "blob", blobs[path])
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            omitted.append({"path": path, "reason": "not UTF-8"})
            continue
        files.append({"path": path, "sha256": sha256_bytes(raw), "content": text})
        sources.add("repository_file", f"{ref}:{path}", sha256_bytes(raw), blob=blobs[path], bytes=len(raw))
    history = []
    if config.settings.view.history_commits:
        count = str(config.settings.view.history_commits)
        history = git(store, "log", "-n", count, "--format=%h %s", commit).decode("utf-8", "replace").splitlines()
    sources.add("repository_history", ref, sha256_text("\n".join(history)), commits=len(history))
    return {"base_commit": commit, "tree": tree, "files": files, "history": history, "omitted": list(omitted)}


def cut(line: str, limit: int) -> str:
    return line if len(line) <= limit else line[:limit] + f" ...(+{len(line) - limit} characters)"


def trim_output(text: str | None, limit: int) -> str:
    """The first and last EDGE lines of a command output; a short output is shown whole, with no overlap."""
    lines = (text or "").splitlines()
    if len(lines) > 2 * EDGE:
        lines = [*lines[:EDGE], f"... ({len(lines) - 2 * EDGE} lines omitted) ...", *lines[-EDGE:]]
    return "\n".join(cut(line, limit) for line in lines)


def blocks_text(message: dict, kind: str) -> list[str]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [
        b[kind].strip()
        for b in content
        if isinstance(b, dict) and b.get("type") == kind and (b.get(kind) or "").strip()
    ]


def render_session(events: list[dict], agent_id: str, limit: int) -> str:
    """The agent's side of one session in event order, built by event kind: thinking summaries, replies, executed
    commands, trimmed command outputs and the agent's own memory writes. Every other event kind (the guard's findings,
    judgments and notices, interventions, context, tokens, cost) has no branch here and so cannot appear."""
    out: list[str] = []
    step = None

    def header(number) -> None:
        nonlocal step
        if number != step:
            step = number
            out.append(f"[step {number}]")

    for event in events:
        if event.get("agent_id", "main") != agent_id:
            continue
        kind = event.get("kind")
        if kind == "model_result":
            message = event.get("message") or {}
            thinking = "\n".join(blocks_text(message, "thinking")) or reasoning_text(message).strip()
            reply = message_text([message]).strip()
            if thinking:
                header(event.get("step"))
                out.append("thinking: " + thinking)
            if reply:
                header(event.get("step"))
                out.append("reply: " + reply)
        elif kind == "action":
            header(event.get("step"))
            out.append("command: " + str(event.get("command") or "").strip())
        elif kind == "raw_output":
            header(event.get("step"))
            notes = []
            if event.get("returncode") not in (0, None):
                notes.append(f"exit status {event['returncode']}")
            if event.get("exception_info"):
                notes.append("exception: " + str(event["exception_info"])[:200])
            tail = f" ({'; '.join(notes)})" if notes else ""
            body = trim_output(event.get("output"), limit)
            out.append(f"output{tail}:\n{body}" if body else f"output{tail}: (empty)")
        elif kind == "memory_change" and event.get("origin") == "agent":
            header(event.get("step"))
            before, after = event.get("before"), event.get("after")
            what = "deleted" if after is None else "created" if before is None else "changed"
            out.append(f"memory write ({what}) {event.get('path')}" + ("" if after is None else f":\n{after}"))
    return "\n\n".join(out) + "\n"


def read_events(raw: bytes) -> list[dict]:
    try:
        return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    except (ValueError, UnicodeDecodeError) as error:
        raise InputError(f"events.jsonl cannot be parsed: {error}", "bad_session") from None


def chain_sessions(req: Request) -> list[Path]:
    """The directories of the sessions before the next one, in order, from this chain's arm directory alone."""
    parent = req.arm_dir / "sessions"
    found = []
    for position in range(req.number):
        matches = sorted(parent.glob(f"{position + 1:03d}-*")) if parent.is_dir() else []
        if len(matches) != 1 or not matches[0].is_dir() or matches[0].is_symlink():
            raise InputError(f"the chain has no single session directory for position {position + 1}", "bad_session")
        found.append(matches[0])
    if req.finished is not None and Path(str(req.finished.get("dir"))).resolve() != found[-1].resolve():
        raise InputError("finished_session.dir is not the chain's last session", "bad_session")
    return found


def completed_sessions(req: Request, config: WriterConfig, sources: Sources) -> list[dict]:
    entries = []
    for position, directory in enumerate(chain_sessions(req)):
        raw = {name: sources.read("session", directory / name, directory) for name in FILE_KINDS["session"]}
        try:
            result = json.loads(raw["result.json"])
            memory_end = json.loads(raw["memory_end.json"])
        except ValueError as error:
            raise InputError(f"{directory}: a session record cannot be parsed: {error}", "bad_session") from None
        task_id = result.get("task_id") if isinstance(result, dict) else None
        if not isinstance(task_id, str) or not isinstance(memory_end, dict):
            raise InputError(
                f"{directory}: result.json names no task or memory_end.json is not a file map", "bad_session"
            )
        if req.finished is not None and position == req.number - 1 and task_id != req.finished.get("task_id"):
            raise InputError("the last session's task differs from the request's finished_session", "bad_session")
        task = task_info(req.task_dir.parent / task_id, sources, full=False)
        for path in outcome_sources(directory):
            if path.is_file():
                sources.add("outcome_source", str(path), sha256_bytes(path.read_bytes()), used_for="two outcome fields")
        carried = {
            p: t for p, t in (loaded_by(memory_end, req.project) or {}).items() if p != USER and isinstance(t, str)
        }
        entries.append(
            {
                "index": position,
                "task": task,
                "agent_session": render_session(
                    read_events(raw["events.jsonl"]), req.agent_id, config.settings.view.line_chars
                ),
                "memory_at_end": dict(sorted(carried.items())),
                "outcomes": session_outcomes(req.campaign_dir, directory, directory.name.split("-", 1)[1]),
            }
        )
    return entries


def assignment_message(envelope: dict, last_write: str | None) -> str:
    """The user message: the envelope, and for the step before a frozen session the last-write sentence verbatim."""
    text = json.dumps(envelope, ensure_ascii=False, indent=2)
    return text if last_write is None else f"{text}\n\n{last_write}"


def build_view(
    req: Request,
    config: WriterConfig,
    limits: AdaptiveLimits,
    notebook: str,
) -> View:
    sources, omitted = Sources(), []
    info = task_info(req.task_dir, sources, full=True)
    store = config.settings.repository_stores.get(info["repository"])
    if store is None:
        raise InputError(f"no repository store is configured for {info['repository']}", "bad_store")
    envelope = {
        "job": "memory",
        "task": info,
        "repository": repository_view(info, Path(store), config, sources, omitted),
        "objective": config.objective,
        "memory_root": req.memory_dir,
        "memory_files": req.memory_files,
        "owned_paths": req.owned,
        "limits": {"max_files_touched": limits.max_files_touched, "max_file_bytes": limits.max_file_bytes},
        "notebook_max_utf8_bytes": config.notebook_max_bytes,
        "notebook": notebook,
        "completed_sessions": completed_sessions(req, config, sources),
    }
    for path, text in req.memory_files.items():
        sources.add("memory_file", path, sha256_text(text), origin="current carried memory")
    sources.add("notebook", "state/writer/notebook.json", sha256_text(notebook))
    sources.add("assignment", "assignment fragment", sha256_text(config.objective))
    if req.last_write and config.last_write is None:
        raise ConfigError("the request carries last_write but the design has no last-write sentence", "last_write")
    messages = [
        {"role": "system", "content": config.system_prompt},
        {"role": "user", "content": assignment_message(envelope, config.last_write if req.last_write else None)},
    ]
    check_size(config, messages)
    return View(
        envelope=envelope,
        messages=messages,
        sources=sources.entries,
        omitted=omitted,
        messages_sha256=sha256_bytes(encode({"messages": messages})),
        files=sources.files,
    )


def check_size(config: WriterConfig, messages: list[dict]) -> int:
    """The request body's size in UTF-8 bytes; an input over the cap is refused, never cut."""
    size = len(encode(request_body(config.api, messages)))
    if size > config.settings.max_input_bytes:
        raise InputError(
            f"the request is {size} bytes, over the cap of {config.settings.max_input_bytes}", "input_too_large"
        )
    return size
