"""The request's observation, as the model first sees it and as the tools can read it.

One request carries the contract, the attacker's strategy and notebook, and an observation package: the session and
task, the repository's file index at task start, the code views the harness built, the files the previous call asked
for, the agent's visible transcript, the chain's earlier sessions, the carried memory and the previous call's result.

The first user message is bounded. Sections are cut in a fixed order with markers that name what is missing, and the
transcript arrives as a tail whose dropped count is stated. Everything cut stays reachable: `read_transcript` pages
through the same request, and `find_files`/`search_files` walk the whole index and the served views.

A visible message may carry a readable `thinking` summary, rendered as `[thinking] ...` ahead of its content, and a
redacted thinking block with no readable text is named as `[redacted thinking]`. Thinking blocks already inside the
content are still flattened as `[thinking] ...` for compatibility.
"""

import json
import re

# Sections of the first message share the envelope by these fractions of `observation_max_bytes`.
INDEX_SHARE, VIEWS_SHARE, REQUESTED_SHARE, MEMORY_SHARE, SESSIONS_SHARE = 0.05, 0.28, 0.22, 0.15, 0.08


def json_bytes(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def bounded_text(text: str, limit: int) -> str:
    """`text` when it fits, else its first bytes and a marker naming how much is missing. Never cuts a UTF-8
    sequence."""
    raw = text.encode("utf-8", "backslashreplace")
    if len(raw) <= limit:
        return text
    cut = raw[:limit].decode("utf-8", "ignore")
    return cut + f"\n[truncated: {len(raw)} bytes in all, {len(raw) - len(cut.encode())} not shown]"


def flatten_content(value: object) -> str:
    """A recorded message's content as text: strings, text blocks, tool results, tool calls and thinking summaries."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict):
                kind = block.get("type")
                if kind in (None, "text"):
                    parts.append(str(block.get("text", "")))
                elif kind == "tool_result":
                    parts.append("[tool_result] " + flatten_content(block.get("content")))
                elif kind == "tool_use":
                    parts.append(
                        f"[tool_use {block.get('name')}] " + json.dumps(block.get("input"), ensure_ascii=False)
                    )
                elif kind == "thinking":
                    parts.append("[thinking] " + str(block.get("thinking", "")))
                else:
                    parts.append(f"[{kind}]")
            else:
                parts.append(str(block))
        return "\n".join(part for part in parts if part)
    if value is None:
        return ""
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def render_message(index: int, message: object) -> str:
    """One recorded message as a line: its position, its role, its readable thinking summary, its text and its tool
    interactions. The harness leaves guard notices out of the request, so nothing here names one."""
    if not isinstance(message, dict):
        return f"[{index:04d}] {message!s}"
    role = message.get("role", "?")
    parts = []
    thinking = message.get("thinking")
    if isinstance(thinking, str) and thinking.strip():
        parts.append("[thinking] " + thinking)
    elif message.get("thinking_redacted") is True:
        parts.append("[redacted thinking]")
    parts.append(flatten_content(message.get("content")))
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            function = call.get("function") or {}
            parts.append(f"tool_call {function.get('name')}({function.get('arguments')})")
    if message.get("tool_call_id"):
        parts.insert(0, f"tool_result {message['tool_call_id']}:")
    text = " | ".join(part for part in parts if part)
    return f"[{index:04d}] {role}: {text}"


def line_slice(text: str, start: int | None, end: int | None) -> dict:
    """A line range of `text` (1-based, inclusive) with its own bounds named."""
    lines = text.splitlines(keepends=True)
    total = len(lines)
    first = max(start or 1, 1)
    last = min(end if end is not None else total, total)
    if total == 0:
        return {"status": "ok", "start_line": 1, "end_line": 1, "total_lines": 0, "text": ""}
    if first > total or last < first:
        return {"status": "denied", "reason": f"the range is outside lines 1-{total}", "total_lines": total}
    return {
        "status": "ok",
        "start_line": first,
        "end_line": last,
        "total_lines": total,
        "text": "".join(lines[first - 1 : last]),
    }


class Transcript:
    """The current session's visible transcript and the chain's earlier sessions, addressable for paging and
    search. Readable thinking summaries are part of each rendered message. `current` is the running session; every
    other reference is an earlier session's id or list position."""

    def __init__(self, observation: dict):
        self.current = [message for message in observation.get("transcript") or [] if isinstance(message, dict)]
        self.sessions = [
            session for session in observation.get("completed_sessions") or [] if isinstance(session, dict)
        ]
        self.by_ref: dict[str, list] = {}
        for position, session in enumerate(self.sessions):
            messages = [message for message in session.get("transcript") or [] if isinstance(message, dict)]
            for ref in (str(position), session.get("id"), session.get("session_id")):
                if isinstance(ref, str) and ref and ref not in self.by_ref:
                    self.by_ref[ref] = messages
        session = observation.get("session") or {}
        if isinstance(session.get("id"), str) and session["id"]:
            self.by_ref.setdefault(session["id"], self.current)

    def describe(self) -> dict:
        sessions = [
            {
                "ref": str(position),
                "id": session.get("session_id", session.get("id")),
                "task_id": session.get("task_id"),
                "stage": session.get("stage"),
                "instruction": bounded_text(str(session.get("instruction") or ""), 300),
                "messages": len(session.get("transcript") or []),
                "calls": session.get("calls"),
            }
            for position, session in enumerate(self.sessions)
        ]
        return {"sessions": sessions, "current": {"messages": len(self.current)}}

    def select(self, ref: str | None) -> tuple[str, list[dict]]:
        key = ref or "current"
        if key == "current":
            return key, self.current
        if key in self.by_ref:
            return key, self.by_ref[key]
        raise KeyError(key)

    def page(self, ref: str | None, offset: int, limit: int, max_bytes: int) -> dict:
        key, messages = self.select(ref)
        total = len(messages)
        first = min(max(offset, 0), total)
        rendered, used = [], 0
        for index in range(first, total):
            if len(rendered) >= limit:
                break
            text = bounded_text(render_message(index, messages[index]), max_bytes)
            size = len(text.encode()) + 4
            if rendered and used + size > max_bytes:
                break
            rendered.append(text)
            used += size
        next_offset = first + len(rendered)
        return {
            "status": "ok",
            "session": key,
            "total": total,
            "offset": first,
            "returned": len(rendered),
            "next": next_offset if next_offset < total else None,
            "messages": rendered,
        }

    def search(self, query: str, ref: str | None, limit: int, *, ignore_case: bool = True, regex: bool = False) -> dict:
        if not query:
            return {"status": "denied", "reason": "query must not be empty"}
        flags = re.IGNORECASE if ignore_case else 0
        try:
            pattern = re.compile(query if regex else re.escape(query), flags)
        except re.error as error:
            return {"status": "denied", "reason": f"the pattern does not compile: {error}"}
        if ref in (None, "all"):
            targets = [("current", self.current)]
            targets += [
                (str(position), messages)
                for position, messages in enumerate([s.get("transcript") or [] for s in self.sessions])
            ]
        else:
            key, messages = self.select(ref)
            targets = [(key, messages)]
        matches, scanned = [], 0
        for key, messages in targets:
            for index, message in enumerate(messages):
                scanned += 1
                text = render_message(index, message)
                found = pattern.search(text)
                if not found:
                    continue
                start = max(found.start() - 120, 0)
                excerpt = text[start : start + 320]
                matches.append({"session": key, "index": index, "role": message.get("role"), "excerpt": excerpt})
                if len(matches) >= limit:
                    return {"status": "ok", "query": query, "matches": matches, "scanned": scanned, "more": True}
        return {"status": "ok", "query": query, "matches": matches, "scanned": scanned, "more": False}


def bounded_map(mapping: object, per_file: int, total: int) -> tuple[dict, dict]:
    """A path-to-record map cut to a per-file and a total budget; what is left out is named once, not cut silently."""
    if not isinstance(mapping, dict):
        return {}, {}
    out, used, omitted = {}, 0, []
    for path in sorted(mapping):
        record = mapping[path]
        entry = dict(record) if isinstance(record, dict) else {"value": record}
        text = entry.get("text")
        if isinstance(text, str):
            cut = bounded_text(text, per_file)
            if cut != text:
                entry["text"] = cut
                entry["truncated"] = True
                entry["full_bytes"] = len(text.encode())
        size = json_bytes(path) + json_bytes(entry)
        if used + size > total:
            omitted.append(path)
            continue
        used += size
        out[path] = entry
    note = {"omitted": omitted, "files": len(mapping)} if omitted else {"files": len(mapping)}
    return out, note


def bounded_memory(memory: object, per_file: int, total: int) -> tuple[dict, dict]:
    if not isinstance(memory, dict):
        return {"files": {}, "owned": []}, {}
    raw_files = memory.get("files")
    files = raw_files if isinstance(raw_files, dict) else {}
    out, used, omitted = {}, 0, []
    for path in sorted(files):
        text = files[path] if isinstance(files[path], str) else json.dumps(files[path], ensure_ascii=False)
        cut = bounded_text(text, per_file)
        size = json_bytes(path) + len(cut.encode()) + 2
        if used + size > total:
            omitted.append(path)
            continue
        used += size
        out[path] = cut
    owned = [path for path in memory.get("owned") or [] if isinstance(path, str)]
    note = {"files": len(files)} | ({"omitted": omitted} if omitted else {})
    return {"files": out, "owned": owned}, note


def summarize_index(index: object, paths: int, cap: int) -> dict:
    """The file index of the repository at task start: its shape, a sample of paths and the count left out. The
    tools walk the whole index."""
    if not isinstance(index, dict):
        return {"status": "absent"}
    summary = {"root": index.get("root"), "status": index.get("status"), "entries": index.get("entries")}
    files = index.get("files")
    if isinstance(files, list):
        shown = [path for path in files if isinstance(path, str)][:paths]
        summary["files"], summary["more"] = shown, max(len(files) - len(shown), 0)
        while shown and json_bytes(summary) > cap:
            shown.pop()
            summary["more"] += 1
    return summary


def session_summaries(sessions: object, cap: int) -> dict:
    """The chain's earlier sessions as short cards; their transcripts are read with `read_transcript`."""
    if not isinstance(sessions, list):
        return {"sessions": []}
    cards = []
    for position, session in enumerate(sessions):
        if not isinstance(session, dict):
            continue
        summary = session.get("summary")
        cards.append(
            {
                "ref": str(position),
                "id": session.get("session_id", session.get("id")),
                "task_id": session.get("task_id"),
                "stage": session.get("stage"),
                "instruction": bounded_text(str(session.get("instruction") or ""), 600),
                "calls": session.get("calls"),
                "messages": len(session.get("transcript") or []),
                "summary": bounded_text(
                    summary if isinstance(summary, str) else json.dumps(summary, ensure_ascii=False, sort_keys=True),
                    4_000,
                ),
            }
        )
    omitted = 0
    while cards and json_bytes(cards) > cap:
        cards.pop(0)
        omitted += 1
    return {"sessions": cards, "omitted": omitted}


def transcript_tail(messages: list, budget: int, per_message: int) -> dict:
    """The last messages of the current transcript that fit; the dropped count is stated, never hidden."""
    total = len(messages)
    if budget <= 0 or not messages:
        return {
            "session": "current",
            "total": total,
            "shown": 0,
            "dropped": total,
            "messages": [],
            "note": "the transcript did not fit the envelope; read_transcript pages through all of it",
        }
    rendered, used = [], 0
    for index in range(total - 1, -1, -1):
        text = bounded_text(render_message(index, messages[index]), per_message)
        size = len(text.encode()) + 4
        if rendered and used + size > budget:
            break
        rendered.append(text)
        used += size
    rendered.reverse()
    return {
        "session": "current",
        "total": total,
        "shown": len(rendered),
        "dropped": total - len(rendered),
        "messages": rendered,
    }


def envelope(request: dict, settings) -> dict:
    """The first user message's content: the contract and the attacker's state, the bounded observation, and the
    transcript's tail in whatever the other sections leave."""
    raw_contract = request.get("contract")
    contract = raw_contract if isinstance(raw_contract, dict) else {}
    raw_observation = request.get("observation")
    observation = raw_observation if isinstance(raw_observation, dict) else {}
    budget = settings.observation_max_bytes
    views, views_note = bounded_map(
        observation.get("code_views"), settings.observation_file_bytes, int(budget * VIEWS_SHARE)
    )
    requested, requested_note = bounded_map(
        observation.get("requested_files"), settings.observation_file_bytes, int(budget * REQUESTED_SHARE)
    )
    memory, memory_note = bounded_memory(
        observation.get("memory"), settings.observation_file_bytes, int(budget * MEMORY_SHARE)
    )
    raw_strategy = request.get("strategy")
    strategy = raw_strategy if isinstance(raw_strategy, dict) else {}
    view = {
        "session": observation.get("session"),
        "budget_used": contract.get("used"),
        "previous_result": observation.get("previous_result"),
        "code_index": summarize_index(
            observation.get("code_index"), settings.observation_paths, int(budget * INDEX_SHARE)
        ),
        "code_views": views,
        "code_views_note": views_note,
        "requested_files": requested,
        "requested_files_note": requested_note,
        "memory": memory,
        "memory_note": memory_note,
        "completed_sessions": session_summaries(observation.get("completed_sessions"), int(budget * SESSIONS_SHARE)),
        "session_summary": observation.get("session_summary"),
    }
    base = {
        "contract": contract,
        "strategy": strategy,
        "notebook": request.get("notebook") or "",
        "observation": view,
    }
    remaining = budget - json_bytes(base) - 200
    view["transcript"] = transcript_tail(
        [message for message in observation.get("transcript") or [] if isinstance(message, dict)],
        remaining,
        max(settings.transcript_page_bytes // 4, 500),
    )
    return base
