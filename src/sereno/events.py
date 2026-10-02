"""Append-only JSONL event log for one run, and the run's diagnostic log.

A run directory holds two files:
    events.jsonl  everything that happened in the run, one JSON object per line
    diag.jsonl    the program's own diagnostic records (HTTP attempts, retries, errors)

The live viewer tails events.jsonl while the run is going, so each event is
flushed as soon as it is written.

The design follows three references (decision log section 85):
- Inspect AI's event model for the event kinds: model calls keep the exact
  request and response, tools, state, scores, errors, approvals, store, and
  spans that group them.
- OpenTelemetry GenAI semantic conventions for field and operation names,
  pinned to open-telemetry/semantic-conventions-genai commit b31e9e8ea26a
  (2026-09-30, all names at stability "development"). Project fields carry a
  `sereno.` prefix.
- Claude Code transcripts for the id links: every event has its own `id`, and
  `parent_id` points to the span it belongs to, so a later worker agent's
  events hang under the call that started it.

Fields present on every event:
    id               uuid of this event
    parent_id        id of the enclosing span_begin (null for the run span)
    ts               ISO 8601 UTC time with milliseconds
    seq              0-based position in the log
    run_id           the run this log belongs to
    session          1-based session number within the chain (null before the first)
    turn             1-based user turn within the session (null outside a turn)
    step             0-based model call index within the session (null outside the loop)
    gen_ai.agent.id  which agent produced or received the event ("main" for now)
    event            event kind, see below

Event kinds and their own fields:
    span_begin   type ("run", "session", "turn"; later "agent"), name, and per type:
                 run: chain, sessions (count), attack, marker, gen_ai.request.model, gen_ai.provider.name,
                      sereno.upstream_provider, gen_ai.request.temperature, max_steps, git,
                      pid (of the process writing the log, for liveness)
                 session: session_id, date, owner, gen_ai.system_instructions,
                      gen_ai.tool.definitions (full schemas as sent), changes (outside changes
                      applied when the session started)
                 turn: content (the user's message)
    span_end     type, name, span (id of the matching span_begin), and per type:
                 run: model_calls, tool_calls, sereno.cost_usd, duration_s
                 session: reason ("final_answer", "max_steps", "error"), final_text
                 A run that dies on an exception logs an error event and closes every
                 open span with reason "error" ("stopped" when interrupted with ctrl+c).
                 A run whose process is killed leaves its spans open; readers check
                 `pid` to tell it from a live one.
    input        content: a user message; prefilled true when it comes from the session's history
    chat         one model call.
                 call {request, response}: the exact JSON body posted and the exact JSON
                     returned; request.messages is the exact conversation the model saw.
                     Stored whole on every call, so the log grows with the square of
                     session length; that is the price of keeping it exact.
                 gen_ai.request.model, gen_ai.response.id, gen_ai.response.model,
                 gen_ai.response.finish_reasons, gen_ai.output.messages (role and typed parts:
                 text, reasoning, tool_call), gen_ai.usage.input_tokens,
                 gen_ai.usage.output_tokens, gen_ai.usage.reasoning.output_tokens,
                 gen_ai.usage.cache_read.input_tokens, sereno.cost_usd, duration_s,
                 retries, attempts [{status, duration_s, error}].
                 History calls carry only gen_ai.output.messages and prefilled true.
    execute_tool gen_ai.tool.name, gen_ai.tool.call.id, gen_ai.tool.call.arguments (an
                 object, or the raw string when it was not valid JSON),
                 gen_ai.tool.call.result (the app's result, null on error), error,
                 sereno.provenance, sereno.state_changed, duration_s; prefilled true when the
                 call comes from the session's history
    state        snapshot (full world), reason ("initial", "session_start" or a tool name)
    score        group (a session id, "final" or "attack"), checks {name: bool}, passed
    error        message, type (exception class), traceback, attempts (model call attempts,
                 when the error came from the model)
    approval     reserved for the gate: decision, explanation, gen_ai.tool.call.id
    store        reserved for memory: gen_ai.operation.name (create_memory, update_memory,
                 search_memory), gen_ai.memory.store.id, records, sereno.provenance

Diagnostic records in diag.jsonl carry ts, level, logger, message, run_id,
event_id (the event being built when the record was written; a chat event's
id is known before its HTTP attempts start) and any extra fields.
"""

import contextvars
import json
import logging
import traceback
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

AGENT_ID = "gen_ai.agent.id"

current_event: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_event", default=None)
current_run: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_run", default=None)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_id() -> str:
    return str(uuid.uuid4())


class JsonFormatter(logging.Formatter):
    _STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        row = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "run_id": current_run.get(),
            "event_id": current_event.get(),
        }
        row.update({k: v for k, v in vars(record).items() if k not in self._STANDARD})
        if record.exc_info:
            row["traceback"] = self.formatException(record.exc_info)
        return json.dumps(row, ensure_ascii=False, default=str)


class EventLog:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")
        self._seq = 0
        self._spans: list[tuple[str, str, str]] = []
        self.session: int | None = None
        self.turn: int | None = None
        self.agent_id = "main"
        self._run_token = current_run.set(run_id)
        self._diag = logging.FileHandler(self.path.parent / "diag.jsonl", encoding="utf-8")
        self._diag.setFormatter(JsonFormatter())
        self._diag.addFilter(lambda _record: current_run.get() == run_id)
        logger = logging.getLogger("sereno")
        logger.setLevel(logging.DEBUG)
        logger.addHandler(self._diag)

    def emit(self, event: str, *, step: int | None = None, id: str | None = None, **fields: Any) -> dict[str, Any]:
        row = {
            "id": id or new_id(),
            "parent_id": self._spans[-1][0] if self._spans else None,
            "ts": now_iso(),
            "seq": self._seq,
            "run_id": self.run_id,
            "session": self.session,
            "turn": self.turn,
            "step": step,
            AGENT_ID: self.agent_id,
            "event": event,
            **fields,
        }
        self._file.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        self._file.flush()
        self._seq += 1
        return row

    def begin(self, type: str, name: str, **fields: Any) -> str:
        row = self.emit("span_begin", type=type, name=name, **fields)
        self._spans.append((row["id"], type, name))
        return row["id"]

    def end(self, type: str, **fields: Any) -> None:
        span_id, span_type, name = self._spans[-1]
        if span_type != type:
            raise ValueError(f"span_end {type!r} does not match the open span {span_type!r}")
        self._spans.pop()
        self.emit("span_end", type=type, name=name, span=span_id, **fields)

    def abort(self, error: BaseException) -> None:
        """Log what ended the run and close every open span: "stopped" for ctrl+c, "error" otherwise."""
        tb = "".join(traceback.format_exception(error))
        self.emit("error", message=str(error), type=type(error).__name__, traceback=tb)
        reason = "stopped" if isinstance(error, KeyboardInterrupt) else "error"
        while self._spans:
            self.end(self._spans[-1][1], reason=reason)

    @contextmanager
    def building(self, event_id: str) -> Generator[None]:
        """Tag diagnostic records written inside the block with the id of the event being built."""
        token = current_event.set(event_id)
        try:
            yield
        finally:
            current_event.reset(token)

    def close(self) -> None:
        self._file.close()
        logging.getLogger("sereno").removeHandler(self._diag)
        self._diag.close()
        current_run.reset(self._run_token)

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
