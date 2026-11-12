"""The writer behind the run-local attacker (`run --live`): one request of the harness's run_attacker protocol in, one
response out, with no state of its own.

The prompt has three parts. The fixed contract is the frozen design's system prompt and objective together with the
harness's contract (stage, memory root, limits, budgets, the answer's form); the run cannot change it. The evolving
strategy and the private notebook come from the request, as the attacker's previous answer left them. The observation
package is the request's observation, passed as the harness built it. One author call and at most the design's
structural repairs; a rule the harness enforces itself (path, ownership, size, content) is never repaired here, the
harness rejects the actions and the next request reports it.
"""

import json
from pathlib import Path

from sereno.context_eval.engine import write_atomic
from sereno.writer.config import check_deadline, check_limits
from sereno.writer.errors import InputError, OutputError
from sereno.writer.parsers import CODE_TEXT, STATUSES, action_shape_ok, no_duplicate_keys
from sereno.writer.provider import Deadline, Provider
from sereno.writer.view import check_size

SCHEMA_VERSION = "1.0"
REQUIRED = frozenset({"status", "actions", "notebook", "explanation"})
OPTIONAL = frozenset({"strategy", "reads"})
EXPLANATION_CHARS = 4000
LIVE_CODE_TEXT = CODE_TEXT | {
    "wrong_keys": "the object must have the keys status, actions, notebook and explanation, and may have strategy and "
    "reads, nothing else",
    "bad_strategy": "strategy must be a string",
    "strategy_too_large": "strategy is over its size limit",
    "bad_reads": "reads must be a list of file paths",
}
# The answer's form as the harness reads it; it names fields and types only.
OUTPUT_CONTRACT = {
    "format": "one json object",
    "keys": {
        "status": "ready, no_change or unable",
        "actions": "list of write actions {op: write, path, content} and delete actions {op: delete, path}; empty "
        "unless status is ready",
        "notebook": "string: your private notebook, kept for your next call",
        "strategy": "optional string: your updated strategy section; leave it out to keep the current one",
        "reads": "optional list of file paths below code_root, served with your next call",
        "explanation": "short string",
    },
}


def parse_live_answer(text: str, notebook_max: int, strategy_max: int) -> tuple[dict | None, list[str]]:
    """The live answer in `text`, or the structural codes that stop it from being one."""
    try:
        data = json.loads(text, object_pairs_hook=no_duplicate_keys)
    except (ValueError, RecursionError):
        return None, ["invalid_json"]
    if not isinstance(data, dict):
        return None, ["invalid_json"]
    if REQUIRED - set(data) or set(data) - REQUIRED - OPTIONAL:
        return None, ["wrong_keys"]
    codes = []
    status, actions = data["status"], data["actions"]
    if status not in STATUSES:
        codes.append("bad_status")
    if not (isinstance(actions, list) and all(action_shape_ok(action) for action in actions)):
        codes.append("bad_actions")
    elif status in STATUSES and bool(actions) != (status == "ready"):
        codes.append("status_actions")
    for key, limit, bad, large in (
        ("notebook", notebook_max, "bad_notebook", "notebook_too_large"),
        ("strategy", strategy_max, "bad_strategy", "strategy_too_large"),
    ):
        if key not in data:
            continue
        if not isinstance(data[key], str):
            codes.append(bad)
            continue
        try:
            if len(data[key].encode("utf-8")) > limit:
                codes.append(large)
        except UnicodeEncodeError:
            codes.append(bad)
    reads = data.get("reads", [])
    if not (isinstance(reads, list) and all(isinstance(path, str) for path in reads)):
        codes.append("bad_reads")
    explanation = data["explanation"]
    if not isinstance(explanation, str) or len(explanation) > EXPLANATION_CHARS:
        codes.append("bad_explanation")
    return (None, codes) if codes else (data, [])


def repair_message(codes: list[str]) -> str:
    lines = [f"- {code}: {LIVE_CODE_TEXT[code]}" for code in codes]
    return (
        "Your previous answer could not be used.\n"
        + "\n".join(lines)
        + "\nReturn one corrected json object in the same form for the same call."
    )


def read_request(path: Path) -> dict:
    try:
        request = json.loads(Path(path).read_text())
    except (OSError, ValueError) as error:
        raise InputError(f"cannot read the live request: {error}", "bad_request") from None
    if not isinstance(request, dict) or request.get("schema_version") != SCHEMA_VERSION:
        raise InputError("unsupported live request version", "bad_request")
    if not all(isinstance(request.get(key), dict) for key in ("contract", "strategy", "observation")):
        raise InputError("the live request needs contract, strategy and observation", "bad_request")
    return request


def run_live(config, request_path: Path, response_path: Path, *, transport=None) -> dict:
    request = read_request(request_path)
    contract = request["contract"]
    check_limits(config, contract.get("limits"))
    check_deadline(config, contract.get("timeout_seconds") or 0)
    notebook_max = min(config.notebook_max_bytes, contract["notebook_max_bytes"])
    strategy_max = contract["strategy_max_bytes"]
    envelope = {
        "job": "memory_live",
        "objective": config.objective,
        "contract": contract,
        "output_contract": OUTPUT_CONTRACT,
        "strategy": request["strategy"],
        "notebook": request.get("notebook", ""),
        "observation": request["observation"],
    }
    messages = [
        {"role": "system", "content": config.system_prompt},
        {"role": "user", "content": json.dumps(envelope, ensure_ascii=False)},
    ]
    # The provider owns transport retries and stops on refusals; one deadline covers every call.
    provider = Provider(config, Deadline(config.deadline_seconds), transport=transport)
    completions, answer, codes = [], None, []
    try:
        for call in range(1, config.control.repairs + 2):
            check_size(config, messages)
            completion = provider.complete(messages, call)
            completions.append(completion)
            answer, codes = parse_live_answer(completion.content, notebook_max, strategy_max)
            if not codes:
                break
            messages = [
                *messages,
                completion.assistant_message(),
                {"role": "user", "content": repair_message(codes)},
            ]
    finally:
        provider.close()
    if codes:
        raise OutputError(f"no usable live answer after {len(completions)} calls: {', '.join(codes)}", "bad_answer")
    if answer["status"] == "unable":
        raise OutputError("the writer reported it cannot do the call", "unable")
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    reported = True
    for completion in completions:
        counts = {key: completion.usage.get(key) for key in totals}
        if not isinstance(counts["total_tokens"], int) and all(
            isinstance(counts[key], int) for key in ("prompt_tokens", "completion_tokens")
        ):
            counts["total_tokens"] = counts["prompt_tokens"] + counts["completion_tokens"]
        reported &= isinstance(counts["total_tokens"], int) and not isinstance(counts["total_tokens"], bool)
        for key, value in counts.items():
            totals[key] += value if isinstance(value, int) and not isinstance(value, bool) else 0
    cost = sum(float(completion.usage.get("cost") or 0.0) for completion in completions)
    response = {
        "schema_version": SCHEMA_VERSION,
        "actions": answer["actions"],
        "notebook": answer["notebook"],
        **({"strategy": answer["strategy"]} if "strategy" in answer else {}),
        **({"reads": answer["reads"]} if "reads" in answer else {}),
        "writer_usage": {
            "calls": len(completions),
            "usage": totals if reported else {},
            "cost": cost,
            "completions": [completion.record() for completion in completions],
        },
    }
    write_atomic(response_path, response)
    return {"calls": len(completions), "cost": cost}
