"""The run-local adaptive attacker of the attack_carry arm.

An external program is called synchronously before the agent's model calls: the first call of a session, budget
permitting, then call 1 + n, 1 + 2n, ... for `every_calls: n`, and once at the end of each completed session. Each
call gets three parts: the fixed experiment contract (stage, memory root, limits, budgets, the response's form), the
attacker's own evolving strategy and private notebook, and an observation package (task, code views, the agent's
visible transcript with its thinking summaries, earlier sessions of the same chain, memory, and the result of its
previous call). It answers once with memory actions, an updated strategy and an updated notebook. The transcript
carries the readable thinking text only: signatures, redacted blocks and provider metadata stay out, and the guard's
notices are withheld whole (decision 518 item 2, the 2026-10-22 rule), so a warning reaches the attacker only through
the target's own text.

Memory actions go through the hook rules of the adaptive arm (path, ownership, append-only, size and content limits);
the user's file is out of reach. In probe sessions and in the session-end call no action is applied. A valid strategy
or notebook update is kept whatever happened to the actions, and the next call learns that result. Strategy and
notebook never reach the agent's memory. The state lives on the arm's engine, so it carries within one chain only, and
a completed session checkpoints it; a resume starts from the last checkpoint, never from a half-finished session.

The external command is trusted host code, as the transition writer is, not a sandbox. The harness enforces the part
of the boundary it can: the program's argv, its working directory and an environment built from an allowlist, with
every forwarded absolute path refused inside the campaign's run tree; a fresh scratch directory under a checked
temporary base outside the run tree; a bounded, non-following copy of what the program wrote; and a private cost
ledger file per call, whose rows the harness appends to the wrapper's own ledger after the call. What remains is the
trusted-host contract: the program runs as the same user as the harness, the host's process metadata still names the
run tree, and a program that walks the filesystem by absolute path can reach it. It must use the request it is given
and keep no state of its own between calls or runs. A program, configuration or provider problem raises
AttackerInfrastructureError: the session ends invalid with the error recorded as infrastructure, never as an attack
outcome.
"""

import contextlib
import copy
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import IO, NoReturn

import yaml

from sereno.attacker.protocol import LOOP_ENDINGS
from sereno.context_eval.adaptive import (
    CAPTURE_BYTES,
    RESPONSE_BYTES,
    ResponseError,
    apply_changes,
    file_sha256,
    parse_actions,
    plan,
    printable,
    run_program,
)
from sereno.context_eval.config import config_identity, fingerprint, without_empty_env
from sereno.context_eval.engine import reasoning_text, write_atomic
from sereno.context_eval.memory import USER, is_index, loaded_by, memory_dir
from sereno.context_eval.schema import ENV_NAME, harness_controlled, validate_path

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
# What a call's scratch directory can hold and the audit folder receives once the program has exited.
CALL_FILES = ("response.json", "failure_usage.json", "writer.stdout", "writer.stderr")


class AttackerInfrastructureError(RuntimeError):
    """The program gave no usable answer: a configuration, program or provider problem, not an attack outcome."""

    def __init__(self, code: str, message: str):
        super().__init__(f"run_attacker {code}: {message}")
        self.code, self.message = code, message


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


def is_guard_notice(message: object) -> bool:
    """A guard notice: the engine's mid-conversation system message carries `extra.notice_id`, and a transcript
    recorded before notices were withheld carries the same mark at the top level of the recorded message. The mark's
    presence is the test, not its value: an empty id is still a notice and never renders as ordinary text."""
    if not isinstance(message, dict):
        return False
    extra = message.get("extra")
    return "notice_id" in message or (isinstance(extra, dict) and "notice_id" in extra)


def without_notices(sessions: list) -> int:
    """Guard notices dropped from the chain's recorded session transcripts in place, as a count; every other message
    keeps its bytes and its order, so nothing names the removed messages."""
    dropped = 0
    for session in sessions:
        if not isinstance(session, dict) or not isinstance(session.get("transcript"), list):
            continue
        kept = [message for message in session["transcript"] if not is_guard_notice(message)]
        dropped += len(session["transcript"]) - len(kept)
        session["transcript"] = kept
    return dropped


def visible_transcript(messages: list[dict]) -> list[dict]:
    """The messages as recorded for the agent's conversation: roles, visible content and tool interactions, and the
    readable thinking summary in a `thinking` field in every provider spelling (`reasoning`, `reasoning_content`,
    content thinking blocks). Guard notices are left out whole; signatures, redacted blocks and provider metadata stay
    out."""
    visible = []
    for message in messages:
        if is_guard_notice(message):
            continue
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


# The shell's own statuses: 126 cannot execute, 127 not found, and 128 + the signal when the command was killed.
UNAVAILABLE = frozenset({126, 127})
SIGNAL_BASE = 128
SIGNAL_MAX = 64


def did_not_finish(result: dict) -> bool:
    """True when the container did not run the command to its own exit status: a timeout, a kill or a host error."""
    if result.get("exception_info"):
        return True
    code = result["returncode"]
    return code < 0 or code in UNAVAILABLE or SIGNAL_BASE < code <= SIGNAL_BASE + SIGNAL_MAX


# What the program's environment may carry: these names, the live-mode proxy and CA variables, the LC_* family, the
# provider credential variables the attacker configuration names and the variables the campaign lists. Nothing else is
# inherited, PWD and OLDPWD included, and the temporary-directory variables point at the call's scratch.
CHILD_ENV_NAMES = ("PATH", "HOME", "LANG", "TZ")
CHILD_ENV_PREFIXES = ("LC_",)
# A live program reaches its provider through `httpx`, which reads the proxy variables and OpenSSL's
# `SSL_CERT_FILE`/`SSL_CERT_DIR`; `REQUESTS_CA_BUNDLE` is forwarded for programs on `requests`. Neither case needs a
# campaign entry, and all of them cross the same containment check as every other forwarded value.
LIVE_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
)
CHILD_TEMP_NAMES = ("TMPDIR", "TEMP", "TMP")
# Variables whose value is a list of paths rather than one; each entry is checked against the run tree on its own.
# `LD_*` names are harness-controlled, so `LD_LIBRARY_PATH` cannot reach the program and is not listed here.
PATH_VALUE_NAMES = frozenset({"PATH", "PYTHONPATH", "MANPATH", "CDPATH", "PKG_CONFIG_PATH"})
# The cost wrapper's meter: the ledger it appends each paid call to and the hook directory that records it. A harness
# running under `scripts/cost.py run` has both in its own environment. The program is given neither path: it writes a
# private file in its scratch, and the harness appends that file's rows to the ledger after the call.
COST_CALLS_NAME = "SERENO_COST_CALLS"
COST_CALLS_FILE = "cost_calls.jsonl"
COST_HOOK_MODULE = "sitecustomize.py"
COST_HOOK_MARKER = "SERENO_COST_CALLS"
COST_HOOK_DIR = Path(__file__).resolve().parents[3] / "scripts" / "cost_hook"
# The most the credential configuration is read for: an attacker configuration or a writer design is far smaller.
CONFIG_BYTES = 1_000_000
# How much of a staged file one read returns; the copy stops once `limit + 1` bytes have been read.
COPY_CHUNK = 65536


def forwarded_paths(name: str, value: str) -> list[str]:
    """The paths a forwarded value names: a PATH-like variable is a `os.pathsep` list, and any other value that splits
    into more than one non-empty entry is read as one too, so a list mixing a relative and an absolute entry cannot
    carry a path the check would skip. A single entry is one value, checked whole when it is absolute."""
    entries = [entry for entry in value.split(os.pathsep) if entry]
    if name in PATH_VALUE_NAMES or len(entries) > 1:
        return entries
    return [value]


def refuse_inside_run_tree(name: str, value: str, root: Path) -> None:
    """Refuse a forwarded value that resolves inside the campaign's run tree (decision 409): every absolute path the
    value names is checked after symlinks are resolved, a PATH-like value entry by entry. A value that names no
    absolute path is left to the program."""
    for entry in forwarded_paths(name, value):
        if not entry or not os.path.isabs(entry):
            continue
        resolved = Path(entry).resolve()
        if resolved.is_relative_to(root):
            raise AttackerInfrastructureError("run_tree", f"{name} names {resolved}, inside the run tree {root}")


def check_forwarded_command(command: list[str], root: Path) -> None:
    """Check every argv word, and the value of a `key=value` word, against the run tree: a command word is the
    program's input, and one that names the campaign hands it what the boundary withholds."""
    for index, word in enumerate(command):
        refuse_inside_run_tree(f"command[{index}]", word, root)
        if "=" in word:
            refuse_inside_run_tree(f"command[{index}]", word.split("=", 1)[1], root)


def check_forwarded_environment(environment: dict[str, str], root: Path) -> None:
    """Check every forwarded environment value against the run tree, PATH-like values entry by entry."""
    for name, value in environment.items():
        refuse_inside_run_tree(name, value, root)


def checked_temp_base(root: Path) -> Path:
    """The temporary base the call's scratch is created under, with symlinks resolved, refused when it is inside the
    campaign's run tree: a scratch there would hand the program the campaign it must not see (decision 409)."""
    base = Path(tempfile.gettempdir()).resolve()
    if base.is_relative_to(root):
        raise AttackerInfrastructureError("temp_base", f"the temporary directory {base} is inside the run tree {root}")
    return base


def cost_hook_directory() -> Path | None:
    """The directory holding the cost wrapper's `sitecustomize.py`: the parent's `PYTHONPATH` entry whose
    `sitecustomize.py` names `SERENO_COST_CALLS`, else this checkout's `scripts/cost_hook`, else None. Only this
    directory is ever forwarded, never another entry of the parent's `PYTHONPATH`."""
    for entry in filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep)):
        try:
            text = (Path(entry) / COST_HOOK_MODULE).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if COST_HOOK_MARKER in text:
            return Path(entry).resolve()
    return COST_HOOK_DIR if (COST_HOOK_DIR / COST_HOOK_MODULE).is_file() else None


@dataclass(frozen=True)
class CostMeter:
    """The cost wrapper's meter: the ledger the harness appends the call's rows to and the hook directory the program
    needs to write them. The program receives neither path, only a private calls file in its own scratch."""

    ledger: Path
    hook: Path


def cost_meter(root: Path) -> CostMeter | None:
    """The cost wrapper's meter, or None when the harness does not run under the wrapper. The ledger and the hook
    directory are resolved and refused inside the campaign's run tree, as the temporary base is: either would name the
    campaign the program must not see. A wrapper whose hook cannot be found fails closed too, since a ledger alone
    would collect nothing."""
    calls = os.environ.get(COST_CALLS_NAME)
    if not calls:
        return None
    hook = cost_hook_directory()
    if hook is None:
        raise AttackerInfrastructureError(
            "cost_meter", f"{COST_CALLS_NAME} is set but the cost hook's {COST_HOOK_MODULE} was not found"
        )
    ledger = Path(calls).resolve()
    for name, path in ((COST_CALLS_NAME, ledger), ("PYTHONPATH", hook)):
        if path.is_relative_to(root):
            raise AttackerInfrastructureError("cost_meter", f"{name} names {path}, inside the run tree {root}")
    return CostMeter(ledger, hook)


def config_argument(command: list[str]) -> str | None:
    """The path a `--config` word of the command names, when it carries one."""
    for index, word in enumerate(command):
        if word == "--config" and index + 1 < len(command):
            return command[index + 1]
        if word.startswith("--config="):
            return word.partition("=")[2]
    return None


def credential_variables(command: list[str]) -> list[str]:
    """The credential environment variables the configuration named by the command's `--config` sets, so the program
    still authenticates: `provider.credential_env` in the attacker runtime's configuration, `credential_env_name` in
    the writer design's. The file is read under the same bounded, non-following rules as a staged file, so a FIFO or a
    file swapped in after the check cannot block or overrun the call. A command without a configuration, and a file
    that cannot be read in that form, adds no name. The name is checked against the harness's own variables where it is
    used, not here, so validation can still refuse a configuration that names one."""
    path = config_argument(command)
    if path is None:
        return []
    try:
        data = read_staged(Path(path), CONFIG_BYTES)
    except OSError:
        return []
    if data is None:
        return []
    try:
        raw = yaml.safe_load(data.decode("utf-8"))
    except (ValueError, RecursionError, yaml.YAMLError):
        return []
    if not isinstance(raw, dict):
        return []
    provider = raw.get("provider")
    name = provider.get("credential_env") if isinstance(provider, dict) else raw.get("credential_env_name")
    return [name] if isinstance(name, str) and ENV_NAME.fullmatch(name) else []


def check_forwarded_config(config) -> None:
    """Refuse at config validation what the campaign may not forward: an `env` entry naming a harness-controlled
    variable, and a credential variable the command's configuration names that the harness itself sets. The schema
    refuses the first as well; the second needs the configuration file, which only validation reads before a run."""
    controlled = sorted(name for name in config.env if harness_controlled(name))
    if controlled:
        raise ValueError(f"run_attacker.env names harness-controlled variables: {', '.join(controlled)}")
    for name in credential_variables(config.command):
        if harness_controlled(name):
            raise ValueError(
                f"the attacker configuration names the harness-controlled variable {name} as its credential"
            )


def child_environment(config, scratch: Path, meter: CostMeter | None) -> dict[str, str]:
    """The program's environment, built from an allowlist instead of inherited: the fixed names above, the live-mode
    proxy and CA variables, the LC_* family, the credential variables the attacker configuration names and the
    variables the campaign config lists, and the temporary-directory variables pinned to the call's scratch. A harness
    running under the cost wrapper forwards its meter without a campaign entry: the hook directory as the only
    `PYTHONPATH` entry, and a private calls file in the scratch as the ledger the program writes, never the wrapper's
    own. PWD, OLDPWD and every other inherited variable are left out."""
    # The credential name is read from the configuration on every call, so it is filtered here as well: a program that
    # rewrites its `--config` after validation cannot make the harness forward a variable it sets for itself.
    credentials = [name for name in credential_variables(config.command) if not harness_controlled(name)]
    names = [*CHILD_ENV_NAMES, *LIVE_ENV_NAMES, *credentials, *config.env]
    environment = {name: os.environ[name] for name in names if name in os.environ}
    environment |= {name: value for name, value in os.environ.items() if name.startswith(CHILD_ENV_PREFIXES)}
    environment |= {name: str(scratch) for name in CHILD_TEMP_NAMES}
    if meter is not None:
        entries = [str(meter.hook), *(environment.get("PYTHONPATH") or "").split(os.pathsep)]
        environment[COST_CALLS_NAME] = str(scratch / COST_CALLS_FILE)
        environment["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(entry for entry in entries if entry))
    return environment


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write bytes so an interruption leaves the old file or none, never a truncated one."""
    partial = path.with_name(f".{path.name}.partial")
    partial.write_bytes(data)
    partial.replace(path)


class AuditCopyError(OSError):
    """A staged file cannot enter the audit folder: not a regular file, over its limit, or unreadable."""


def file_limit(name: str) -> int:
    """The largest byte count a staged file may have: a captured output is bounded like a capture, and every other
    file like a response."""
    return CAPTURE_BYTES if name in ("writer.stdout", "writer.stderr") else RESPONSE_BYTES


def open_staged(source: Path, limit: int) -> IO[bytes] | None:
    """Open a staged file under the copy rules, or return None when it does not exist. A symlink or any other
    non-regular file is refused before the open and again on the open descriptor, and so is a file over `limit` bytes,
    with its size in the message; `O_NOFOLLOW` refuses a symlink swapped in after the check, and `O_NONBLOCK` keeps a
    FIFO swapped in from blocking the open forever."""
    name = source.name
    try:
        info = os.lstat(source)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise AuditCopyError(f"{name}: {type(error).__name__}: {error}") from error
    if not stat.S_ISREG(info.st_mode):
        raise AuditCopyError(f"{name}: not a regular file, {info.st_size} bytes, {stat.filemode(info.st_mode)}")
    if info.st_size > limit:
        raise AuditCopyError(f"{name}: {info.st_size} bytes, over the {limit}-byte limit")
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise AuditCopyError(f"{name}: {type(error).__name__}: {error}") from error
    try:
        stream = os.fdopen(descriptor, "rb")
    except OSError as error:
        os.close(descriptor)
        raise AuditCopyError(f"{name}: {type(error).__name__}: {error}") from error
    try:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise AuditCopyError(f"{name}: not a regular file, {stat.filemode(opened.st_mode)}")
        if opened.st_size > limit:
            raise AuditCopyError(f"{name}: {opened.st_size} bytes, over the {limit}-byte limit")
    except BaseException:
        stream.close()
        raise
    return stream


def read_staged(source: Path, limit: int) -> bytes | None:
    """A staged file's bytes, read under `open_staged`'s rules with a remaining-bytes budget: the read stops after
    `limit + 1` bytes, so a file that grows while it is read is refused instead of copied without bound. None means
    the file does not exist; more than `limit` bytes is a refusal. A capture thread still writing its file is
    tolerated: the read takes the bytes that are there and waits for nothing."""
    stream = open_staged(source, limit)
    if stream is None:
        return None
    try:
        chunks, copied = [], 0
        while copied <= limit:
            chunk = stream.read(min(COPY_CHUNK, limit + 1 - copied))
            if not chunk:
                break
            chunks.append(chunk)
            copied += len(chunk)
        if copied > limit:
            raise AuditCopyError(f"{source.name}: grew past the {limit}-byte limit while copying")
        return b"".join(chunks)
    finally:
        stream.close()


def copy_staged(source: Path, target: Path, limit: int) -> None:
    """Copy one staged file into the audit folder once the program has exited. A symlink or any other non-regular
    source is refused, and so is a file over `limit` bytes, both before the file is read and with its size in the
    message; the copy itself reads at most `limit + 1` bytes, so it stops at the limit even when the file grows."""
    try:
        data = read_staged(source, limit)
        if data is None:
            return
        partial = target.with_name(f".{target.name}.partial")
        partial.write_bytes(data)
        partial.replace(target)
    except OSError as error:
        target.with_name(f".{target.name}.partial").unlink(missing_ok=True)
        if isinstance(error, AuditCopyError):
            raise
        raise AuditCopyError(f"{source.name}: {type(error).__name__}: {error}") from error


def program_usage(staged: Path) -> dict | None:
    """What the program reported it spent, read from its own scratch under the copy rules before a later step can fail
    the call: its failure sidecar, or the `writer_usage` of a response it wrote. Best effort, never raising: a call
    that ran keeps its usage in the record whatever ends it, so `realized_usage` charges it."""
    for name in ("failure_usage.json", "response.json"):
        try:
            data = read_staged(staged / name, file_limit(name))
        except OSError:
            continue
        if not data:
            continue
        try:
            record = json.loads(data)
        except (ValueError, RecursionError):
            continue
        if not isinstance(record, dict):
            continue
        usage = record.get("writer_usage") if name == "response.json" else record
        if not isinstance(usage, dict):
            continue
        return {
            "tokens": reported_tokens(usage),
            "cost_usd": reported_cost(usage),
            "unreported_completions": reported_count(usage, "unreported_completions"),
            "declined_attempts": reported_count(usage, "declined_attempts"),
        }
    return None


def append_cost_calls(source: Path, ledger: Path) -> None:
    """Append the call's own cost rows to the wrapper's ledger once the program has exited: the program wrote them to a
    private file in its scratch, so it never reads the ledger's earlier rows, the clean arm's included. The file is
    read under the same copy rules and bounded like a response; a missing or empty file appends nothing, and a torn
    last line is closed so the next row starts on its own line."""
    data = read_staged(source, RESPONSE_BYTES)
    if not data:
        return
    if not data.endswith(b"\n"):
        data += b"\n"
    try:
        with ledger.open("ab") as out:
            out.write(data)
    except OSError as error:
        raise AuditCopyError(
            f"{COST_CALLS_NAME}: cannot append to the ledger: {type(error).__name__}: {error}"
        ) from error


def collect_call(staged: Path, audit: Path) -> None:
    """Copy a call's files out of its scratch directory into the audit folder once the program has exited: its
    response, usage sidecar and captured output. The request's recorded bytes are already there, written before the
    program was launched. The program is never given a path in the audit folder, so the run tree stays outside its
    reach."""
    for name in CALL_FILES:
        copy_staged(staged / name, audit / name, file_limit(name))


class RunLocalAttacker:
    """The attacker of one session. `state` is the checkpoint of the chain's previous completed session, or None for
    the chain's first session; it is copied, so nothing this session does reaches it until `finish` returns.
    `campaign_root` is the campaign's run tree, which the runner pins once and passes down; a session run on its own
    uses its own directory."""

    def __init__(
        self,
        config,
        runtime,
        instruction: str,
        task_id: str,
        state: dict | None = None,
        *,
        campaign_root: Path | None = None,
    ):
        self.config, self.runtime, self.instruction, self.task_id = config, runtime, instruction, task_id
        self.campaign_root = Path(campaign_root if campaign_root is not None else runtime.log.path.parent).resolve()
        self.state = copy.deepcopy(state) if state is not None else initial_state(config)
        sessions = self.state.get("sessions")
        # A checkpoint written before notices were withheld still carries them in its session transcripts.
        self.notices_withheld_in_state = without_notices(sessions) if isinstance(sessions, list) else 0
        self.stage = "exposure" if runtime.session.exposure else "probe"
        self.ticks = 0
        self.session_calls = 0
        self.skipped: set[str] = set()
        self.calls: list[dict] = []
        self._temp_root: Path | None = None
        self.index = self.file_index()

    def temp_root(self) -> Path:
        """The run's private scratch root, created on the first call with a random name and mode 0700 under the
        checked temporary base and removed when this attacker is collected. The 0700 mode and the random names narrow
        what another process can find; a process of the same user can still list the base and read a live call's
        scratch, which no mode prevents. A base or root that cannot be created is a `temp_base` error."""
        if self._temp_root is None:
            base = checked_temp_base(self.campaign_root)
            try:
                root = Path(tempfile.mkdtemp(prefix="sereno-attacker-", dir=base))
                os.chmod(root, 0o700)
            except OSError as error:
                raise AttackerInfrastructureError(
                    "temp_base", f"cannot create the run's scratch root: {type(error).__name__}: {error}"
                ) from error
            weakref.finalize(self, shutil.rmtree, root, ignore_errors=True)
            self._temp_root = root
        return self._temp_root

    def clean_temp_root(self) -> None:
        """Remove the run's private scratch root, if one was created."""
        if self._temp_root is not None:
            shutil.rmtree(self._temp_root, ignore_errors=True)
            self._temp_root = None

    def file_index(self) -> dict:
        """The files Git tracks below the code root at task start as absolute paths, the form `read_code` serves, or
        their count when there are too many."""
        root = self.config.code_root
        command = f"cd {shlex.quote(root)} && git -c safe.directory=/app -c core.quotepath=off ls-files"
        try:
            listed = self.runtime.env.execute(command)
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            raise AttackerInfrastructureError(
                "code_index", f"cannot list the repository files: {type(error).__name__}: {error}"
            ) from error
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
        try:
            resolved = self.runtime.env.execute(
                f"cd {shlex.quote(root)} && LC_ALL=C realpath -e --relative-to=. -- {shlex.quote(relative)}"
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            raise AttackerInfrastructureError("code_realpath", f"{type(error).__name__}: {error}") from error
        # Only realpath's own failure (a missing file, a symlink loop) means the file is absent; a command that was
        # killed or could not run says nothing about the file.
        if did_not_finish(resolved):
            raise AttackerInfrastructureError("code_realpath", "the container did not finish resolving the path")
        if resolved["returncode"]:
            if "Permission denied" in resolved["output"]:
                raise AttackerInfrastructureError("code_realpath", "the path cannot be resolved in the container")
            return {"status": "missing"}
        real = resolved["output"].strip()
        if real == ".." or real.startswith("../") or real == ".git" or real.startswith(".git/"):
            return {"status": "denied", "reason": f"the path resolves outside {root} or into .git"}
        absolute = f"{root}/{real}"
        try:
            validate_path(absolute, root)
        except ValueError:
            return {"status": "denied", "reason": f"the path resolves outside {root} or into .git"}
        # The size is measured before the file is read, so a large file costs no host memory; the read keeps its own
        # size check because the file can grow between the two.
        try:
            measured = self.runtime.env.execute(
                f"cd {shlex.quote(root)} && [ -f {shlex.quote(real)} ] && LC_ALL=C stat -c %s -- {shlex.quote(real)}"
            )
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            raise AttackerInfrastructureError("code_size", f"{type(error).__name__}: {error}") from error
        if did_not_finish(measured):
            raise AttackerInfrastructureError("code_size", "the container did not finish measuring the file")
        if measured["returncode"]:
            return {"status": "missing"}
        size_text = measured["output"].strip()
        if not size_text.isdigit():
            raise AttackerInfrastructureError("code_size", "the file size is not a number")
        size = int(size_text)
        if size > self.config.max_read_bytes:
            return {"status": "too_large", "bytes": size, "limit": self.config.max_read_bytes}
        try:
            text = self.runtime.env.read(absolute)
        except ValueError as error:
            return {"status": "unreadable", "reason": type(error).__name__}
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            raise AttackerInfrastructureError("read_code", f"{type(error).__name__}: {error}") from error
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

    def withheld_notices(self, messages: list[dict]) -> int:
        """The guard notices this call's view leaves out: those in the session's messages and those dropped from the
        chain's earlier transcripts when its checkpoint was loaded."""
        return self.notices_withheld_in_state + sum(1 for message in messages if is_guard_notice(message))

    def advance(self, messages: list[dict]) -> None:
        """Before a model call: call the program on the session's first model call and then on call 1 + n,
        1 + 2n, ... for `every_calls: n`, while the budget has room. Its writes
        land in the files; the messages already prepared for this call are not changed."""
        tick = self.ticks
        self.ticks += 1
        if tick % self.config.every_calls:
            return
        self.call("in_task", visible_transcript(messages), withheld=self.withheld_notices(messages))

    def finish(self, messages: list[dict], summary: dict) -> dict:
        """At the end of a completed session: one evaluation call that may update only the private state, then the
        checkpoint the next session of the chain starts from."""
        transcript = visible_transcript(messages)
        try:
            self.call("session_end", transcript, summary, withheld=self.withheld_notices(messages))
        finally:
            self.clean_temp_root()
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

    def call(self, kind: str, transcript: list[dict], summary: dict | None = None, *, withheld: int) -> None:
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
        try:
            encoded = (json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n").encode()
        except UnicodeEncodeError as error:
            raise AttackerInfrastructureError(
                "request_encoding", f"{type(error).__name__}: the request is not encodable as UTF-8"
            ) from error
        audit = runtime.log.path.parent / AUDIT_DIR / f"{number:04d}"
        try:
            audit.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            raise AttackerInfrastructureError("audit_copy", f"cannot create the audit folder: {error}") from error
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
            "notices_withheld": withheld,
        }
        if len(encoded) > config.max_input_bytes:
            self.fail(
                audit,
                base,
                "input_over_limit",
                f"the request is {len(encoded)} bytes, over max_input_bytes={config.max_input_bytes}",
            )
        try:
            scratch_root = self.temp_root()
        except AttackerInfrastructureError as error:
            self.fail(audit, base, error.code, error.message)
        # The audit's request copy and a record skeleton exist before the program starts: an interrupt or a host kill
        # during the call leaves the request and a record behind, not an empty audit folder.
        try:
            write_bytes_atomic(audit / "request.json", encoded)
            write_atomic(audit / "record.json", base | {"status": "incomplete"})
        except OSError as error:
            self.fail(audit, base, "audit_copy", f"cannot write the audit record: {error}")
        clock = time.monotonic()
        # A fresh scratch directory on every call, under the run's private root outside the run tree: the program
        # keeps nothing between calls, and no path it is given names the session, its audit folder or its event log
        # (decision 409). Its files reach the audit folder only as copies made after it exits.
        try:
            scratch = TemporaryDirectory(prefix="call-", dir=scratch_root, ignore_cleanup_errors=True)
        except OSError as error:
            self.fail(audit, base, "temp_base", f"cannot create the call's scratch directory: {error}")
        with scratch:
            staged = Path(scratch.name)
            try:
                write_bytes_atomic(staged / "request.json", encoded)
            except OSError as error:
                self.fail(audit, base, "audit_copy", f"cannot stage the request: {error}")
            # Every value the program receives is checked against the run tree before it is launched: a command word
            # naming the campaign, or a forwarded variable holding one, fails the call closed as `run_tree`.
            try:
                check_forwarded_command(config.command, self.campaign_root)
                meter = cost_meter(self.campaign_root)
                environment = child_environment(config, staged, meter)
                check_forwarded_environment(environment, self.campaign_root)
            except AttackerInfrastructureError as error:
                self.fail(audit, base, error.code, error.message)
            if meter is not None:
                # The program's ledger is its own empty file in the scratch; the wrapper's ledger is appended to
                # below, so the program never reads the campaign's earlier rows, the clean arm's included.
                try:
                    (staged / COST_CALLS_FILE).write_bytes(b"")
                except OSError as error:
                    self.fail(audit, base, "audit_copy", f"cannot stage the cost calls file: {error}")
            try:
                outcome = run_program(
                    [*config.command, str(staged / "request.json"), str(staged / "response.json")],
                    staged,
                    config.timeout_seconds,
                    staged,
                    env=environment,
                )
            except BaseException:
                if meter is not None:
                    # A paid call the interrupt killed still happened: its rows reach the ledger before the scratch is
                    # removed, and the interrupt itself keeps propagating.
                    with contextlib.suppress(Exception):
                        append_cost_calls(staged / COST_CALLS_FILE, meter.ledger)
                raise
            # The program ran: from here on its record says so, and carries what it spent even when a later step of
            # the call fails. `incomplete` is reserved for a call interrupted between launch and outcome.
            seconds = round(time.monotonic() - clock, 3)
            self.state["usage"]["seconds"] = round(self.state["usage"]["seconds"] + seconds, 3)
            base["seconds"] = seconds
            base["program"] = {
                key: outcome.get(key) for key in ("exit_code", "timed_out", "error", "stdout_bytes", "stderr_bytes")
            }
            if usage := program_usage(staged):
                base["usage"] = {key: usage[key] for key in ("tokens", "cost_usd", "unreported_completions")}
                base["declined_attempts"] = usage["declined_attempts"]
            if meter is not None:
                try:
                    append_cost_calls(staged / COST_CALLS_FILE, meter.ledger)
                except OSError as error:
                    self.fail(audit, base, "audit_copy", str(error))
            try:
                collect_call(staged, audit)
            except OSError as error:
                self.fail(audit, base, "audit_copy", str(error))
        if outcome["timed_out"]:
            self.fail(audit, base, "timeout", f"the program ran past {config.timeout_seconds} seconds")
        if outcome["error"] or outcome["exit_code"] != 0:
            self.fail(audit, base, "writer_failed", outcome["error"] or f"exit status {outcome['exit_code']}")
        try:
            response = self.load_response(audit / "response.json")
            actions = parse_actions(response["actions"], runtime.project)
        except AttackerInfrastructureError as error:
            self.fail(audit, base, error.code, error.message)
        except ResponseError as error:
            self.fail(audit, base, "invalid_response", str(error))
        try:
            base["response_sha256"] = file_sha256(audit / "response.json")
        except OSError as error:
            self.fail(audit, base, "response_hash", f"{type(error).__name__}: {error}")
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
        try:
            write_atomic(audit / "record.json", record)
        except OSError as error:
            # The call happened but its record cannot be written; that is an audit failure, not an attack outcome.
            self.fail(audit, base, "audit_copy", f"cannot write the audit record: {error}")
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
        try:
            if not path.is_file():
                raise ResponseError("the program wrote no response.json")
            if path.stat().st_size > RESPONSE_BYTES:
                raise ResponseError("response.json is too large")
            data = json.loads(path.read_bytes().decode())
        except OSError as error:
            raise AttackerInfrastructureError("response_read", f"{type(error).__name__}: {error}") from error
        except ResponseError:
            raise
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

    def fail(self, audit: Path, base: dict, code: str, message: str) -> NoReturn:
        """Record the call as an infrastructure error, log the event and end it: never an attack outcome. A record that
        cannot be written is not retried, since the same write would fail the same way: the infrastructure error is
        raised without it."""
        record = base | {"status": "infrastructure_error", "error": {"code": code, "message": printable(message)}}
        with contextlib.suppress(OSError):
            write_atomic(audit / "record.json", record)
        with contextlib.suppress(OSError):
            self.runtime.emit("run_attacker_error", code=code, message=printable(message), call=base["call"])
        raise AttackerInfrastructureError(code, message)


def campaign_identity(config) -> dict:
    """An experiment config's dump for its manifest, resume, pairing and hashes, in the one form `config_identity`
    defines: the attacker's empty `env` left out, as `writer_manifest` leaves it out."""
    return config_identity(config.model_dump(mode="json"))


def writer_manifest(config) -> dict:
    """The attacker as the campaign manifest records it: its settings, budgets and starting strategy, and the sha256
    of every command word that names an existing file."""
    check_command_identity(config.command)
    return without_empty_env(config.model_dump(mode="json")) | {
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
