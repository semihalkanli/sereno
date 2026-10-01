"""Append-only JSONL event log for one run.

Every line is one self-contained JSON object. The live viewer tails the file
while the run is going, so each event is flushed as soon as it is written.

Fields present on every event:
    ts          wall-clock UNIX time
    seq         0-based position in the log
    run_id      the run this log belongs to
    session     1-based session number within the chain
    turn        1-based user turn within the session
    step        0-based model call index within the session (null outside the loop)
    agent_id    which agent produced or received the event ("main" for now)
    event       event type, see below
    provenance  source label of the content, set on tool results
    memory_op   memory operation, null until memory exists
    gate        gate decision, null until a gate exists

Event types and their own fields:
    run_start       chain (also as scenario), attack, marker, model, provider, temperature,
                    max_steps, git
    session_start   session_id, date, owner, tools (names), system_prompt, changes (outside
                    changes applied when the session started)
    world_state     state (full world snapshot), reason ("initial", "session_start" or a tool name)
    user_message    text; prefilled true when it comes from the session's history
    model_response  text, reasoning, tool_calls [{id, name, args}], finish_reason,
                    usage {prompt_tokens, completion_tokens, reasoning_tokens, cost},
                    latency_s; only text, tool_calls and prefilled when from the history
    tool_result     call_id, name, args, result, error, state_changed; prefilled true when the
                    call comes from the session's history
    session_end     reason ("final_answer", "max_steps", "error"), final_text
    grade           group (a session id, "final" or "attack"), checks {name: bool}, passed
    run_end         cost_usd, model_calls, tool_calls
"""

import json
import time
from pathlib import Path
from typing import Any


class EventLog:
    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8")
        self._seq = 0
        self.session = 1
        self.turn = 1
        self.agent_id = "main"

    def emit(
        self,
        event: str,
        *,
        step: int | None = None,
        provenance: dict[str, Any] | None = None,
        memory_op: dict[str, Any] | None = None,
        gate: dict[str, Any] | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        row = {
            "ts": time.time(),
            "seq": self._seq,
            "run_id": self.run_id,
            "session": self.session,
            "turn": self.turn,
            "step": step,
            "agent_id": self.agent_id,
            "event": event,
            "provenance": provenance,
            "memory_op": memory_op,
            "gate": gate,
            **fields,
        }
        self._file.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        self._file.flush()
        self._seq += 1
        return row

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
