"""Append-only JSONL event log for one run, and the run's diagnostic log.

A run directory holds two files:
    events.jsonl  everything that happened in the run, one JSON object per line
    diag.jsonl    the program's own diagnostic records (HTTP attempts, retries, errors)

The live viewer tails events.jsonl while the run is going, so each event is
flushed as soon as it is written.

The design follows three references (decision log section 85):
- Inspect AI's event model for the event kinds: model calls keep the exact
  request and response, tools, state, scores, errors, approvals, and
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
                      sereno.upstream_provider, gen_ai.request.temperature, gen_ai.request.top_p,
                      sereno.reasoning_effort (each null: provider default), max_steps, git,
                      pid (of the process writing the log, for liveness), seed, poison (slot ->
                      first session that shows it), trigger (the attack's {session, phrase} or null)
                 session: session_id, date, owner, gen_ai.system_instructions,
                      gen_ai.tool.definitions (full schemas as sent), changes (outside changes
                      applied when the session started)
                 turn: content (the user's message)
    span_end     type, name, span (id of the matching span_begin), reason, and per type:
                 run: model_calls, tool_calls, sereno.cost_usd, duration_s
                 session: final_text; reason "final_answer", "max_steps" or "error"
                 Any other span ends with reason "completed". An exception logs one error
                 event and closes every open span with reason "error" ("stopped" for
                 ctrl+c). A run whose process is killed leaves its spans open; readers
                 check `pid` to tell it from a live one.
    input        content: a user message; prefilled true when it comes from the session's history,
                 harness true when the harness wrote it (the session-start memory reminder)
    chat         one model call.
                 call {request, response}: the exact JSON body posted and the exact JSON
                     returned; request.messages is the exact conversation the model saw.
                     A scripted model records the request it was given and its reply.
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
ENVELOPE = ("id", "parent_id", "ts", "seq", "run_id", "session", "turn", "step", AGENT_ID, "event")

current_event: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_event", default=None)
current_run: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_run", default=None)


def iso_ms(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def now_iso() -> str:
    return iso_ms(datetime.now(UTC))


def new_id() -> str:
    return str(uuid.uuid4())


class JsonFormatter(logging.Formatter):
    _STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        row = {
            "ts": iso_ms(datetime.fromtimestamp(record.created, UTC)),
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
        self._spans: list[tuple[str, str, str, tuple[int | None, int | None]]] = []
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
        parent = self._spans[-1][0] if self._spans else None
        envelope = (id or new_id(), parent, now_iso(), self._seq, self.run_id, self.session, self.turn, step)
        row = dict(zip(ENVELOPE, (*envelope, self.agent_id, event), strict=True)) | fields
        self._file.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        self._file.flush()
        self._seq += 1
        return row

    def begin(self, type: str, name: str, *, session: int | None = None, turn: int | None = None, **fields: Any) -> str:
        """Open a span. `session` and `turn` set the numbers stamped on events inside it."""
        saved = self.session, self.turn
        if session is not None:
            self.session, self.turn = session, None
        if turn is not None:
            self.turn = turn
        span_id = self.emit("span_begin", type=type, name=name, **fields)["id"]
        self._spans.append((span_id, type, name, saved))
        return span_id

    def end(self, **fields: Any) -> None:
        """Close the innermost span; its reason is "completed" unless given."""
        span_id, type, name, saved = self._spans.pop()
        fields.setdefault("reason", "completed")
        self.emit("span_end", type=type, name=name, span=span_id, **fields)
        self.session, self.turn = saved

    @contextmanager
    def span(self, type: str, name: str, **fields: Any) -> Generator[dict[str, Any]]:
        """A span around a block; the block fills the yielded dict with the span_end fields.

        On ctrl+c the span closes with reason "stopped", on any other exception with
        "error"; the exception is logged once as an error event and propagates.
        """
        self.begin(type, name, **fields)
        end: dict[str, Any] = {}
        try:
            yield end
        except BaseException as e:
            if not getattr(e, "_sereno_logged", False):
                self.error(e)
                e._sereno_logged = True  # type: ignore[attr-defined]
            end["reason"] = "stopped" if isinstance(e, KeyboardInterrupt) else "error"
            raise
        finally:
            self.end(**end)

    def error(self, error: BaseException, **fields: Any) -> None:
        tb = "".join(traceback.format_exception(error))
        self.emit("error", message=str(error), type=type(error).__name__, traceback=tb, **fields)

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
