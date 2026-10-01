"""Live terminal viewer for a run's event log.

    uv run python -m sereno.viewer <events.jsonl> [--no-follow] [--marker REGEX]

Tails the append-only JSONL log written by `sereno.events.EventLog` and shows one
scrolling stream in the style of the Claude Code CLI: user prompts, model text,
each tool call with its result beneath, and a status bar with cost, counts,
status and grade. The world snapshot is a panel toggled with w.

Keys: q quit, f follow on/off, w world panel, t thinking, ctrl+o or e expand results.
"""

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import RichLog, Static

POLL_SECONDS = 0.3
RESULT_PREVIEW_LINES = 3
ARG_MAX_CHARS = 40
RESULT_VALUE_MAX_CHARS = 100
MARKER_STYLE = "bold black on yellow"
DOT = "●"
ELBOW = "⎿"
INDENT = "    "


class LogTail:
    """Reads complete new lines from a growing file, keeping a partial last line for later."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._partial = b""

    def read_new(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open("rb") as f:
            f.seek(self._offset)
            chunk = f.read()
        self._offset += len(chunk)
        lines = (self._partial + chunk).split(b"\n")
        self._partial = lines.pop()
        return [json.loads(line) for line in lines if line.strip()]


class RunState:
    """Status bar data folded from events."""

    def __init__(self) -> None:
        self.run_id: str | None = None
        self.scenario: str | None = None
        self.model: str | None = None
        self.provider: str | None = None
        self.session = 1
        self.turn = 1
        self.step: int | None = None
        self.cost = 0.0
        self.model_calls = 0
        self.tool_calls = 0
        self.status = "waiting"
        self.grade: dict[str, Any] | None = None
        self.world: dict[str, Any] | None = None
        self.previous_world: dict[str, Any] | None = None

    def apply(self, event: dict[str, Any]) -> None:
        self.run_id = event.get("run_id", self.run_id)
        self.session = event.get("session", self.session)
        self.turn = event.get("turn", self.turn)
        if event.get("step") is not None:
            self.step = event["step"]
        kind = event["event"]
        if kind == "run_start":
            self.scenario = event.get("scenario")
            self.model = event.get("model")
            self.provider = event.get("provider")
            self.status = "running"
        elif kind == "model_response":
            self.model_calls += 1
            self.cost += (event.get("usage") or {}).get("cost") or 0.0
        elif kind == "tool_result":
            self.tool_calls += 1
        elif kind == "world_state":
            self.previous_world = self.world
            self.world = event.get("state")
        elif kind == "session_end":
            self.status = event.get("reason") or "ended"
        elif kind == "grade":
            self.grade = event

    def status_line(self) -> Text:
        text = Text()
        model = self.model or "-"
        text.append(f"{model}@{self.provider}" if self.provider else model, style="bold")
        step = "-" if self.step is None else str(self.step)
        text.append(f"  session {self.session} turn {self.turn} step {step}", style="dim")
        text.append(f"  USD {self.cost:.6f}")
        text.append(f"  {self.model_calls} model calls  {self.tool_calls} tool calls", style="dim")
        style = {"waiting": "dim", "running": "yellow", "final_answer": "green"}.get(self.status, "red")
        text.append("  ")
        text.append(self.status, style=f"bold {style}")
        if self.grade is not None:
            passed = bool(self.grade.get("passed"))
            checks = self.grade.get("checks") or {}
            text.append("  grade ")
            text.append("PASS" if passed else "FAIL", style="bold green" if passed else "bold red")
            text.append(f" {sum(bool(v) for v in checks.values())}/{len(checks)}", style="dim")
        return text


def _short(value: Any) -> str:
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    raw = raw.replace("\n", " ")
    if isinstance(value, str):
        raw = json.dumps(raw, ensure_ascii=False)
    return raw if len(raw) <= ARG_MAX_CHARS else raw[: ARG_MAX_CHARS - 1] + "…"


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
    return raw if len(raw) <= limit else raw[: limit - 1] + "\u2026"


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
    """The lines under a tool call: the result, or a pending marker while the tool runs."""
    text = Text()
    if result is None:
        text.append(f"\n  {ELBOW} running…", style="dim")
        return text
    tag = provenance_tag(result.get("provenance"))
    suffix = Text()
    if tag:
        suffix.append(f"  {tag}", style="dim")
    if result.get("state_changed"):
        suffix.append("  state changed", style="yellow")
    if result.get("error"):
        text.append(f"\n  {ELBOW} ")
        text.append(f"Error: {result['error']}", style="red")
        text.append_text(suffix)
        return text
    lines = result_lines(str(result.get("result") or "")) or ["(empty)"]
    shown = lines if expanded else lines[:RESULT_PREVIEW_LINES]
    text.append(f"\n  {ELBOW} {shown[0]}")
    text.append_text(suffix)
    for line in shown[1:]:
        text.append(f"\n{INDENT}{line}")
    if len(lines) > len(shown):
        text.append(f"\n{INDENT}… +{len(lines) - len(shown)} lines (ctrl+o to expand)", style="dim")
    return text


def render_stream(events: list[dict[str, Any]], expand_results: bool, show_thinking: bool) -> list[Text]:
    """Renders the whole log as stream entries, pairing each tool call with its result by call id."""
    results = {e.get("call_id"): e for e in events if e["event"] == "tool_result"}
    entries: list[Text] = []
    for event in events:
        kind = event["event"]
        text = Text()
        if kind == "run_start":
            text.append(
                f"run {event.get('run_id')}  scenario {event.get('scenario')}  "
                f"temperature {event.get('temperature')}  max_steps {event.get('max_steps')}",
                style="dim",
            )
        elif kind == "session_start":
            text.append(f"session {event.get('session')}  date {event.get('date')}", style="bold cyan")
            text.append(f"  {len(event.get('tools') or [])} tools", style="dim")
        elif kind == "user_message":
            text.append(f"> {event.get('text') or ''}", style="on grey19")
        elif kind == "model_response":
            reasoning = event.get("reasoning")
            if reasoning:
                lines = reasoning.splitlines() or [reasoning]
                if show_thinking:
                    text.append("Thinking", style="dim italic")
                    _indented(text, reasoning, style="dim italic", first_prefix=INDENT)
                else:
                    text.append(f"Thinking ({len(lines)} lines, t to show)", style="dim italic")
            if event.get("text"):
                if text:
                    text.append("\n\n")
                text.append(f"{DOT} ", style="bold")
                text.append(event["text"])
            for call in event.get("tool_calls") or []:
                if text:
                    text.append("\n\n")
                text.append(f"{DOT} ", style="bold green")
                text.append(format_call(call.get("name"), call.get("args")), style="bold")
                text.append_text(render_result(results.get(call.get("id")), expand_results))
        elif kind == "tool_result":
            continue
        elif kind == "world_state":
            continue
        elif kind == "session_end":
            text.append(f"session ended: {event.get('reason')}", style="bold cyan")
        elif kind == "grade":
            passed = bool(event.get("passed"))
            text.append(f"{DOT} grade ", style="bold")
            text.append("PASS" if passed else "FAIL", style="bold green" if passed else "bold red")
            for i, (name, ok) in enumerate((event.get("checks") or {}).items()):
                text.append(f"\n  {ELBOW} " if i == 0 else f"\n{INDENT}")
                text.append(("ok   " if ok else "FAIL ") + name, style="green" if ok else "red")
        elif kind == "run_end":
            text.append(
                f"run ended: USD {event.get('cost_usd')}, {event.get('model_calls')} model calls, "
                f"{event.get('tool_calls')} tool calls",
                style="dim",
            )
        else:
            text.append(f"{kind} ", style="bold")
            text.append(
                json.dumps({k: v for k, v in event.items() if k not in _COMMON}, ensure_ascii=False, default=str)
            )
        for key in ("memory_op", "gate"):
            if event.get(key) is not None:
                text.append(
                    f"\n  {ELBOW} {key}: {json.dumps(event[key], ensure_ascii=False, default=str)}", style="cyan"
                )
        if text:
            entries.append(text)
    return entries


_COMMON = {"ts", "seq", "run_id", "session", "turn", "step", "agent_id", "event", "provenance", "memory_op", "gate"}


def _items_by_key(world: dict[str, Any] | None, app: str, collection: str, key: str) -> dict[Any, dict]:
    if not world:
        return {}
    return {item.get(key): item for item in (world.get(app) or {}).get(collection) or []}


def short_address(address: str, limit: int = 20) -> str:
    """The local part of a long email address, the whole address when it is short."""
    if len(address) <= limit or "@" not in address:
        return address
    return address.split("@", 1)[0]


def short_span(start: Any, end: Any) -> str:
    """Formats an ISO start and end as "Tue 13 Oct 10:30-11:15"; falls back to the raw values."""
    try:
        s, e = datetime.fromisoformat(str(start)), datetime.fromisoformat(str(end))
    except ValueError:
        return f"{start} - {end}"
    if s.date() == e.date():
        return f"{s:%a %d %b %H:%M}-{e:%H:%M}"
    return f"{s:%a %d %b %H:%M} - {e:%a %d %b %H:%M}"


def render_world(world: dict[str, Any] | None, previous: dict[str, Any] | None) -> Text:
    text = Text()
    if world is None:
        text.append("no world snapshot yet", style="dim")
        return text
    owner = world.get("owner") or {}
    text.append(f"today {world.get('today')}\n", style="bold")
    text.append(f"{owner.get('name')} <{owner.get('email')}>\n", style="dim")

    def section(title: str, app: str, collection: str, key: str, line, order=None) -> None:
        text.append(f"\n{title}\n", style="bold underline")
        now = _items_by_key(world, app, collection, key)
        before = _items_by_key(previous, app, collection, key) if previous is not None else now
        if not now:
            text.append("  (empty)\n", style="dim")
        items = sorted(now.items(), key=lambda kv: order(kv[1])) if order else now.items()
        for item_key, item in items:
            changed = before.get(item_key) != item
            text.append((f"{DOT} " if changed else "  ") + line(item) + "\n", style="bold yellow" if changed else "")

    def email_line(e: dict) -> str:
        who = ", ".join(e.get("to") or []) if e.get("folder") == "sent" else str(e.get("sender"))
        unread = "" if e.get("read", True) else " (unread)"
        return f"{e.get('folder')}  {short_address(who)}  {e.get('subject')}{unread}"

    def event_line(e: dict) -> str:
        return f"{short_span(e.get('start'), e.get('end'))}  {e.get('title')}"

    section("mail", "mail", "emails", "id", email_line)
    section("calendar", "calendar", "events", "id", event_line, order=lambda e: str(e.get("start")))
    section("files", "files", "files", "path", lambda f: str(f.get("path")))
    return text


class RunViewer(App):
    CSS = """
    #transcript { width: 1fr; padding: 0 1; }
    #world-pane { width: 60; display: none; border-left: solid $primary-muted; padding: 0 1; }
    #world-pane.shown { display: block; }
    #status { dock: bottom; height: 2; padding: 0 1; background: $boost; }
    """
    BINDINGS: ClassVar = [
        Binding("q", "quit", "quit"),
        Binding("f", "toggle_follow", "follow"),
        Binding("w", "toggle_world", "world"),
        Binding("t", "toggle_thinking", "thinking"),
        Binding("ctrl+o", "toggle_results", "results"),
        Binding("e", "toggle_results", "results", show=False),
    ]

    def __init__(self, path: Path, follow: bool = True, marker: str | None = None) -> None:
        super().__init__()
        self.path = path
        self.follow = follow
        self.marker = re.compile(marker) if marker else None
        self.state = RunState()
        self.tail = LogTail(path)
        self.events: list[dict[str, Any]] = []
        self.entries = 0
        self.expand_results = False
        self.show_thinking = False

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield RichLog(id="transcript", wrap=True, markup=False, auto_scroll=True)
            with VerticalScroll(id="world-pane"):
                yield Static(id="world")
        yield Static(id="status")

    def on_mount(self) -> None:
        self.title = f"sereno watch {self.path}"
        self.poll()
        self._timer = self.set_interval(POLL_SECONDS, self.poll, pause=not self.follow)

    def poll(self) -> None:
        new = self.tail.read_new()
        for event in new:
            self.state.apply(event)
        self.events += new
        if new:
            self.redraw()
            if any(e["event"] == "world_state" for e in new):
                self._redraw_world()
        self._redraw_status()

    def redraw(self) -> None:
        log = self.query_one("#transcript", RichLog)
        log.clear()
        entries = render_stream(self.events, self.expand_results, self.show_thinking)
        for entry in entries:
            if self.marker is not None:
                entry.highlight_regex(self.marker, style=MARKER_STYLE)
            log.write(entry)
            log.write(Text(""))
        self.entries = len(entries)

    def _redraw_world(self) -> None:
        world = render_world(self.state.world, self.state.previous_world)
        if self.marker is not None:
            world.highlight_regex(self.marker, style=MARKER_STYLE)
        self.query_one("#world", Static).update(world)

    def _redraw_status(self) -> None:
        text = self.state.status_line()
        text.append("\n")
        hints = "q quit  f follow  w world  t thinking  ctrl+o results"
        text.append(hints + ("  [paused]" if not self.follow else ""), style="dim")
        self.query_one("#status", Static).update(text)

    def action_toggle_follow(self) -> None:
        self.follow = not self.follow
        self.query_one("#transcript", RichLog).auto_scroll = self.follow
        if self.follow:
            self._timer.resume()
            self.poll()
        else:
            self._timer.pause()
        self._redraw_status()

    def action_toggle_world(self) -> None:
        self.query_one("#world-pane").toggle_class("shown")

    def action_toggle_thinking(self) -> None:
        self.show_thinking = not self.show_thinking
        self.redraw()

    def action_toggle_results(self) -> None:
        self.expand_results = not self.expand_results
        self.redraw()


def run_viewer(path: Path, follow: bool = True, marker: str | None = None) -> None:
    RunViewer(path, follow=follow, marker=marker).run()


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch a run's event log live.")
    parser.add_argument("path", type=Path)
    parser.add_argument("--no-follow", action="store_true", help="read the log once and stop polling")
    parser.add_argument("--marker", help="regex to highlight wherever it appears (poison markers)")
    args = parser.parse_args()
    run_viewer(args.path, follow=not args.no_follow, marker=args.marker)


if __name__ == "__main__":
    main()
