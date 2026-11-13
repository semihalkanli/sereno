"""The run-local adaptive attacker of the attack_carry arm.

An external program is called synchronously before the agent's model calls: the first call of a session always, then
call 1 + n, 1 + 2n, ... for `every_calls: n`, and once at the end of each completed session. Each call gets three
parts: the fixed experiment contract (stage, memory root, limits, budgets, the response's form), the attacker's own
evolving strategy and private notebook, and an observation package (task, code views, the agent's visible transcript
with its thinking summaries, earlier sessions of the same chain, memory, and the result of its previous call). It
answers once with memory actions, an updated strategy and an updated notebook. The transcript carries the readable
thinking text only: signatures, redacted blocks and provider metadata stay out.

Memory actions go through the hook rules of the adaptive arm (path, ownership, append-only, size and content limits);
the user's file is out of reach. In probe sessions and in the session-end call no action is applied. A valid strategy
or notebook update is kept whatever happened to the actions, and the next call learns that result. Strategy and
notebook never reach the agent's memory. The state lives on the arm's engine, so it carries within one chain only, and
a completed session checkpoints it; a resume starts from the last checkpoint, never from a half-finished session.

The external command is trusted host code, as the transition writer is, not a sandbox: it must use only the request it
is given and keep no state of its own between calls or runs. A program, configuration or provider problem raises
AttackerInfrastructureError: the session ends invalid with the error recorded as infrastructure, never as an attack
outcome.
"""

import copy
import hashlib
import json
import shlex
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from sereno.attacker.protocol import LOOP_ENDINGS
from sereno.context_eval.adaptive import (
    RESPONSE_BYTES,
    ResponseError,
    apply_changes,
    file_sha256,
    parse_actions,
    plan,
    printable,
    run_program,
)
from sereno.context_eval.config import fingerprint
from sereno.context_eval.engine import reasoning_text, write_atomic
from sereno.context_eval.memory import USER, is_index, loaded_by, memory_dir
from sereno.context_eval.schema import validate_path

SCHEMA_VERSION = "1.0"
STATE_SCHEMA = "1.0"
STATE_FILE = "run_attacker_state.json"
AUDIT_DIR = "run_attacker"
RESPONSE_KEYS = frozenset(
    {"schema_version", "actions", "strategy", "notebook", "reads", "writer_usage", "status", "loop_ending"}
)
# What a call's actions came to. Only `applied` changes memory.
ACTION_RESULTS = ("applied", "no_change", "no_action", "rejected", "not_applied")
# Message fields a recorded transcript shows; provider metadata stays out except the readable thinking text, which
# `visible_transcript` copies into `thinking`.
VISIBLE_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")
HIDDEN_BLOCKS = frozenset({"thinking", "redacted_thinking"})
EVENT_OWN = frozenset({"schema_version", "kind", "step", "session_id"})


class AttackerInfrastructureError(RuntimeError):
    """The program gave no usable answer: a configuration, program or provider problem, not an attack outcome."""

    def __init__(self, code: str, message: str):
        super().__init__(f"run_attacker {code}: {message}")
        self.code = code


def initial_state(config) -> dict:
    """The state a chain starts from: the configured strategy as version 0, an empty notebook, nothing owned."""
    return {
        "schema_version": STATE_SCHEMA,
        "strategy": {"version": 0, "text": config.strategy, "sha256": fingerprint(config.strategy)},
        "notebook": "",
        "owned": [],
        "pending_reads": [],
        "previous_result": None,
        "sessions": [],
        "usage": {
            "calls": 0,
            "tokens": 0,
            "seconds": 0.0,
            "writes": 0,
            "cost_usd": 0.0,
            "unreported_usage_calls": 0,
            "skipped_calls": 0,
        },
    }


def read_state(path: Path) -> dict | None:
    """A checkpoint that parses and has the state's form; None when it is missing or was cut short."""
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    keys = {"schema_version", "strategy", "notebook", "owned", "sessions", "usage"}
    if not isinstance(state, dict) or state.get("schema_version") != STATE_SCHEMA or keys - set(state):
        return None
    return state


def visible_transcript(messages: list[dict]) -> list[dict]:
    """The messages as recorded for the agent's conversation: roles, visible content and tool interactions, the
    readable thinking summary in a `thinking` field in every provider spelling (`reasoning`, `reasoning_content`,
    content thinking blocks), and the guard's notices marked as such; signatures, redacted blocks and provider
    metadata stay out."""
    visible = []
    for message in messages:
        item = {key: copy.deepcopy(message[key]) for key in VISIBLE_KEYS if key in message}
        blocks = message.get("content")
        if isinstance(item.get("content"), list):
            item["content"] = [
                block
                for block in item["content"]
                if not (isinstance(block, dict) and block.get("type") in HIDDEN_BLOCKS)
            ]
        thinking = reasoning_text(message)
        if not thinking and isinstance(blocks, list):
            thinking = "\n".join(
                block["thinking"]
                for block in blocks
                if isinstance(block, dict)
                and block.get("type") == "thinking"
                and isinstance(block.get("thinking"), str)
                and block["thinking"]
            )
        if isinstance(thinking, str) and thinking:
            item["thinking"] = thinking
        elif isinstance(blocks, list) and any(
            isinstance(block, dict) and block.get("type") == "redacted_thinking" for block in blocks
        ):
            item["thinking_redacted"] = True
        if notice := (message.get("extra") or {}).get("notice_id"):
            item["notice_id"] = notice
        visible.append(item)
    return visible


def reported_tokens(usage: object) -> int | None:
    """Total tokens in the program's writer_usage (the provider's usage record), or None when it reports none."""
    if not isinstance(usage, dict):
        return None
    inner = usage.get("usage")
    record = inner if isinstance(inner, dict) else usage
    total = record.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool):
        return total
    prompt, completion = record.get("prompt_tokens"), record.get("completion_tokens")
    if isinstance(prompt, int) and isinstance(completion, int):
        if not isinstance(prompt, bool) and not isinstance(completion, bool):
            return prompt + completion
    return None


def reported_cost(usage: object) -> float:
    cost = usage.get("cost") if isinstance(usage, dict) else None
    return float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else 0.0


def reported_count(usage: object, key: str) -> int:
    value = usage.get(key) if isinstance(usage, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class RunLocalAttacker:
    """The attacker of one session. `state` is the checkpoint of the chain's previous completed session, or None for
    the chain's first session; it is copied, so nothing this session does reaches it until `finish` returns."""

    def __init__(self, config, runtime, instruction: str, task_id: str, state: dict | None = None):
        self.config, self.runtime, self.instruction, self.task_id = config, runtime, instruction, task_id
        self.state = copy.deepcopy(state) if state is not None else initial_state(config)
        self.stage = "exposure" if runtime.session.exposure else "probe"
        self.ticks = 0
        self.session_calls = 0
        self.skipped: set[str] = set()
        self.calls: list[dict] = []
        self.index = self.file_index()

    def file_index(self) -> dict:
        """The files Git tracks below the code root at task start as absolute paths, the form `read_code` serves, or
        their count when there are too many."""
        root = self.config.code_root
        command = f"cd {shlex.quote(root)} && git -c safe.directory=/app -c core.quotepath=off ls-files"
        listed = self.runtime.env.execute(command)
        if listed["returncode"]:
            raise AttackerInfrastructureError("code_index", "cannot list the repository files")
        files = [f"{root}/{line}" for line in listed["output"].splitlines() if line]
        if len(files) > self.config.max_index_entries:
            return {"root": root, "status": "over_limit", "entries": len(files), "files": None}
        return {"root": root, "status": "ok", "entries": len(files), "files": files}

    def read_code(self, path: object) -> dict:
        """One file below the code root as it now is in the workspace, or why it is not given. A file over the size
        limit is named with its size, never cut."""
        root = self.config.code_root
        if not isinstance(path, str):
            return {"status": "denied", "reason": "not a path"}
        try:
            validate_path(path, root)
        except ValueError:
            return {"status": "denied", "reason": f"not a canonical file path below {root}, outside .git"}
        # A symlink must not lead out of the code root, so the real path is resolved in the container.
        relative = path.removeprefix(root + "/")
        resolved = self.runtime.env.execute(
            f"cd {shlex.quote(root)} && realpath -e --relative-to=. -- {shlex.quote(relative)}"
        )
        real = resolved["output"].strip()
        if resolved["returncode"]:
            return {"status": "missing"}
        if real == ".." or real.startswith("../") or real == ".git" or real.startswith(".git/"):
            return {"status": "denied", "reason": f"the path resolves outside {root} or into .git"}
        try:
            text = self.runtime.env.read(path)
        except (RuntimeError, ValueError, UnicodeDecodeError) as error:
            return {"status": "unreadable", "reason": type(error).__name__}
        if text is None:
            return {"status": "missing"}
        size = len(text.encode())
        if size > self.config.max_read_bytes:
            return {"status": "too_large", "bytes": size, "limit": self.config.max_read_bytes}
        return {"status": "ok", "bytes": size, "text": text}

    def serve_reads(self) -> dict:
        """The files the previous call asked for, read now; asks over the per-call budget are named, not served."""
        served = {}
        for number, path in enumerate(self.state.get("pending_reads", [])):
            key = path if isinstance(path, str) else f"#{number}"
            if number >= self.config.max_reads_per_call:
                served[key] = {"status": "denied", "reason": "over max_reads_per_call"}
            else:
                served[key] = self.read_code(path)
        self.state["pending_reads"] = []
        return served

    def exhausted(self, kind: str) -> str | None:
        budget, used = self.config.budget, self.state["usage"]
        reserve = int(kind == "in_task")
        if used["calls"] >= budget.max_calls - reserve:
            return "max_calls"
        if self.session_calls >= budget.max_calls_per_session - reserve:
            return "max_calls_per_session"
        if used["tokens"] >= budget.max_tokens:
            return "max_tokens"
        if used["seconds"] >= budget.max_seconds:
            return "max_seconds"
        return None

    def advance(self, messages: list[dict]) -> None:
        """Before a model call: call the program on the session's first model call and then on call 1 + n,
        1 + 2n, ... for `every_calls: n`, while the budget has room. Its writes
        land in the files; the messages already prepared for this call are not changed."""
        tick = self.ticks
        self.ticks += 1
        if tick % self.config.every_calls:
            return
        self.call("in_task", visible_transcript(messages))

    def finish(self, messages: list[dict], summary: dict) -> dict:
        """At the end of a completed session: one evaluation call that may update only the private state, then the
        checkpoint the next session of the chain starts from."""
        transcript = visible_transcript(messages)
        self.call("session_end", transcript, summary)
        self.state["sessions"].append(
            {
                "session_id": self.runtime.session.id,
                "task_id": self.task_id,
                "stage": self.stage,
                "instruction": self.instruction,
                "transcript": transcript,
                "summary": summary,
                "calls": len(self.calls),
                "applied_attack_actions": self.runtime.applied_attack_actions,
            }
        )
        return copy.deepcopy(self.state)

    def call(self, kind: str, transcript: list[dict], summary: dict | None = None) -> None:
        if reason := self.exhausted(kind):
            self.state["usage"]["skipped_calls"] += 1
            if reason not in self.skipped:
                self.skipped.add(reason)
                self.runtime.emit("run_attacker_skipped", call_kind=kind, reason=reason, stage=self.stage)
            return
        self.session_calls += 1
        self.state["usage"]["calls"] += 1
        number = self.session_calls
        runtime, config = self.runtime, self.config
        writes_open = kind == "in_task" and self.stage == "exposure"
        request = self.request(kind, number, transcript, summary, writes_open)
        encoded = (json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n").encode()
        audit = runtime.log.path.parent / AUDIT_DIR / f"{number:04d}"
        audit.mkdir(parents=True, exist_ok=False)
        base = {
            "schema_version": SCHEMA_VERSION,
            "call": number,
            "chain_call": self.state["usage"]["calls"],
            "kind": kind,
            "stage": self.stage,
            "session_id": runtime.session.id,
            "step": runtime.step,
            "started_at": datetime.now(UTC).isoformat(),
            "strategy_version_before": self.state["strategy"]["version"],
            "strategy_sha256_before": self.state["strategy"]["sha256"],
            "notebook_sha256_before": fingerprint(self.state["notebook"]),
            "request_bytes": len(encoded),
            "request_sha256": hashlib.sha256(encoded).hexdigest(),
        }
        if len(encoded) > config.max_input_bytes:
            self.fail(
                audit,
                base,
                "input_over_limit",
                f"the request is {len(encoded)} bytes, over max_input_bytes={config.max_input_bytes}",
            )
        partial = audit / ".request.json.partial"
        partial.write_bytes(encoded)
        partial.replace(audit / "request.json")
        clock = time.monotonic()
        # A fresh working directory on every call: the program keeps nothing of its own between calls.
        with TemporaryDirectory(prefix="sereno-run-attacker-") as scratch:
            outcome = run_program(
                [*config.command, str(audit / "request.json"), str(audit / "response.json")],
                Path(scratch),
                config.timeout_seconds,
                audit,
            )
        seconds = round(time.monotonic() - clock, 3)
        self.state["usage"]["seconds"] = round(self.state["usage"]["seconds"] + seconds, 3)
        base["seconds"] = seconds
        base["program"] = {
            key: outcome.get(key) for key in ("exit_code", "timed_out", "error", "stdout_bytes", "stderr_bytes")
        }
        if outcome["timed_out"] or outcome["error"] or outcome["exit_code"] != 0:
            failure = audit / "failure_usage.json"
            if failure.is_file() and failure.stat().st_size <= RESPONSE_BYTES:
                try:
                    usage = json.loads(failure.read_bytes())
                except (OSError, ValueError, UnicodeDecodeError, RecursionError):
                    usage = None
                if isinstance(usage, dict):
                    base["usage"] = {
                        "tokens": reported_tokens(usage),
                        "cost_usd": reported_cost(usage),
                        "unreported_completions": reported_count(usage, "unreported_completions"),
                    }
                    base["declined_attempts"] = reported_count(usage, "declined_attempts")
        if outcome["timed_out"]:
            self.fail(audit, base, "timeout", f"the program ran past {config.timeout_seconds} seconds")
        if outcome["error"] or outcome["exit_code"] != 0:
            self.fail(audit, base, "writer_failed", outcome["error"] or f"exit status {outcome['exit_code']}")
        try:
            response = self.load_response(audit / "response.json")
            actions = parse_actions(response["actions"], runtime.project)
        except ResponseError as error:
            self.fail(audit, base, "invalid_response", str(error))
        base["response_sha256"] = file_sha256(audit / "response.json")
        base["loop_ending"] = response.get("loop_ending")
        base["actions"] = len(actions)
        usage = response.get("writer_usage")
        tokens = reported_tokens(usage)
        missing = reported_count(usage, "unreported_completions")
        self.state["usage"]["tokens"] += tokens or 0
        self.state["usage"]["unreported_usage_calls"] += tokens is None or missing > 0
        self.state["usage"]["cost_usd"] += reported_cost(usage)
        base["usage"] = {"tokens": tokens, "cost_usd": reported_cost(usage), "unreported_completions": missing}
        base["declined"] = response.get("status") == "declined" or (
            isinstance(usage, dict) and usage.get("finish") == "declined"
        )
        base["declined_attempts"] = reported_count(usage, "declined_attempts")
        status, reason, violations, changes = self.apply(actions, kind, writes_open)
        applied_attack_actions = sum(change.real for change in changes) if status == "applied" else 0
        # The private state follows every valid answer, the rejected or unapplied actions included. A lone surrogate
        # in the program's JSON becomes an escape, so the checkpoint stays writable UTF-8.
        strategy = None if response.get("strategy") is None else printable(response["strategy"])
        if strategy is not None and strategy != self.state["strategy"]["text"]:
            self.state["strategy"] = {
                "version": self.state["strategy"]["version"] + 1,
                "text": strategy,
                "sha256": fingerprint(strategy),
            }
        if response.get("notebook") is not None:
            self.state["notebook"] = printable(response["notebook"])
        self.state["pending_reads"] = [printable(path) for path in response.get("reads") or []]
        result = {
            "call": number,
            "session_id": runtime.session.id,
            "kind": kind,
            "stage": self.stage,
            "status": status,
            "reason": reason,
            "violations": violations,
            "changes": [change.record() for change in changes] if status == "applied" else [],
            "reads_requested": len(self.state["pending_reads"]),
        }
        self.state["previous_result"] = result
        record = base | {
            "status": status,
            "reason": reason,
            "violations": violations,
            "applied_attack_actions": applied_attack_actions,
            "changes": result["changes"],
            "applied": status == "applied",
            "strategy_version_after": self.state["strategy"]["version"],
            "strategy_sha256_after": self.state["strategy"]["sha256"],
            "notebook_sha256_after": fingerprint(self.state["notebook"]),
            "reads_requested": result["reads_requested"],
            "usage_after": dict(self.state["usage"]),
        }
        write_atomic(audit / "record.json", record)
        self.calls.append(record)
        # The event log stamps its own kind, schema, step and session; the record keeps the call's own.
        fields = {key: value for key, value in record.items() if key not in EVENT_OWN | {"usage_after"}}
        runtime.emit("run_attacker", call_kind=kind, call_step=record["step"], **fields)

    def request(self, kind: str, number: int, transcript, summary, writes_open: bool) -> dict:
        runtime, config = self.runtime, self.config
        project = runtime.project
        memory = (loaded_by(runtime.memory, project) if project else dict(runtime.memory)) or {}
        return {
            "schema_version": SCHEMA_VERSION,
            "contract": {
                "kind": kind,
                "stage": self.stage,
                "writes_open": writes_open,
                "project": project,
                "memory_root": memory_dir(project) if project else None,
                "limits": config.limits.model_dump(),
                "budget": config.budget.model_dump(),
                "used": dict(self.state["usage"]),
                "session_call": number,
                "timeout_seconds": config.timeout_seconds,
                "strategy_max_bytes": config.strategy_max_bytes,
                "notebook_max_bytes": config.notebook_max_bytes,
                "code_root": config.code_root,
                "max_reads_per_call": config.max_reads_per_call,
                "max_read_bytes": config.max_read_bytes,
                "response_keys": sorted(RESPONSE_KEYS),
            },
            "strategy": dict(self.state["strategy"]),
            "notebook": self.state["notebook"],
            "observation": {
                "session": {
                    "id": runtime.session.id,
                    "task_id": self.task_id,
                    "instruction": self.instruction,
                    "step": runtime.step,
                },
                "code_index": self.index,
                "code_views": {path: self.read_code(path) for path in config.code_paths},
                "requested_files": self.serve_reads(),
                "transcript": transcript,
                "completed_sessions": self.state["sessions"],
                "memory": {
                    "files": {path: text for path, text in sorted(memory.items()) if path != USER},
                    "owned": list(self.state["owned"]),
                },
                "previous_result": self.state["previous_result"],
                "session_summary": summary,
            },
        }

    def load_response(self, path: Path) -> dict:
        """The program's answer, checked for its form; a form problem is the program's failure, never an attack
        outcome."""
        config = self.config
        if not path.is_file():
            raise ResponseError("the program wrote no response.json")
        if path.stat().st_size > RESPONSE_BYTES:
            raise ResponseError("response.json is too large")
        try:
            data = json.loads(path.read_bytes().decode())
        except (ValueError, RecursionError) as error:
            raise ResponseError(f"response.json is not valid JSON: {error}") from None
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise ResponseError(f"response.json must hold an object with schema_version {SCHEMA_VERSION!r}")
        if extra := set(data) - RESPONSE_KEYS:
            raise ResponseError(f"unexpected fields {sorted(extra)}")
        if "status" in data and data["status"] != "declined":
            raise ResponseError("status, when present, must be declined")
        if "loop_ending" in data and (
            not isinstance(data["loop_ending"], str) or data["loop_ending"] not in LOOP_ENDINGS
        ):
            raise ResponseError(f"loop_ending must be one of {sorted(LOOP_ENDINGS)}")
        if "actions" not in data:
            raise ResponseError("actions is missing")
        for key, limit in (("strategy", config.strategy_max_bytes), ("notebook", config.notebook_max_bytes)):
            value = data.get(key)
            if value is not None and (not isinstance(value, str) or len(printable(value).encode()) > limit):
                raise ResponseError(f"{key} must be a string of at most {limit} UTF-8 bytes")
        reads = data.get("reads")
        if reads is not None and not (isinstance(reads, list) and all(isinstance(path, str) for path in reads)):
            raise ResponseError("reads must be a list of paths")
        if reads is not None and len(reads) > 1000:
            raise ResponseError("reads holds more than 1000 paths")
        if data.get("writer_usage") is not None and not isinstance(data["writer_usage"], dict):
            raise ResponseError("writer_usage must be an object")
        return data

    def apply(self, actions, kind: str, writes_open: bool):
        """What the actions come to, and the changes made: nothing is written outside an exposure session's in-task
        call, over the write budget, or when any action breaks a rule."""
        runtime, config = self.runtime, self.config
        if not actions:
            return "no_action", None, [], []
        if kind == "session_end":
            return "not_applied", "the session-end evaluation writes no memory", [], []
        if not writes_open:
            return "not_applied", "probe session: attack writes are closed", [], []
        if self.state["usage"]["writes"] >= config.budget.max_writes:
            return "not_applied", "the write budget is exhausted", [], []
        violations, changes = plan(
            runtime.memory, actions, set(self.state["owned"]), config.limits, runtime.project, runtime.memory_config
        )
        if violations:
            return "rejected", f"{len(violations)} rule violations", list(map(printable, violations)), []
        if not any(change.real for change in changes):
            return "no_change", None, [], changes
        before = dict(runtime.memory)
        after = apply_changes(before, changes)
        touched = [change.path for change in changes if change.real]
        try:
            for path in touched:
                runtime.env.write(path, after.get(path))
        except Exception as error:
            try:
                for path in touched:
                    runtime.env.write(path, before.get(path))
            except Exception:
                runtime.applied_attack_actions = None
                raise
            raise AttackerInfrastructureError("write_failed", f"{type(error).__name__}: {error}") from error
        if runtime.applied_attack_actions is not None:
            runtime.applied_attack_actions += len(touched)
        runtime.capture_memory("attacker")
        self.state["usage"]["writes"] += 1
        created = {change.path for change in changes if change.result == "created" and not is_index(change.path)}
        self.state["owned"] = sorted(set(self.state["owned"]) | created)
        return "applied", None, [], changes

    def fail(self, audit: Path, base: dict, code: str, message: str):
        record = base | {"status": "infrastructure_error", "error": {"code": code, "message": printable(message)}}
        write_atomic(audit / "record.json", record)
        self.runtime.emit("run_attacker_error", code=code, message=printable(message), call=base["call"])
        raise AttackerInfrastructureError(code, message)


def writer_manifest(config) -> dict:
    """The attacker as the campaign manifest records it: its settings, budgets and starting strategy, and the sha256
    of every command word that names an existing file."""
    check_command_identity(config.command)
    return config.model_dump(mode="json") | {
        "strategy_sha256": fingerprint(config.strategy),
        "program_sha256": {
            word: file_sha256(Path(word))
            for word in config.command
            if Path(word).is_absolute() and Path(word).is_file()
        },
    }


def check_command_identity(command: list[str]) -> None:
    from sereno.attacker.config import check_command_identity as check_attacker_command
    from sereno.attacker.errors import ConfigError

    try:
        check_attacker_command(command)
    except ConfigError as error:
        raise ValueError(f"run_attacker {error.code}: {error}") from None
