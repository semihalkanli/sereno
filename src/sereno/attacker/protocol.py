"""The run-local attacker protocol: request validation, the first message, and the response's form.

The harness writes one request and reads one response (see `sereno.context_eval.run_attacker`). This module keeps the
program's side of that contract in one place: a request is refused before a model is called when it is not protocol
1.0, a response is checked before it is written, and the first user message is the bounded observation envelope. The
protocol's state (strategy, notebook, pending reads) belongs to the request; this package keeps nothing between calls.
"""

import json
from pathlib import Path

from sereno.attacker.config import AttackerConfig
from sereno.attacker.errors import ConfigError, InputError, OutputError
from sereno.attacker.observation import envelope
from sereno.attacker.tools import Toolbox
from sereno.context_eval.engine import write_atomic
from sereno.usage import sum_usage

SCHEMA_VERSION = "1.0"
KINDS = frozenset({"in_task", "session_end"})
STAGES = frozenset({"exposure", "probe"})
RESPONSE_KEYS = frozenset(
    {"schema_version", "actions", "strategy", "notebook", "reads", "writer_usage", "status", "loop_ending"}
)
LOOP_ENDINGS = frozenset({"plain_text", "empty_reply", "finish", "max_steps", "max_tokens", "declined"})


def read_request(path: Path) -> dict:
    """The request at `path`, checked for the shape the runtime drives; `InputError` names the first problem."""
    try:
        data = json.loads(Path(path).read_text())
    except OSError as error:
        raise InputError(f"cannot read the request: {error}", "request") from None
    except ValueError as error:
        raise InputError(f"the request is not valid JSON: {error}", "request") from None
    if not isinstance(data, dict):
        raise InputError("the request must be a JSON object", "request")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise InputError(f"the request must carry schema_version {SCHEMA_VERSION!r}", "request")
    for key in ("contract", "strategy", "observation"):
        if not isinstance(data.get(key), dict):
            raise InputError(f"the request needs a {key} object", "request")
    contract = data["contract"]
    if contract.get("kind") not in KINDS:
        raise InputError(f"contract.kind must be one of {sorted(KINDS)}", "contract")
    if contract.get("stage") not in STAGES:
        raise InputError(f"contract.stage must be one of {sorted(STAGES)}", "contract")
    if not isinstance(contract.get("writes_open"), bool):
        raise InputError("contract.writes_open must be a boolean", "contract")
    if not isinstance(data["strategy"].get("text"), str):
        raise InputError("strategy.text must be a string", "contract")
    notebook = data.get("notebook", "")
    if not isinstance(notebook, str):
        raise InputError("notebook must be a string", "contract")
    if not isinstance(contract.get("limits"), dict):
        raise InputError("contract.limits must be an object", "contract")
    for key in ("strategy_max_bytes", "notebook_max_bytes"):
        value = contract.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise InputError(f"contract.{key} must be a positive integer", "contract")
    timeout = contract.get("timeout_seconds")
    if not isinstance(timeout, int | float) or isinstance(timeout, bool) or timeout <= 0:
        raise InputError("contract.timeout_seconds must be a positive number", "contract")
    response_keys = contract.get("response_keys")
    if response_keys is not None:
        if not isinstance(response_keys, list) or not all(isinstance(key, str) for key in response_keys):
            raise InputError("contract.response_keys must be a list of strings", "contract")
        if not {"schema_version", "actions"} <= set(response_keys):
            raise InputError("contract.response_keys must allow schema_version and actions", "contract")
    return data


def check_contract(config: AttackerConfig, request: dict) -> None:
    """Refuse a runtime configuration the request cannot honour before a model is called."""
    contract = request["contract"]
    timeout = contract["timeout_seconds"]
    if config.settings.deadline_seconds >= timeout:
        raise ConfigError(
            f"deadline_seconds {config.settings.deadline_seconds} must be below the harness timeout {timeout}",
            "deadline",
        )
    # Zero is a count, not a bound: the harness denies every queued read at the next call.
    reads = contract.get("max_reads_per_call")
    if not isinstance(reads, int) or isinstance(reads, bool) or reads < 0:
        raise InputError("contract.max_reads_per_call must be a non-negative integer", "contract")
    # A size bound stays positive: a zero limit would name every file too large and never serve one.
    read_bytes = contract.get("max_read_bytes")
    if not isinstance(read_bytes, int) or isinstance(read_bytes, bool) or read_bytes <= 0:
        raise InputError("contract.max_read_bytes must be a positive integer", "contract")
    for key in ("code_root", "memory_root", "project"):
        value = contract.get(key)
        if value is not None and not isinstance(value, str):
            raise InputError(f"contract.{key} must be a string or null", "contract")


def envelope_text(config: AttackerConfig, request: dict) -> str:
    """The first user message: the bounded observation, strategy and notebook as compact JSON."""
    return json.dumps(envelope(request, config.settings.agent), ensure_ascii=False, separators=(",", ":"))


def usage_record(completions: list, *, finish: str, steps: int, provider, config: AttackerConfig) -> dict:
    """The provider use of this call, in the form the harness's `reported_tokens` and `reported_cost` read."""
    totals, missing, cost = sum_usage(completion.usage for completion in completions)
    return {
        "calls": len(completions),
        "usage": totals,
        "unreported_completions": missing,
        "cost": cost,
        "steps": steps,
        "finish": finish,
        "declined_attempts": sum(completion.declined for completion in completions),
        "provider": provider.name,
        "model": config.settings.provider.model,
        "config_sha256": config.config_sha256,
        "prompt_sha256": config.prompt_sha256,
        "completions": [completion.record() for completion in completions],
    }


def build_response(
    config: AttackerConfig,
    request: dict,
    toolbox: Toolbox,
    *,
    finish: str,
    ending: str | None = None,
    steps: int,
    provider,
    completions: list,
) -> dict:
    """The protocol response from the state this call staged, checked before it can be written."""
    contract = request["contract"]
    allowed = set(contract.get("response_keys") or RESPONSE_KEYS)
    response = {
        "schema_version": SCHEMA_VERSION,
        "actions": [dict(action) for action in toolbox.staged.values()],
    }
    if "loop_ending" in allowed:
        response["loop_ending"] = finish if ending is None else ending
    if finish == "declined" and "status" in allowed:
        response["status"] = "declined"
    if toolbox.strategy_dirty and "strategy" in allowed:
        response["strategy"] = toolbox.strategy
    if toolbox.notebook_dirty and "notebook" in allowed:
        response["notebook"] = toolbox.notebook
    if toolbox.queued and "reads" in allowed:
        response["reads"] = list(toolbox.queued)
    if "writer_usage" in allowed:
        response["writer_usage"] = usage_record(
            completions, finish=finish, steps=steps, provider=provider, config=config
        )
    validate_response(response, contract)
    return response


def text_bytes(value: str, key: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise OutputError(f"{key} is not encodable as UTF-8", "response") from None


def validate_response(response: dict, contract: dict) -> None:
    """The response's form; a problem here is a program failure, never an attack outcome."""
    if not isinstance(response, dict):
        raise OutputError("the response must be a JSON object", "response")
    if response.get("schema_version") != SCHEMA_VERSION:
        raise OutputError(f"the response must carry schema_version {SCHEMA_VERSION!r}", "response")
    if extra := sorted(set(response) - RESPONSE_KEYS):
        raise OutputError(f"unexpected response fields {extra}", "response")
    if "status" in response and response["status"] != "declined":
        raise OutputError("status, when present, must be declined", "response")
    if "loop_ending" in response and (
        not isinstance(response["loop_ending"], str) or response["loop_ending"] not in LOOP_ENDINGS
    ):
        raise OutputError(f"loop_ending must be one of {sorted(LOOP_ENDINGS)}", "response")
    actions = response.get("actions")
    if not isinstance(actions, list):
        raise OutputError("actions must be a list", "response")
    for action in actions:
        if not isinstance(action, dict) or set(action) - {"op", "path", "content"}:
            raise OutputError("an action must hold only op, path and content", "response")
        if action.get("op") not in {"write", "delete"}:
            raise OutputError("an action's op must be write or delete", "response")
        if not isinstance(action.get("path"), str) or not action["path"]:
            raise OutputError("an action needs a non-empty path", "response")
        if action["op"] == "write" and not isinstance(action.get("content"), str):
            raise OutputError("a write action needs content", "response")
        if action["op"] == "delete" and action.get("content") not in (None, ""):
            raise OutputError("a delete action carries no content", "response")
    for key, limit in (
        ("strategy", contract["strategy_max_bytes"]),
        ("notebook", contract["notebook_max_bytes"]),
    ):
        if key not in response:
            continue
        value = response[key]
        if not isinstance(value, str):
            raise OutputError(f"{key} must be a string", "response")
        if text_bytes(value, key) > limit:
            raise OutputError(f"{key} is over its {limit}-byte limit", "response")
    reads = response.get("reads")
    if reads is not None and not (isinstance(reads, list) and all(isinstance(path, str) and path for path in reads)):
        raise OutputError("reads must be a list of non-empty paths", "response")
    if "writer_usage" in response and not isinstance(response["writer_usage"], dict):
        raise OutputError("writer_usage must be an object", "response")


def write_response(path: Path, response: dict) -> None:
    """Write the answer so the harness sees the old file or the complete new one, never a partial one."""
    write_atomic(Path(path), response)
