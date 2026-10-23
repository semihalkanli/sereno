"""One step of the writer program: request.json in, response.json out.

The order is fixed. Check the request and the campaign's limits against the frozen design; settle the previous
transition's staged candidate by the harness's receipt; build the permitted view; reuse a complete candidate recorded
for this exact input, or generate one (one author call, at most two structural repairs, no best-of-N); stage it; write
response.json. A failure that is the program's (provider, input, configuration, an answer the hook contract cannot
carry) ends the step with an exception and no response; only a deliberate no_change or a candidate the harness will
itself reject returns normally.
"""

import time
from collections.abc import Callable
from pathlib import Path

import httpx

from sereno.context_eval.config import fingerprint
from sereno.writer.config import WriterConfig, campaign_timeout, check_deadline, check_limits, code_sha256
from sereno.writer.errors import OutputError, WriterError
from sereno.writer.parsers import Answer, Checked, check_answer, hook_response, parse_answer, repair_message
from sereno.writer.provider import Attempt, Completion, Deadline, Provider
from sereno.writer.request import Request, parse_request, read_request
from sereno.writer.transaction import AttemptDir, Chain, dump, now, write_bytes_atomic
from sereno.writer.view import View, build_view, check_size, sha256_bytes


def input_hash(req: Request, config: WriterConfig, view: View, chain: Chain) -> str:
    """What binds a staged candidate to its input: the chain and transition, the next task, the memory, the preceding
    receipt, the configuration and prompt, and the exact view."""
    return fingerprint(
        {
            "chain": req.chain,
            "transition": req.transition,
            "session": req.session_id,
            "task": req.task_id,
            "memory_sha256": fingerprint(req.memory_files),
            "owned": req.owned,
            "limits": req.limits,
            "last_write": req.last_write,
            "preceding_receipt_sha256": chain.receipt_sha256,
            "config_sha256": config.config_sha256,
            "prompt_sha256": config.prompt_sha256,
            "view_sha256": view.messages_sha256,
        }
    )


def write_response(path: Path, response: dict) -> str:
    """response.json written atomically; returns the sha256 of its bytes, which is the hash the hook records."""
    raw = dump(response)
    write_bytes_atomic(path, raw)
    return sha256_bytes(raw)


def with_repair(messages: list[dict], completion: Completion, codes: list[str]) -> list[dict]:
    """A repair request: the same conversation, the assistant's own turn with its reasoning fields as returned, and
    the neutral problem codes."""
    return [*messages, completion.assistant_message(), {"role": "user", "content": repair_message(codes)}]


class Generation:
    """The result of the author call and its repairs."""

    def __init__(self):
        self.answer: Answer | None = None
        self.checked = Checked()
        self.codes: list[str] = []
        self.calls = 0
        self.cost = 0.0


def generate(
    req: Request,
    config: WriterConfig,
    limits,
    view: View,
    provider: Provider,
    attempt: AttemptDir,
) -> Generation:
    """One author call and at most `repairs` structural repairs. Every candidate, valid or not, is recorded."""
    result = Generation()
    messages = view.messages
    for call in range(1, config.control.repairs + 2):
        view.reverify()
        check_size(config, messages)
        completion = provider.complete(messages, call)
        result.calls = call
        result.cost += float(completion.usage.get("cost") or 0.0)
        answer, codes = parse_answer(completion.content, config.notebook_max_bytes)
        checked = Checked()
        if answer is not None:
            checked = check_answer(answer, req.memory_files, req.owned, limits, req.project)
            codes = checked.codes
        attempt.append(
            "candidates.jsonl",
            {
                "call": call,
                "kind": "initial" if call == 1 else "repair",
                "completion": completion.record(),
                "reasoning": completion.reasoning,
                "content": completion.content,
                "codes": codes,
                "violations": checked.violations,
                "effective": checked.effective,
            },
        )
        result.answer, result.checked, result.codes = answer, checked, codes
        if answer is not None and answer.status == "unable":
            raise OutputError(f"the writer reported it cannot do the assignment: {answer.explanation[:300]}", "unable")
        if not codes or call == config.control.repairs + 1:
            return result
        messages = with_repair(messages, completion, codes)
    return result  # unreachable: the loop returns on its last call


def finish_failure(attempt: AttemptDir | None, started: str, error: Exception, extra: dict) -> None:
    if attempt is not None:
        code = error.code if isinstance(error, WriterError) else "internal_error"
        attempt.write(
            "attempt.json",
            {
                "outcome": "failed",
                "code": code,
                "message": str(error)[:500],
                "started_at": started,
                "finished_at": now(),
            }
            | extra,
        )


def run_step(
    config: WriterConfig,
    request_path: Path,
    response_path: Path,
    deadline: Deadline,
    *,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Run one step. Returns a short summary; raises a WriterError for anything that must end the program non-zero."""
    req = parse_request(read_request(request_path), config)
    limits = check_limits(config, req.limits)
    check_deadline(config, campaign_timeout(req.campaign_dir))
    chain = Chain(req)
    killed = chain.mark_killed()
    notebook = chain.reconcile()
    view = build_view(req, config, limits, notebook)
    digest = input_hash(req, config, view, chain)
    reuse = chain.reusable(digest, sha256_bytes(Path(request_path).read_bytes()))
    if reuse is not None:
        folder, candidate = reuse
        sha = write_response(response_path, candidate["response"])
        if sha != candidate["response_sha256"]:
            raise OutputError("the staged response no longer hashes to its record", "bad_state")
        chain.log({"event": "reused", "transition": req.transition, "attempt": folder.path.name})
        return {"outcome": "reused", "attempt": folder.path.name, "calls": 0, "killed_marked": killed}
    started = now()
    attempt = chain.new_attempt()
    summary = {"attempt": attempt.path.name, "killed_marked": killed}
    provider = None
    try:
        attempt.write(
            "input.json",
            {
                "input_sha256": digest,
                "chain": req.chain,
                "transition": req.transition,
                "session": req.session_id,
                "task": req.task_id,
                "config_sha256": config.config_sha256,
                "code_sha256": code_sha256(),
                "prompt_sha256": config.prompt_sha256,
                "view_sha256": view.messages_sha256,
                "preceding_receipt_sha256": chain.receipt_sha256,
                "last_write": req.last_write,
                "config_files": config.files,
                "assignment_review": config.assignment_review,
                "endpoint": config.endpoint,
                "requested_model": config.api["model"],
                "started_at": started,
            },
        )
        attempt.write("manifest.json", view.manifest())
        attempt.write("view.json", view.messages)

        def sink(record: Attempt, request: bytes, response: bytes | None) -> None:
            stem = f"call-{record.call}-t{record.attempt}"
            write_bytes_atomic(attempt.path / f"{stem}.request.json", request)
            if response is not None:
                write_bytes_atomic(attempt.path / f"{stem}.response.json", response)
            attempt.append("transport.jsonl", record.record())

        provider = Provider(config, deadline, transport=transport, sleep=sleep, sink=sink)
        generated = generate(req, config, limits, view, provider, attempt)
        answer, checked = generated.answer, generated.checked
        if answer is None or not checked.deliverable:
            raise OutputError(
                f"no usable answer after {generated.calls} calls: {', '.join(generated.codes)}", "unusable_answer"
            )
        response = hook_response(answer)
        sha = sha256_bytes(dump(response))
        # Stage first, then deliver: the candidate is on disk before the hook can see its response.
        attempt.write(
            "candidate.json",
            {
                "attempt": attempt.path.name,
                "input_sha256": digest,
                "status": answer.status,
                "notebook": answer.notebook,
                "explanation": answer.explanation,
                "codes": generated.codes,
                "response": response,
                "response_sha256": sha,
            },
        )
        if write_response(response_path, response) != sha:
            raise OutputError("the response changed while it was written", "bad_state")
        outcome = "delivered_violating" if generated.codes else f"delivered_{answer.status}"
        summary |= {"outcome": outcome, "calls": generated.calls, "cost": generated.cost, "codes": generated.codes}
        attempt.write(
            "attempt.json",
            {"outcome": outcome, "started_at": started, "finished_at": now(), "response_sha256": sha}
            | {key: summary[key] for key in ("calls", "cost", "codes")}
            | {"transport_attempts": len(provider.attempts)},
        )
        return summary
    except Exception as error:
        finish_failure(attempt, started, error, {"transport_attempts": len(provider.attempts) if provider else 0})
        raise
    finally:
        if provider is not None:
            provider.close()
