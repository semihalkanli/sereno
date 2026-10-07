"""Follow a context-eval run live from its events.jsonl files, or replay it afterwards.

Per model call it shows the thinking summary, the reply text, the commands, what the model saw of their output (and
what an intervention changed in it), memory changes, interventions, exposures, memory recall, tokens and cost.

Several agents can share one session log (an orchestrator and the subagents it starts). Every event may carry an
`agent_id`; an event without one belongs to the agent `main`, which is the whole single-agent case. Three event kinds
describe the agents themselves:

- `agent_start`: `agent_id`, `parent_id` (the agent that started it; none for the root), `role`, `model`, `task`
- `agent_message`: `agent_id` (sender), `to` (receiver), `content`: a delegation, a handoff or a returned result
- `agent_end`: `agent_id`, `status`, `final`

Once an agent other than `main` appears, every line is labelled with its agent and indented under its parent, so
interleaved parallel subagents stay readable, and the session summary splits calls and cost per agent.
"""

import difflib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from sereno.context_eval.engine import message_text, reasoning_text

HEAD_LINES, TAIL_LINES, PROMPT_LINES = 20, 10, 15
MAIN = "main"
MARKER_STYLE = "bold white on red"
AGENT_STYLES = ["bright_blue", "bright_green", "bright_magenta", "bright_yellow", "bright_cyan", "orange1"]


def first_line(path: Path) -> str:
    with path.open("rb") as stream:
        line = stream.readline()
    return line.decode() if line.endswith(b"\n") else ""


def campaign_root(path: Path) -> Path | None:
    """The nearest directory at or above path that holds a campaign manifest."""
    for directory in [path, *path.parents]:
        if (directory / "manifest.json").is_file():
            return directory
    return None


def session_logs(path: Path) -> list[Path]:
    """Event logs under path in the order their sessions started; a shared session copy is listed once."""
    if path.is_file():
        return [path]
    if (path / "events.jsonl").is_file():
        return [path / "events.jsonl"]
    found = []
    for log in path.rglob("events.jsonl"):
        head = first_line(log)
        if head:
            found.append((json.loads(head).get("ts", ""), str(log), head, log))
    logs, seen = [], set()
    for _, _, head, log in sorted(found):
        if head not in seen:
            seen.add(head)
            logs.append(log)
    return logs


def campaign_markers(root: Path | None) -> list[str]:
    """Markers of every configured intervention, longest first so a marker inside another is not split."""
    if root is None:
        return []
    config = json.loads((root / "manifest.json").read_text()).get("config") or {}
    markers = {
        item["marker"] for items in (config.get("variants") or {}).values() for item in items if item.get("marker")
    }
    return sorted(markers, key=len, reverse=True)


def session_label(log: Path, root: Path | None) -> str:
    directory = log.parent
    if root is None:
        return directory.name
    try:
        parts = directory.relative_to(root).parts
    except ValueError:
        return directory.name
    return " / ".join(p for p in parts if p not in ("sessions", "arms", "cases"))


def clipped(value: str, head: int, tail: int) -> str:
    lines = value.splitlines()
    if len(lines) <= head + tail:
        return value
    hidden = f"... {len(lines) - head - tail} lines hidden (--full shows all) ..."
    return "\n".join([*lines[:head], hidden, *lines[len(lines) - tail :]])


def tool_commands(message: dict) -> list[str]:
    commands = []
    for call in message.get("tool_calls") or []:
        arguments = (call.get("function") or {}).get("arguments") or "{}"
        try:
            commands.append(json.loads(arguments).get("command", arguments))
        except (json.JSONDecodeError, AttributeError):
            commands.append(arguments)
    return commands


class Tail:
    """New complete lines of a growing JSONL file; a partly written last line waits for its newline.

    Bytes, not text: a read can stop inside a multi-byte character, which only a complete line may decode.
    """

    def __init__(self, path: Path):
        self.path, self.offset, self.pending = path, 0, b""

    def read(self) -> list[dict]:
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            chunk = stream.read()
            self.offset = stream.tell()
        lines = (self.pending + chunk).split(b"\n")
        self.pending = lines.pop()
        return [json.loads(line) for line in lines if line.strip()]


@dataclass
class Agent:
    id: str
    parent: str | None = None
    role: str = ""
    model: str = ""
    style: str = AGENT_STYLES[0]
    calls: int = 0
    cost: float = 0.0
    shown_commands: list[str] = field(default_factory=list)
    raw_outputs: list[str] = field(default_factory=list)
    exposed: set[str] = field(default_factory=set)
    recalled: set[tuple[str, str]] = field(default_factory=set)


class SessionView:
    """Rendering state for one session's event stream, kept per agent."""

    def __init__(self, console: Console, log: Path, label: str, markers: list[str], full: bool, only=None):
        self.console, self.log, self.label, self.markers, self.full = console, log, label, markers, full
        self.only = set(only or ())
        self.agents: dict[str, Agent] = {}
        self.started: datetime | None = None
        self.ended = False

    def agent(self, agent_id: str) -> Agent:
        if agent_id not in self.agents:
            self.agents[agent_id] = Agent(agent_id, style=AGENT_STYLES[len(self.agents) % len(AGENT_STYLES)])
        return self.agents[agent_id]

    def depth(self, agent: Agent) -> int:
        depth, parent = 0, agent.parent
        while parent is not None and parent in self.agents and depth < len(self.agents):
            depth, parent = depth + 1, self.agents[parent].parent
        return depth

    def multi(self) -> bool:
        # A named agent means an agent layout, so even its first lines carry labels.
        return any(agent_id != MAIN for agent_id in self.agents)

    def put(self, agent: Agent | None, renderable) -> None:
        """Print under the agent's indentation once the session has more than one agent."""
        if agent is not None and self.multi():
            renderable = Padding(renderable, (0, 0, 0, 2 * self.depth(agent)))
        self.console.print(renderable)

    def tag(self, agent: Agent | None) -> Text:
        if agent is None or not self.multi():
            return Text()
        return Text(f"[{agent.id}{' ' + agent.role if agent.role else ''}] ", style=f"bold {agent.style}")

    def line(self, agent: Agent | None, value: str, style: str = "") -> None:
        text = self.tag(agent)
        text.append_text(self.text(value, style))
        self.put(agent, text)

    def panel(self, agent: Agent | None, body: str, title: str, border: str, style: str = "") -> None:
        self.put(
            agent, Panel(self.text(body, style), title=self.tag(agent) + title, title_align="left", border_style=border)
        )

    def text(self, value: str, style: str = "") -> Text:
        text = Text(value, style=style)
        text.highlight_words(self.markers, MARKER_STYLE)
        return text

    def clip(self, value: str, head: int = HEAD_LINES, tail: int = TAIL_LINES) -> str:
        return value if self.full else clipped(value, head, tail)

    def clock(self, event: dict) -> str:
        stamp = datetime.fromisoformat(event["ts"])
        if self.started is None:
            self.started = stamp
        seconds = int((stamp - self.started).total_seconds())
        return f"{stamp.astimezone():%H:%M:%S} +{seconds // 60}:{seconds % 60:02d}"

    def show(self, event: dict) -> None:
        scoped = event["kind"] in AGENT_SCOPED or "agent_id" in event
        agent = self.agent(event.get("agent_id") or MAIN) if scoped else None
        if agent is not None and self.only and agent.id not in self.only:
            if event["kind"] == "agent_start":
                self.on_agent_start(event, agent, quiet=True)
            return
        handler = getattr(self, f"on_{event['kind']}", None)
        if handler is not None:
            handler(event, agent)

    def on_session_start(self, event: dict, agent) -> None:
        self.clock(event)
        image = (event.get("image") or {}).get("reference", "")
        self.console.rule(Text(f"{self.label}  task {event.get('task_id')}", style="bold cyan"))
        self.console.print(Text(f"session {event.get('session_id')}  image {image}", style="dim"))

    def on_agent_start(self, event: dict, agent: Agent, quiet: bool = False) -> None:
        agent.parent, agent.role, agent.model = (
            event.get("parent_id"),
            event.get("role") or "",
            event.get("model") or "",
        )
        if quiet:
            return
        started_by = f" by {agent.parent}" if agent.parent else ""
        details = ", ".join(v for v in (agent.role, agent.model) if v)
        self.line(agent, f"agent {agent.id} started{started_by}{f' ({details})' if details else ''}", "bold")
        if event.get("task"):
            self.panel(agent, self.clip(event["task"], PROMPT_LINES, 0), "task", agent.style)

    def on_agent_message(self, event: dict, agent: Agent) -> None:
        self.panel(
            agent,
            self.clip(message_text([event]) or str(event.get("content", ""))),
            f"to {event.get('to')}",
            agent.style,
        )

    def on_agent_end(self, event: dict, agent: Agent) -> None:
        if event.get("final"):
            self.panel(agent, event["final"], "final reply", agent.style)
        self.line(
            agent, f"agent {agent.id} ended: {event.get('status')}  calls {agent.calls}  ${agent.cost:.4f}", "bold"
        )

    def on_context_sent(self, event: dict, agent) -> None:
        if event.get("offset", 0) == 0:
            if event["step"] > 1 and (agent is None or agent.calls > 0):
                self.line(agent, f"context rebuilt from the start: {event['message_count']} messages", "yellow")
            for message in event["messages"]:
                if message.get("role") in ("system", "user"):
                    self.panel(agent, self.clip(message_text([message]), PROMPT_LINES, 0), message["role"], "blue")
        # Content stays in the context once seen, so each exposure and recall route is shown when it first appears.
        if fresh := [i for i in event.get("matched_interventions") or [] if i not in agent.exposed]:
            agent.exposed.update(fresh)
            self.line(agent, f"exposure: the model now sees content of {', '.join(fresh)}", "bold red")
        for intervention, routes in (event.get("memory_recall") or {}).items():
            new = {route: evidence for route, evidence in routes.items() if (intervention, route) not in agent.recalled}
            agent.recalled.update((intervention, route) for route in new)
            if new:
                how = ", ".join(f"{route} ({evidence})" for route, evidence in new.items())
                self.line(agent, f"memory recall: {intervention} via {how}", "bold yellow")

    def on_model_result(self, event: dict, agent) -> None:
        message = event.get("message") or {}
        cost = event.get("cost_usd") or 0.0
        agent.calls += 1
        agent.cost += cost
        total = sum(a.cost for a in self.agents.values())
        response = (message.get("extra") or {}).get("response") or {}
        usage = response.get("usage") or {}
        meta = [self.clock(event)]
        if response.get("model") or agent.model:
            meta.append(response.get("model") or agent.model)
        if usage:
            meta.append(
                f"in {usage.get('prompt_tokens', 0)} (cache read {usage.get('cache_read_input_tokens') or 0}, "
                f"write {usage.get('cache_creation_input_tokens') or 0}) out {usage.get('completion_tokens', 0)}"
            )
        meta.append(f"${cost:.4f}  total ${total:.4f}")
        header = self.tag(agent) + Text(f"● step {event['step']}", style="bold")
        if self.multi():
            header.append(f" (call {agent.calls})", style="bold")
        header.append("   " + "   ".join(meta), style="dim")
        if event.get("format_error"):
            header.append("   format error: the reply had no valid tool call", style="bold red")
        self.console.print()
        self.put(agent, header)
        if thinking := reasoning_text(message):
            self.panel(agent, thinking, "thinking", "magenta", "italic")
        elif message.get("thinking_blocks"):
            self.line(agent, "thinking hidden by the provider (display omitted)", "dim italic")
        if (reply := message_text([message])).strip():
            self.put(agent, self.text(reply))
        agent.shown_commands = []
        for command in tool_commands(message):
            self.show_command(agent, command)

    def show_command(self, agent: Agent, command: str) -> None:
        agent.shown_commands.append(command)
        self.put(agent, Syntax(f"$ {command}", "bash", word_wrap=True, background_color="default"))

    def on_action(self, event: dict, agent) -> None:
        # Scripted sessions run commands no tool call showed; a command already shown is not repeated.
        if event["command"] in agent.shown_commands:
            agent.shown_commands.remove(event["command"])
        else:
            self.show_command(agent, event["command"])

    def on_raw_output(self, event: dict, agent) -> None:
        agent.raw_outputs.append(event.get("output", ""))
        if event.get("exception_info"):
            self.line(agent, f"command did not finish normally: {event['exception_info']}", "bold red")

    def on_observation(self, event: dict, agent) -> None:
        for index, message in enumerate(event.get("messages") or []):
            extra = message.get("extra") or {}
            seen = extra.get("raw_output", message_text([message]))
            raw = agent.raw_outputs[index] if index < len(agent.raw_outputs) else None
            code = extra.get("returncode")
            title = f"output (exit {code})" if code is not None else "output"
            self.panel(agent, self.clip(seen), title, "green" if code in (0, None) else "red")
            if raw is not None and raw != seen:
                added = [
                    line[2:] for line in difflib.ndiff(raw.splitlines(), seen.splitlines()) if line.startswith("+ ")
                ]
                self.panel(
                    agent, "\n".join(added) or "(text removed)", "added to this output by an intervention", "bold red"
                )
        agent.raw_outputs = []

    def on_intervention(self, event: dict, agent) -> None:
        where = event.get("path") or "command output"
        self.line(
            agent,
            f"intervention {event['intervention_id']} fired: {event.get('method')} at {event.get('phase')} -> {where}"
            f" (occurrence {event.get('occurrence')})",
            "bold red",
        )

    def on_intervention_skipped(self, event: dict, agent) -> None:
        self.line(agent, f"intervention {event['intervention_id']} skipped: {event.get('reason')}", "yellow")

    def on_intervention_commit(self, event: dict, agent) -> None:
        self.line(agent, f"planted files committed: {', '.join(event.get('paths') or {})}", "dim")

    def on_memory_change(self, event: dict, agent) -> None:
        before, after = event.get("before"), event.get("after")
        diff = list(difflib.unified_diff((before or "").splitlines(), (after or "").splitlines(), lineterm=""))[2:]
        body = Text()
        for line in diff:
            style = "green" if line.startswith("+") else "red" if line.startswith("-") else "dim"
            body.append_text(self.text(line + "\n", style))
        action = "created" if before is None else "deleted" if after is None else "changed"
        title = self.tag(agent) + f"memory {action} by {event.get('origin')}: {event.get('path')}"
        self.put(agent, Panel(body, title=title, title_align="left", border_style="yellow"))

    def on_memory_violation(self, event: dict, agent) -> None:
        self.line(agent, f"memory violation: {event.get('message')}", "bold red")

    def on_error(self, event: dict, agent) -> None:
        self.line(agent, f"error {event.get('error_type')}: {event.get('message')}", "bold red")

    def on_session_end(self, event: dict, agent) -> None:
        self.ended = True
        directory = self.log.parent
        summary = f"session end  {event.get('status')}  patch {event.get('patch_status')}  {self.clock(event)}"
        result = directory / "result.json"
        if result.is_file():
            data = json.loads(result.read_text())
            summary += f"  exit {data.get('exit_status')}  steps {data.get('steps')}  ${data.get('cost_usd') or 0:.4f}"
            if data.get("final"):
                self.panel(None, data["final"], "final reply", "cyan")
        grade = directory / "grade.json"
        if grade.is_file():
            data = json.loads(grade.read_text())
            summary += f"  grade {data.get('status')} reward {data.get('reward')}"
        if self.multi():
            for each in self.agents.values():
                self.line(each, f"calls {each.calls}  ${each.cost:.4f}", "bold")
        self.console.rule(Text(summary, style="bold cyan"))


# Events that always belong to an agent; the others belong to one only when they name it.
AGENT_SCOPED = {
    "agent_start",
    "agent_message",
    "agent_end",
    "context_sent",
    "model_result",
    "action",
    "raw_output",
    "observation",
}


def watch(path: Path, follow=False, full=False, agents=(), interval=0.5, console=None) -> int:
    """Print every session under path; with follow, keep printing new events and new sessions until interrupted."""
    console = console or Console(highlight=False)
    path = path.expanduser().resolve()
    if not path.exists():
        raise ValueError(f"{path} does not exist")
    root = campaign_root(path if path.is_dir() else path.parent)
    markers = campaign_markers(root)
    single = path.is_file() or (path / "events.jsonl").is_file()
    views: dict[Path, tuple[Tail, SessionView]] = {}
    current, waiting = None, False
    try:
        while True:
            for log in session_logs(path):
                if log not in views:
                    view = SessionView(console, log, session_label(log, root), markers, full, agents)
                    views[log] = (Tail(log), view)
            for log, (tail, view) in views.items():
                events = tail.read()
                if events and current not in (None, log):
                    console.rule(Text(view.label, style="cyan"), style="dim")
                for event in events:
                    current = log
                    view.show(event)
            if not follow or (single and views and all(view.ended for _, view in views.values())):
                return 0
            if not views and not waiting:
                waiting = True
                console.print(Text(f"waiting for sessions under {path}", style="dim"))
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
