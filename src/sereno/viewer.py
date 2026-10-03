"""Agent view for Sereno runs, modelled on Claude Code's agent view.

    uv run sereno watch                       the run list
    uv run sereno watch [<events.jsonl> | --latest] [--no-follow] [--marker REGEX]

The run list shows every run directory under runs/agent grouped by state
(working, failed, stopped, completed), one row per run with an icon, a
deterministic one-line summary, attack and grade badges and the age. Space
opens a peek panel for the selected run, Enter or right arrow attaches to its
transcript, tab moves to the dispatch input, which starts `sereno run` with the
typed arguments (wrapped in the cost wrapper when it calls a model).

Attached, the run is one scrolling stream in the style of the Claude Code CLI:
user prompts, model text, each tool call with its result beneath, and a status
bar. [ and ] move a cursor over the run's events and d opens the event under
the cursor as raw JSON with its diagnostic records, so every logged field is
reachable. World snapshots stay in the log; the stream does not show them.

List keys: up/down select, space peek, enter or right attach, tab dispatch, q or esc quit.
Attached keys: left or esc back, f follow, t thinking, ctrl+o or e expand results,
[ and ] move the event cursor, d event detail, q quit.
"""

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from sereno.checks import is_task_group
from sereno.events import ENVELOPE, read_events

POLL_SECONDS = 0.3
LIST_POLL_SECONDS = 0.5
TAIL_BYTES = 1_000_000
RESULT_PREVIEW_LINES = 3
ARG_MAX_CHARS = 40
RESULT_VALUE_MAX_CHARS = 100
MARKER_STYLE = "bold black on yellow"
CURSOR_STYLE = "on grey23"
DOT = "●"
ELBOW = "⎿"
INDENT = "    "
WORKING_FRAMES = "✢✳✶✻✽✻✶✳"
GROUPS = ("working", "failed", "stopped", "completed")
STATE_STYLE = {"working": "yellow", "failed": "red", "stopped": "grey50", "completed": "green"}
TOOL_PREFIX = "gen_ai.tool."
STATE_EVENT = b'"event": "state"'


class LogTail:
    """Reads complete new lines from a growing file, keeping a partial last line for later.

    With `start` past 0 the first, probably cut, line is dropped.
    """

    def __init__(self, path: Path, start: int = 0) -> None:
        self.path = path
        self._offset = start
        self._skip_first = start > 0
        self._partial = b""

    def read_new(self) -> list[dict[str, Any]]:
        try:
            if self.path.stat().st_size == self._offset:
                return []
        except FileNotFoundError:
            return []
        with self.path.open("rb") as f:
            f.seek(self._offset)
            chunk = f.read()
        self._offset += len(chunk)
        lines = (self._partial + chunk).split(b"\n")
        self._partial = lines.pop()
        if self._skip_first and lines:
            lines = lines[1:]
            self._skip_first = False
        # World snapshots are the largest lines and the viewer never shows them.
        return [json.loads(line) for line in lines if line.strip() and STATE_EVENT not in line]


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


@dataclass
class RunInfo:
    """What the list, the peek panel and the status bar need, folded from a run's events."""

    path: Path
    run_id: str = ""
    start: dict[str, Any] | None = None
    end: dict[str, Any] | None = None
    legacy: bool = False
    session: int | None = None
    turn: int | None = None
    step: int | None = None
    last_chat: dict[str, Any] | None = None
    last_text: str | None = None
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_tool: dict[str, Any] | None = None
    scores: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    final_text: str | None = None
    model_calls: int = 0
    tool_calls: int = 0
    retries: int = 0
    cost: float = 0.0
    last_ts: datetime | None = None

    def apply(self, event: dict[str, Any]) -> None:
        kind = event.get("event")
        if self.start is None and not self.legacy:
            if kind == "span_begin" and event.get("type") == "run":
                self.start = event
            else:
                self.legacy = True
        self.run_id = event.get("run_id") or self.run_id
        self.last_ts = _parse_ts(event.get("ts")) or self.last_ts
        if self.legacy:
            return
        if event.get("session") is not None:
            self.session = event["session"]
        self.turn = event.get("turn")
        if event.get("step") is not None:
            self.step = event["step"]
        if kind == "chat":
            self.last_chat = event
            text = _chat_text(event)
            if text:
                self.last_text = text
            if not event.get("prefilled"):
                self.model_calls += 1
                self.cost += event.get("sereno.cost_usd") or 0.0
                self.retries += event.get("retries") or 0
        elif kind == "execute_tool":
            self.results[event.get(TOOL_PREFIX + "call.id")] = event
            self.last_tool = event
            if not event.get("prefilled"):
                self.tool_calls += 1
        elif kind == "score":
            self.scores[event.get("group")] = event
        elif kind == "error":
            self.errors.append(event)
        elif kind == "span_end" and event.get("type") == "session":
            self.final_text = event.get("final_text")
        elif kind == "span_end" and event.get("type") == "run":
            self.end = event

    @property
    def name(self) -> str:
        base = self.run_id or self.path.parent.name
        return base.split("_", 1)[1] if "_" in base else base

    @property
    def model(self) -> str | None:
        return (self.start or {}).get("gen_ai.request.model")

    @property
    def attack(self) -> str | None:
        return (self.start or {}).get("attack")

    @property
    def marker(self) -> str | None:
        return (self.start or {}).get("marker")

    @property
    def injected(self) -> bool:
        return bool(self.scores.get("attack", {}).get("passed"))

    def state(self) -> str:
        if self.legacy:
            return "completed"
        if self.end is not None and self.end.get("reason") == "stopped":
            return "stopped"
        if self.errors or (self.end is not None and self.end.get("reason") == "error"):
            return "failed"
        if self.end is not None:
            return "completed"
        if self.start is None or pid_alive(self.start.get("pid")):
            return "working"
        return "stopped"

    def task_checks(self) -> tuple[int, int]:
        checks = [
            ok for group, e in self.scores.items() if is_task_group(group) for ok in (e.get("checks") or {}).values()
        ]
        return sum(bool(ok) for ok in checks), len(checks)

    def pending_call(self) -> dict[str, Any] | None:
        """A tool call of the last model response that has no result yet."""
        for part in _parts(self.last_chat):
            if part.get("type") == "tool_call" and part.get("id") not in self.results:
                return part
        return None

    def age(self, now: datetime | None = None) -> float:
        started = _parse_ts((self.start or {}).get("ts"))
        if started is None:
            return 0.0
        if self.end is not None and self.end.get("duration_s") is not None:
            return float(self.end["duration_s"])
        if self.state() != "working":
            return ((self.last_ts or started) - started).total_seconds()
        return ((now or datetime.now(UTC)) - started).total_seconds()

    def summary(self) -> str:
        """One deterministic line on what the run is doing, what stopped it or what it produced."""
        if self.legacy:
            return "old log format"
        state = self.state()
        if state == "failed":
            error = self.errors[-1] if self.errors else {}
            return f"error: {error.get('type', '')} {error.get('message', '')}".strip()
        if state == "completed":
            passed, total = self.task_checks()
            result = f"result: {_one_line(self.final_text, 60)}" if self.final_text else "result: (no answer)"
            return f"{result} · {passed}/{total} checks" if total else result
        pending = self.pending_call()
        if pending is not None:
            activity = format_call(pending.get("name"), pending.get("arguments"))
        elif (
            self.last_tool is not None and self.last_chat is not None and self.last_tool["seq"] > self.last_chat["seq"]
        ):
            activity = "waiting for the model"
        elif self.last_text:
            activity = _one_line(self.last_text, 80)
        else:
            activity = "starting"
        return f"stopped: {activity}" if state == "stopped" else activity

    def status_line(self) -> Text:
        text = Text()
        model = self.model or "-"
        provider = (self.start or {}).get("sereno.upstream_provider")
        text.append(f"{model}@{provider}" if provider else model, style="bold")
        step = "-" if self.step is None else str(self.step)
        turn = "-" if self.turn is None else str(self.turn)
        session = "-" if self.session is None else str(self.session)
        text.append(f"  session {session} turn {turn} step {step}", style="dim")
        text.append(f"  USD {self.cost:.6f}")
        text.append(f"  {self.model_calls} model calls  {self.tool_calls} tool calls", style="dim")
        if self.retries:
            text.append(f"  {self.retries} retries", style="yellow")
        state = self.state()
        text.append("  ")
        text.append(state if state != "working" else "running", style=f"bold {STATE_STYLE[state]}")
        passed, total = self.task_checks()
        if total:
            ok = passed == total
            text.append("  grade ")
            text.append("PASS" if ok else "FAIL", style="bold green" if ok else "bold red")
            text.append(f" {passed}/{total}", style="dim")
        if self.injected:
            text.append("  injected", style="bold red")
        return text


class RunWatch:
    """A run's directory as the list sees it: the first line in full, then the end of the file, incrementally."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.info = RunInfo(path)
        self.tail: LogTail | None = None

    def poll(self) -> bool:
        if self.tail is None:
            if not self.path.exists():
                return False
            with self.path.open("rb") as f:
                first = f.readline()
            if not first.endswith(b"\n"):
                return False
            self.info.apply(json.loads(first))
            size = self.path.stat().st_size
            start = len(first) if size - len(first) <= TAIL_BYTES else size - TAIL_BYTES
            self.tail = LogTail(self.path, start)
        elif self.info.end is not None or self.info.legacy:
            return False
        new = self.tail.read_new()
        for event in new:
            self.info.apply(event)
        return bool(new)


def _parts(chat: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not chat:
        return []
    return [p for m in chat.get("gen_ai.output.messages") or [] for p in m.get("parts") or []]


def _chat_text(chat: dict[str, Any]) -> str | None:
    texts = [p.get("content") for p in _parts(chat) if p.get("type") == "text" and p.get("content")]
    return "\n".join(texts) or None


def _short(value: Any) -> str:
    """An argument value on one line; strings stay quoted so they read as strings."""
    if isinstance(value, str):
        value = json.dumps(" ".join(value.split()), ensure_ascii=False)
    return _one_line(value, ARG_MAX_CHARS)


def format_call(name: str | None, args: Any) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            pass
    if isinstance(args, dict):
        inner = ", ".join(f"{k}={_short(v)}" for k, v in args.items())
    else:
        inner = _short(args)
    return f"{name}({inner})"


def provenance_tag(provenance: dict[str, Any] | None) -> str:
    if not provenance:
        return ""
    parts = [str(provenance.get("channel", "?"))]
    if provenance.get("sha256"):
        parts.append(f"sha:{provenance['sha256'][:4]}")
    parts += [f"{k}:{v}" for k, v in provenance.items() if k not in {"channel", "sha256"} and v is not None]
    return "[" + " ".join(parts) + "]"


def _indented(text: Text, body: str, style: str = "", first_prefix: str = f"  {ELBOW} ") -> None:
    for i, line in enumerate(body.splitlines() or [""]):
        text.append("\n" + (first_prefix if i == 0 else INDENT) + line, style=style)


def _one_line(value: Any, limit: int = RESULT_VALUE_MAX_CHARS) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    raw = " ".join(raw.split())
    return raw if len(raw) <= limit else raw[: limit - 1] + "…"


def result_lines(raw: str) -> list[str]:
    """Compact lines for a tool result: JSON dicts as key: value, lists of dicts one item per line."""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw.splitlines()
    if isinstance(data, dict):
        return [f"{k}: {_one_line(v)}" for k, v in data.items()]
    if isinstance(data, list):
        lines = []
        for item in data:
            if isinstance(item, dict):
                scalars = [v for v in item.values() if isinstance(v, str | int | float | bool) and v != ""]
                lines.append("  ".join(_one_line(v, 60) for v in scalars))
            else:
                lines.append(_one_line(item))
        return lines or ["[]"]
    return [_one_line(data)]


def render_result(result: dict[str, Any] | None, expanded: bool) -> Text:
    """The lines under a tool call: the execute_tool result, or a pending marker while the tool runs."""
    text = Text()
    if result is None:
        text.append(f"\n  {ELBOW} running…", style="dim")
        return text
    tag = provenance_tag(result.get("sereno.provenance"))
    suffix = Text()
    if tag:
        suffix.append(f"  {tag}", style="dim")
    if result.get("sereno.state_changed"):
        suffix.append("  state changed", style="yellow")
    if result.get("error"):
        text.append(f"\n  {ELBOW} ")
        text.append(f"Error: {result['error']}", style="red")
        text.append_text(suffix)
        return text
    lines = result_lines(str(result.get(TOOL_PREFIX + "call.result") or "")) or ["(empty)"]
    shown = lines if expanded else lines[:RESULT_PREVIEW_LINES]
    text.append(f"\n  {ELBOW} {shown[0]}")
    text.append_text(suffix)
    for line in shown[1:]:
        text.append(f"\n{INDENT}{line}")
    if len(lines) > len(shown):
        text.append(f"\n{INDENT}… +{len(lines) - len(shown)} lines (ctrl+o to expand)", style="dim")
    return text


def tool_line(text: Text, name: Any, args: Any, result: dict[str, Any] | None, expand: bool) -> None:
    """A tool call as `● name(args)` with its result, or a pending marker, beneath."""
    text.append(f"{DOT} ", style="bold green")
    text.append(format_call(name, args), style="bold")
    text.append_text(render_result(result, expand))


def score_lines(text: Text, event: dict[str, Any], label: str) -> None:
    """A score group's verdict, then one line per check."""
    passed = bool(event.get("passed"))
    text.append(label, style="bold")
    text.append("PASS" if passed else "FAIL", style="bold green" if passed else "bold red")
    for i, (name, ok) in enumerate((event.get("checks") or {}).items()):
        text.append(f"\n  {ELBOW} " if i == 0 else f"\n{INDENT}")
        text.append(("ok   " if ok else "FAIL ") + name, style="green" if ok else "red")


@dataclass
class Entry:
    """One block of the stream and the seq numbers of the events it shows."""

    text: Text
    seqs: list[int]


def _render_chat(event: dict[str, Any], results: dict[str, dict[str, Any]], expand: bool, thinking: bool) -> Entry:
    text = Text()
    seqs = [event["seq"]]
    for part in _parts(event):
        kind = part.get("type")
        if text:
            text.append("\n\n")
        if kind == "reasoning":
            body = part.get("content") or ""
            if thinking:
                text.append("Thinking", style="dim italic")
                _indented(text, body, style="dim italic", first_prefix=INDENT)
            else:
                text.append(f"Thinking ({len(body.splitlines() or [body])} lines, t to show)", style="dim italic")
        elif kind == "text":
            text.append(f"{DOT} ", style="bold")
            text.append(part.get("content") or "")
        elif kind == "tool_call":
            result = results.get(part.get("id"))
            tool_line(text, part.get("name"), part.get("arguments"), result, expand)
            if result is not None:
                seqs.append(result["seq"])
    notes = []
    if event.get("prefilled"):
        notes.append("history")
    if event.get("retries"):
        notes.append(f"{event['retries']} retries")
    if notes:
        text.append(f"\n  ({', '.join(notes)})", style="dim")
    return Entry(text, seqs)


def render_event(
    event: dict[str, Any], results: dict[str, dict[str, Any]], called: set[str], expand: bool, thinking: bool
) -> Entry | None:
    """One event as a stream entry; tool results show under their call, so they and turn spans give none."""
    kind = event["event"]
    span = event.get("type")
    if kind == "chat":
        return _render_chat(event, results, expand, thinking)
    if kind == "state" or span == "turn" or (kind == "execute_tool" and event.get(TOOL_PREFIX + "call.id") in called):
        return None
    text = Text()
    if kind == "span_begin" and span == "run":
        text.append(
            f"run {event.get('run_id')}  chain {event.get('chain')}  attack {event.get('attack')}  "
            f"max_steps {event.get('max_steps')}",
            style="dim",
        )
    elif kind == "span_begin" and span == "session":
        text.append(f"session {event.get('session')} {event.get('name')}  date {event.get('date')}", style="bold cyan")
        text.append(f"  {len(event.get('gen_ai.tool.definitions') or [])} tools", style="dim")
    elif kind == "input":
        text.append(f"> {event.get('content') or ''}", style="on grey19")
        if event.get("prefilled"):
            text.append("  (history)", style="dim")
        if event.get("harness"):
            text.append("  (harness)", style="dim")
    elif kind == "execute_tool":
        tool_line(text, event.get(TOOL_PREFIX + "name"), event.get(TOOL_PREFIX + "call.arguments"), event, expand)
    elif kind == "span_end" and span == "session":
        text.append(f"session ended: {event.get('reason')}", style="bold cyan")
    elif kind == "span_end" and span == "run":
        text.append(
            f"run ended: USD {event.get('sereno.cost_usd')}, {event.get('model_calls')} model calls, "
            f"{event.get('tool_calls')} tool calls, {event.get('duration_s')} s",
            style="bold red" if event.get("reason") in ("error", "stopped") else "dim",
        )
    elif kind == "score":
        score_lines(text, event, f"{DOT} score {event.get('group')} ")
    elif kind == "error":
        text.append(f"{DOT} error {event.get('type')}: {event.get('message')}", style="bold red")
        for i, attempt in enumerate(event.get("attempts") or []):
            text.append(f"\n  {ELBOW} " if i == 0 else f"\n{INDENT}", style="red")
            text.append(
                f"attempt {i + 1}: status {attempt.get('status')}, {attempt.get('duration_s')} s, "
                f"{_one_line(attempt.get('error'), 80)}",
                style="red",
            )
    else:
        text.append(f"{kind} ", style="bold cyan")
        text.append(
            json.dumps({k: v for k, v in event.items() if k not in ENVELOPE}, ensure_ascii=False, default=str),
            style="cyan",
        )
    return Entry(text, [event["seq"]])


def tool_index(events: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Tool results by call id, and the ids of calls a model response made."""
    results = {e.get(TOOL_PREFIX + "call.id"): e for e in events if e["event"] == "execute_tool"}
    called = {p.get("id") for e in events if e["event"] == "chat" for p in _parts(e) if p.get("type") == "tool_call"}
    return results, called


def render_stream(events: list[dict[str, Any]], expand_results: bool, show_thinking: bool) -> list[Entry]:
    """Renders the whole log as stream entries, pairing each tool call with its result by call id."""
    results, called = tool_index(events)
    entries = (render_event(e, results, called, expand_results, show_thinking) for e in events)
    return [e for e in entries if e is not None]


def event_label(event: dict[str, Any]) -> str:
    kind = event.get("event", "?")
    detail = event.get("type") or event.get(TOOL_PREFIX + "name") or event.get("group") or event.get("reason") or ""
    return f"{kind} {detail}".strip()


def detail_text(event: dict[str, Any], diag: list[dict[str, Any]]) -> Text:
    """An event's full JSON, then the diagnostic records written while it was built."""
    text = Text()
    text.append(f"event {event.get('seq')}  {event_label(event)}\n\n", style="bold")
    text.append(json.dumps(event, ensure_ascii=False, indent=2, default=str))
    related = [d for d in diag if d.get("event_id") == event.get("id")]
    text.append(f"\n\ndiagnostic records ({len(related)})\n", style="bold")
    for record in related:
        text.append(json.dumps(record, ensure_ascii=False, default=str) + "\n", style="dim")
    return text


def peek_text(info: RunInfo) -> Text:
    """The peek panel: activity of a working run, result of a finished one, the error of a failed one."""
    text = Text()
    state = info.state()
    text.append(f"{info.name}  ", style="bold")
    text.append(state, style=STATE_STYLE[state])
    if info.model:
        text.append(f"  {info.model}", style="dim")
    if info.attack:
        text.append(f"  attack {info.attack}", style="magenta")
    if info.legacy:
        text.append("\nold log format; the agent view cannot read it.", style="dim")
        return text
    if state == "failed":
        for error in info.errors[-1:]:
            text.append(f"\n{error.get('type')}: {error.get('message')}", style="red")
    if state in ("working", "stopped"):
        text.append(f"\n{info.summary()}")
        if info.last_text:
            text.append(f"\n{DOT} ", style="bold")
            text.append(_one_line(info.last_text, 300))
        if info.last_tool is not None:
            tool = info.last_tool
            text.append("\n")
            tool_line(text, tool.get(TOOL_PREFIX + "name"), tool.get(TOOL_PREFIX + "call.arguments"), tool, False)
    else:
        text.append(f"\nresult: {info.final_text or '(no answer)'}")
        for group, score in info.scores.items():
            text.append("\n")
            score_lines(text, score, f"{group} ")
    if info.marker:
        text.highlight_regex(re.escape(info.marker), style=MARKER_STYLE)
    return text


def row_text(info: RunInfo, frame: int, width: int) -> Text:
    """One run row: state icon, name, badges, summary and age at the right edge."""
    state = info.state()
    if state == "working":
        icon = WORKING_FRAMES[frame % len(WORKING_FRAMES)]
    else:
        icon = "✻" if info.end is None and pid_alive((info.start or {}).get("pid")) else "∙"
    style = "grey50" if info.legacy else STATE_STYLE[state]
    left = Text("  ")
    left.append(icon, style=style)
    left.append(f" {info.name:<28.28} ", style="grey50" if info.legacy else "bold")
    badges = Text()
    if info.session is not None:
        sessions = (info.start or {}).get("sessions")
        badges.append(f"s{info.session}/{sessions} " if sessions else f"s{info.session} ", style="dim")
    if info.attack:
        badges.append(f"attack:{info.attack} ", style="magenta")
    if info.injected:
        badges.append("injected ", style="bold red")
    passed, total = info.task_checks()
    if total:
        badges.append(f"{passed}/{total} ", style="green" if passed == total else "red")
    if info.model and not info.legacy:
        badges.append(f"{info.model.split('/')[-1]} ", style="dim")
    age = format_age(info.age())
    room = max(10, width - left.cell_len - badges.cell_len - len(age) - 2)
    summary = _one_line(info.summary(), room)
    line = left + badges
    line.append(f"{summary:<{room}}", style="grey50" if info.legacy else "")
    line.append(f" {age:>{len(age)}}", style="dim")
    return line


def group_runs(infos: list[RunInfo]) -> dict[str, list[RunInfo]]:
    """Runs by state in display order, newest first inside each group."""
    groups: dict[str, list[RunInfo]] = {g: [] for g in GROUPS}
    for info in infos:
        groups[info.state()].append(info)
    for runs in groups.values():
        runs.sort(key=lambda i: i.path.parent.name, reverse=True)
    return groups


def dispatch_argv(args_text: str) -> list[str]:
    """The command that starts a run for the typed `sereno run` arguments; model runs go through the cost wrapper."""
    args = shlex.split(args_text)
    if not args:
        raise ValueError("type the arguments for sereno run, e.g. kickoff --scripted")
    run = ["uv", "run", "sereno", "run", *args]
    if "--model" not in args:
        return run
    i = args.index("--model")
    if i + 1 >= len(args):
        raise ValueError("--model needs a value")
    return ["uv", "run", "scripts/cost.py", "run", "--label", f"{args[0]}_{args[i + 1]}", "--", *run]


def dispatch(args_text: str, runs_dir: Path, repo: Path) -> Path:
    """Starts a detached run and returns the file its output goes to."""
    argv = dispatch_argv(args_text)
    runs_dir.mkdir(parents=True, exist_ok=True)
    out = runs_dir / f"dispatch-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.log"
    with out.open("ab") as f:
        subprocess.Popen(
            argv, cwd=repo, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True
        )
    return out


class DetailScreen(Screen):
    BINDINGS: ClassVar = [
        Binding("escape", "app.pop_screen", "back"),
        Binding("d", "app.pop_screen", "back", show=False),
        Binding("left", "app.pop_screen", "back", show=False),
        Binding("q", "app.quit", "quit"),
    ]

    def __init__(self, event: dict[str, Any], diag: list[dict[str, Any]]) -> None:
        super().__init__()
        self.event = event
        self.diag = diag

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="detail"):
            yield Static(detail_text(self.event, self.diag), id="detail-body")
        yield Static("esc back  q quit", id="detail-hints")


class TranscriptScreen(Screen):
    CSS = """
    #transcript { width: 1fr; padding: 0 1; }
    #status { dock: bottom; height: 2; padding: 0 1; background: $boost; }
    """
    BINDINGS: ClassVar = [
        Binding("left", "detach", "back"),
        Binding("escape", "detach", "back", show=False),
        Binding("q", "app.quit", "quit"),
        Binding("f", "toggle_follow", "follow"),
        Binding("t", "toggle_thinking", "thinking"),
        Binding("ctrl+o", "toggle_results", "results"),
        Binding("e", "toggle_results", "results", show=False),
        Binding("left_square_bracket", "cursor(-1)", "previous event"),
        Binding("right_square_bracket", "cursor(1)", "next event"),
        Binding("d", "detail", "detail"),
    ]

    def __init__(self, path: Path, follow: bool = True, marker: str | None = None) -> None:
        super().__init__()
        self.path = path
        self.follow = follow
        self.marker_override = re.compile(marker) if marker else None
        self.info = RunInfo(path)
        self.tail = LogTail(path)
        self.events: list[dict[str, Any]] = []
        self.entries = 0
        self.shown = 0
        self.expand_results = False
        self.show_thinking = False
        self.cursor: int | None = None

    def compose(self) -> ComposeResult:
        yield RichLog(id="transcript", wrap=True, markup=False, auto_scroll=self.follow)
        yield Static(id="status")

    def on_mount(self) -> None:
        self.app.title = f"sereno watch {self.path}"
        self._timer = self.set_interval(POLL_SECONDS, self.poll, pause=not self.follow)
        self.poll()

    @property
    def marker(self) -> re.Pattern | None:
        if self.marker_override is not None:
            return self.marker_override
        return re.compile(re.escape(self.info.marker)) if self.info.marker else None

    def poll(self) -> None:
        new = self.tail.read_new()
        for event in new:
            self.info.apply(event)
        self.events += new
        if new:
            self._append()
        if self.info.end is not None or self.info.legacy:
            self._timer.pause()
        self._redraw_status()

    def _write(self, log: RichLog, entry: Entry, cursor_seq: int | None = None) -> int | None:
        text = entry.text
        if self.marker is not None:
            text.highlight_regex(self.marker, style=MARKER_STYLE)
        line = None
        if cursor_seq is not None and cursor_seq in entry.seqs:
            text = text.copy()
            text.stylize(CURSOR_STYLE)
            line = len(log.lines)
        log.write(text)
        log.write(Text(""))
        self.entries += 1
        return line

    def _append(self) -> None:
        """Writes the events not shown yet. A model response whose tool calls are still
        running waits until their results arrive, so nothing already written changes."""
        if self.info.legacy:
            self.redraw()
            return
        log = self.query_one("#transcript", RichLog)
        results, called = tool_index(self.events)
        while self.shown < len(self.events):
            event = self.events[self.shown]
            if self.info.end is None and any(
                p.get("type") == "tool_call" and p.get("id") not in results for p in _parts(event)
            ):
                break
            entry = render_event(event, results, called, self.expand_results, self.show_thinking)
            if entry is not None:
                self._write(log, entry)
            self.shown += 1

    def redraw(self) -> None:
        """Rewrites the whole stream, for a toggle or a cursor move."""
        log = self.query_one("#transcript", RichLog)
        log.clear()
        self.entries = 0
        if self.info.legacy:
            log.write(Text("This run uses the old log format; the agent view cannot show it.", style="dim"))
            return
        cursor_seq = self.cursor_event["seq"] if self.cursor_event is not None and self.cursor is not None else None
        cursor_line = None
        for entry in render_stream(self.events, self.expand_results, self.show_thinking):
            line = self._write(log, entry, cursor_seq)
            cursor_line = line if line is not None else cursor_line
        self.shown = len(self.events)
        if cursor_line is not None and not self.follow:
            log.scroll_to(y=cursor_line, animate=False)

    @property
    def cursor_event(self) -> dict[str, Any] | None:
        if not self.events:
            return None
        index = len(self.events) - 1 if self.cursor is None else self.cursor
        return self.events[max(0, min(index, len(self.events) - 1))]

    def _redraw_status(self) -> None:
        text = self.info.status_line()
        text.append("\n")
        event = self.cursor_event
        if event is not None:
            text.append(f"event {event['seq']} {event_label(event)}  ", style="cyan")
        hints = "← back  f follow  t thinking  ctrl+o results  [ ] event  d detail  q quit"
        text.append(hints + ("  [paused]" if not self.follow else ""), style="dim")
        self.query_one("#status", Static).update(text)

    def action_detach(self) -> None:
        self.app.pop_screen()

    def action_toggle_follow(self) -> None:
        self.follow = not self.follow
        self.query_one("#transcript", RichLog).auto_scroll = self.follow
        if self.follow:
            self.cursor = None
            self._timer.resume()
            self.poll()
            self.redraw()
        else:
            self._timer.pause()
        self._redraw_status()

    def action_toggle_thinking(self) -> None:
        self.show_thinking = not self.show_thinking
        self.redraw()

    def action_toggle_results(self) -> None:
        self.expand_results = not self.expand_results
        self.redraw()

    def action_cursor(self, delta: int) -> None:
        if not self.events:
            return
        current = len(self.events) - 1 if self.cursor is None else self.cursor
        self.cursor = max(0, min(current + delta, len(self.events) - 1))
        if self.follow:
            self.action_toggle_follow()
        self.redraw()
        self._redraw_status()

    def action_detail(self) -> None:
        event = self.cursor_event
        if event is None:
            return
        diag_path = self.path.parent / "diag.jsonl"
        diag = read_events(diag_path) if diag_path.exists() else []
        self.app.push_screen(DetailScreen(event, diag))


class RunListScreen(Screen):
    CSS = """
    #header { height: 1; padding: 0 1; }
    #runs { height: 1fr; border: none; }
    #peek { height: auto; max-height: 50%; padding: 0 2; border-top: solid $accent; display: none; }
    #dispatch { dock: bottom; margin-bottom: 1; }
    #hints { dock: bottom; height: 1; padding: 0 1; }
    """
    BINDINGS: ClassVar = [
        Binding("q", "app.quit", "quit"),
        Binding("escape", "escape", "quit", show=False),
        Binding("space", "peek", "peek"),
        Binding("right", "attach", "attach"),
    ]

    def __init__(self, runs_dir: Path, marker: str | None = None) -> None:
        super().__init__()
        self.runs_dir = runs_dir
        self.marker = marker
        self.watches: dict[Path, RunWatch] = {}
        self.collapsed: set[str] = set()
        self.frame = 0
        self.peeking = False

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield OptionList(id="runs")
        yield Static(id="peek")
        yield Static("↑↓ select  space peek  enter/→ attach  tab dispatch  q quit", id="hints")
        yield Input(placeholder="sereno run arguments, e.g. kickoff --scripted  (enter to start)", id="dispatch")

    def on_mount(self) -> None:
        self.app.title = f"sereno agents {self.runs_dir}"
        self.query_one("#runs", OptionList).focus()
        self.poll()
        self.set_interval(LIST_POLL_SECONDS, self.poll)

    def poll(self) -> None:
        for path in self.runs_dir.glob("*/events.jsonl"):
            if path not in self.watches:
                self.watches[path] = RunWatch(path)
        for watch in self.watches.values():
            watch.poll()
        self.frame += 1
        self.refresh_rows()

    def on_resize(self) -> None:
        self.refresh_rows()

    @property
    def infos(self) -> list[RunInfo]:
        return [w.info for w in self.watches.values() if w.tail is not None]

    def refresh_rows(self) -> None:
        runs = self.query_one("#runs", OptionList)
        selected = runs.highlighted_option.id if runs.highlighted_option is not None else None
        groups = group_runs(self.infos)
        width = max(60, runs.size.width - 2)
        options: list[Option] = []
        for group in GROUPS:
            members = groups[group]
            if not members:
                continue
            arrow = "▸" if group in self.collapsed else "▾"
            title = Text(f"{arrow} {group.capitalize()} ({len(members)})", style=f"bold {STATE_STYLE[group]}")
            options.append(Option(title, id=f"group:{group}"))
            if group not in self.collapsed:
                options += [Option(row_text(i, self.frame, width), id=str(i.path)) for i in members]
        runs.set_options(options)
        if selected is not None:
            try:
                runs.highlighted = runs.get_option_index(selected)
            except Exception:
                runs.highlighted = None
        if runs.highlighted is None and options:
            runs.highlighted = 1 if len(options) > 1 else 0
        header = Text(" · ").join(
            Text(f"{len(groups[g])} {g}", style=STATE_STYLE[g]) for g in ("working", "completed", "failed", "stopped")
        )
        self.query_one("#header", Static).update(header)
        self._update_peek()

    def selected_info(self) -> RunInfo | None:
        option = self.query_one("#runs", OptionList).highlighted_option
        if option is None or option.id is None or option.id.startswith("group:"):
            return None
        watch = self.watches.get(Path(option.id))
        return watch.info if watch is not None else None

    def _update_peek(self) -> None:
        peek = self.query_one("#peek", Static)
        info = self.selected_info()
        peek.display = self.peeking and info is not None
        if peek.display and info is not None:
            peek.update(peek_text(info))

    def on_option_list_option_highlighted(self, _event: OptionList.OptionHighlighted) -> None:
        self._update_peek()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id or ""
        if option_id.startswith("group:"):
            group = option_id.split(":", 1)[1]
            self.collapsed ^= {group}
            self.refresh_rows()
        else:
            self.action_attach()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        from sereno.runner import REPO

        try:
            out = dispatch(event.value, self.runs_dir, REPO)
        except ValueError as e:
            self.notify(str(e), severity="error")
            return
        event.input.clear()
        self.notify(f"started: sereno run {event.value}  (output: {out.name})")
        self.query_one("#runs", OptionList).focus()

    def action_peek(self) -> None:
        self.peeking = not self.peeking
        self._update_peek()

    def action_attach(self) -> None:
        info = self.selected_info()
        if info is not None:
            self.app.push_screen(TranscriptScreen(info.path, follow=True, marker=self.marker))

    def action_escape(self) -> None:
        if isinstance(self.focused, Input):
            self.query_one("#runs", OptionList).focus()
        elif self.peeking:
            self.action_peek()
        else:
            self.app.exit()


class AgentView(App):
    """The run list, optionally with one run's transcript already attached on top of it."""

    def __init__(
        self, runs_dir: Path, marker: str | None = None, attach: Path | None = None, follow: bool = True
    ) -> None:
        super().__init__()
        self.runs_dir = runs_dir
        self.marker = marker
        self.attach = attach
        self.follow = follow

    def on_mount(self) -> None:
        self.push_screen(RunListScreen(self.runs_dir, self.marker))
        if self.attach is not None:
            self.push_screen(TranscriptScreen(self.attach, follow=self.follow, marker=self.marker))


def run_agent_view(runs_dir: Path, marker: str | None = None) -> None:
    AgentView(runs_dir, marker=marker).run()


def run_viewer(path: Path, follow: bool = True, marker: str | None = None) -> None:
    """Opens one run attached; left arrow goes to the list of the runs next to it."""
    AgentView(path.parent.parent, marker=marker, attach=path, follow=follow).run()
