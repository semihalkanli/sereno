"""Bounded intervention hooks, exact observation evidence, and patch separation."""

import json
import random
import re
from collections import Counter
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from sereno.context_eval.config import fingerprint
from sereno.context_eval.environment import commit_planted
from sereno.context_eval.memory import MemoryViolation, owner

MEMORY_ROOT = re.compile(r"/memories(?![\w.-])")
SUBMIT = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
# Lines as git splits them: only at "\n", so CR, form feed and Unicode separators stay inside a line.
LINES = re.compile(r"[^\n]*\n|[^\n]+")


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")


def message_text(messages: list[dict]) -> str:
    """Read actual visible content, not JSON-escaped envelopes or internal model metadata."""
    texts = []
    for message in messages:
        content = message.get("content", message.get("output", ""))
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(
                part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
    return "\n".join(texts)


def reasoning_text(message: dict) -> str:
    """Reasoning the provider returned: `reasoning` (OpenRouter) or `reasoning_content` (litellm), possibly nested."""
    response = (message.get("extra") or {}).get("response")
    # A format error from litellm can carry the response as its repr string.
    choices = (response.get("choices") if isinstance(response, dict) else None) or [{}]
    for source in (message, message.get("provider_specific_fields") or {}, choices[0].get("message") or {}):
        for key in ("reasoning", "reasoning_content"):
            if isinstance(source.get(key), str) and source[key]:
                return source[key]
    return ""


def assistant_messages(events: list[dict]) -> list[dict]:
    """Model replies: model_result events, else assistant messages in the last sent context (older scripted logs)."""
    if results := [e["message"] for e in events if e["kind"] == "model_result"]:
        return results
    contexts = sent_contexts(events)
    return [m for m in contexts[-1] if m.get("role") == "assistant"] if contexts else []


def executed_commands(events: list[dict]) -> str:
    return "\n".join(e["command"] for e in events if e["kind"] == "action")


def serialize(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class EventLog:
    """Append-only session events, each stamped with the model call (`step`) it belongs to."""

    def __init__(self, path: Path, run_id: str):
        self.path, self.run_id = path, run_id
        self.sequence = 0
        self.step = 0
        self.kinds = Counter()

    def emit(self, kind: str, **data) -> str:
        self.sequence += 1
        self.kinds[kind] += 1
        event_id = f"e{self.sequence:06d}"
        record = {
            "schema_version": "1.0",
            "id": event_id,
            "run_id": self.run_id,
            "ts": datetime.now(UTC).isoformat(),
            "kind": kind,
            "step": self.step,
            **data,
        }
        with self.path.open("a") as stream:
            stream.write(serialize(record) + "\n")
        return event_id


def sent_contexts(events: list[dict]) -> list[list[dict]]:
    """Rebuild every context sent to the model from delta records, verifying message count and hash."""
    contexts, current = [], []
    for event in events:
        if event["kind"] != "context_sent":
            continue
        # Records without an offset predate delta logging and hold the full list.
        offset = event.get("offset", 0)
        if offset not in (0, len(current)):
            raise ValueError(f"context {event['id']} offset does not follow the previous context")
        current = current[:offset] + event["messages"]
        if "messages_sha256" in event and (
            len(current) != event["message_count"] or fingerprint(serialize(current)) != event["messages_sha256"]
        ):
            raise ValueError(f"context {event['id']} does not match its logged count and hash")
        contexts.append(current)
    return contexts


def intervention_catalog(events) -> dict:
    """Per intervention: how to recognise its content and which channel and objective it belongs to."""
    return {
        e.id: {
            "marker": e.marker,
            "copy_patterns": e.copy_patterns,
            "method": e.method,
            "channel": e.channel,
            "phase": e.phase,
            "objective": e.objective,
            "family": e.family,
            "intended": e.intended,
            "path": e.path,
        }
        for e in events
    }


# What Jinja's tojson escapes beyond JSON, as mini-swe renders observations.
HTML_SAFE = str.maketrans({"<": "\\u003c", ">": "\\u003e", "&": "\\u0026", "'": "\\u0027"})


def renderings(line: str) -> set[str]:
    """A line as an observation can carry it: raw, JSON-escaped (scripted) or escaped by tojson (mini-swe)."""
    escaped = json.dumps(line)[1:-1]
    return {line, json.dumps(line, ensure_ascii=False)[1:-1], escaped, escaped.translate(HTML_SAFE)}


def recall_evidence(event, sources: list[str], text: str) -> str | None:
    """How memory content reached an incoming context: by its marker, a copy pattern, or not at all."""
    if event.marker and event.marker in text and any(event.marker in source for source in sources):
        return "marker"
    if any((copy := event.copy_match(source)) and copy in text for source in sources):
        return "copy"
    return None


class BoundedStrategy:
    def __init__(self, name: str):
        self.name = name

    def eligible(self, event, total_fires: int, session_fires: int, context: dict) -> bool:
        return total_fires < event.max_fires and (self.name != "sequence" or session_fires == 0)


class InterventionEngine:
    def __init__(self, events, registry, *, enabled=True, seed=0):
        self.events, self.registry = events, registry
        self.enabled = enabled
        self.rng = random.Random(seed)
        self.fires = Counter()
        self.session_fires = Counter()

    def apply(self, runtime, phase: str, command: str = "", output: dict | None = None):
        if not self.enabled:
            return output
        planted = []
        for event in self.events:
            if (
                event.phase != phase
                or runtime.session.id not in event.sessions
                or not runtime.session.exposure
                or (event.command_contains and event.command_contains not in command)
                or (event.output_contains and event.output_contains not in (output or {}).get("output", ""))
                or (event.min_step and runtime.step < event.min_step)
                or (event.max_step and runtime.step > event.max_step)
            ):
                continue
            context = {
                "session_id": runtime.session.id,
                "phase": phase,
                "command": command,
                "output": output,
                "rng": self.rng,
            }
            total, session_count = self.fires[event.id], self.session_fires[(event.id, runtime.session.id)]
            # The engine enforces the total bound even for custom strategies.
            if total >= event.max_fires or not self.registry.strategies[event.strategy].eligible(
                event, total, session_count, context
            ):
                continue
            try:
                before = output["output"] if event.method == "output" else runtime.env.read(event.path)
                after = edit(before, event)
            except (ValueError, RuntimeError) as error:
                # Only configured content without old_text is a configuration error; text the agent changed (any
                # later phase, or memory carried from an earlier session) must not invalidate a paid session.
                carried = event.method == "memory" and runtime.memory_start.get(event.path) != (
                    runtime.memory_config.initial().get(event.path)
                )
                if phase == "session_start" and not carried:
                    raise
                runtime.emit("intervention_skipped", intervention_id=event.id, phase=phase, reason=str(error))
                continue
            if event.method == "output":
                output["output"] = after
            else:
                runtime.env.write(event.path, after)
            self.fires[event.id] += 1
            self.session_fires[(event.id, runtime.session.id)] += 1
            runtime.emit(
                "intervention",
                intervention_id=event.id,
                method=event.method,
                strategy=event.strategy,
                phase=phase,
                path=event.path,
                before_sha256=fingerprint(before or ""),
                after_sha256=fingerprint(after),
                occurrence=self.fires[event.id],
                origin="intervention",
            )
            runtime.journal.append(
                {
                    "event": event.model_dump(mode="json"),
                    "step": runtime.step,
                    "action": runtime.actions,
                    "before": before,
                    "after": after,
                }
            )
            if event.method == "memory":
                try:
                    runtime.capture_memory("intervention")
                except MemoryViolation as error:
                    # The planted content broke the memory rules: a configuration failure, not an agent outcome.
                    raise RuntimeError(f"memory intervention {event.id} broke the memory rules: {error}") from error
            if event.placement == "commit":
                planted.append(runtime.journal[-1])
        if planted:
            # A planted file must not show up as untracked or modified; patches still diff against the task base.
            git = commit_planted(runtime.env, sorted({entry["event"]["path"] for entry in planted}))
            runtime.emit("intervention_commit", origin="intervention", **git)
            for entry in planted:
                path = entry["event"]["path"]
                entry["git"] = {
                    "placement": git["paths"][path],
                    "base_commit": git["base_commit"],
                    "planted_head": git["planted_head"],
                }
        return output


def edit(before: str | None, event) -> str:
    text = event.text
    original = before or ""
    if event.operation == "append":
        return original + text
    if event.operation == "prepend":
        return text + original
    if event.old_text is None:
        return text
    if original.count(event.old_text) != 1:
        raise ValueError(f"{event.id}: old_text must match exactly once")
    return original.replace(event.old_text, text, 1)


def merge3(base: str, ours: str, theirs: str) -> str | None:
    """Line-level three-way merge like `git merge-file -p ours base theirs`; None when the changes conflict.

    Changes on both sides with no unchanged line between them conflict, as in git.
    """
    o, a, b = (LINES.findall(text) for text in (base, ours, theirs))
    blocks_a = SequenceMatcher(None, o, a, autojunk=False).get_matching_blocks()
    blocks_b = SequenceMatcher(None, o, b, autojunk=False).get_matching_blocks()
    # Base lines unchanged on both sides anchor the merge; everything between anchors is one hunk.
    anchors, i, j = [], 0, 0
    while i < len(blocks_a) and j < len(blocks_b):
        (oa, sa, na), (ob, sb, nb) = blocks_a[i], blocks_b[j]
        low, high = max(oa, ob), min(oa + na, ob + nb)
        if low < high:
            anchors.append((low, high, sa + low - oa, sb + low - ob))
        if oa + na < ob + nb:
            i += 1
        else:
            j += 1
    anchors.append((len(o), len(o), len(a), len(b)))
    merged, po, pa, pb = [], 0, 0, 0
    for low, high, sa, sb in anchors:
        hunk_o, hunk_a, hunk_b = o[po:low], a[pa:sa], b[pb:sb]
        if hunk_a == hunk_o:
            merged += hunk_b
        elif hunk_b in (hunk_o, hunk_a):
            merged += hunk_a
        else:
            return None
        merged += o[low:high]
        po, pa, pb = high, sa + high - low, sb + high - low
    return "".join(merged)


def separate_patch(env, journal: list[dict], base_commit: str) -> bytes:
    """Undo intervention edits by a three-way merge; an agent edit to the planted lines makes the patch ambiguous."""
    for entry in reversed(journal):
        event = entry["event"]
        if event["method"] != "file":
            continue
        path, before, after = event["path"], entry["before"], entry["after"]
        try:
            current = env.read(path)
            if current == after:
                env.write(path, before)
                continue
            merged = merge3(after, current or "", before or "")
            # A merge that keeps planted text means the agent moved or copied it: not separable. Planting again
            # must give back exactly what the agent left (a deleted file aside); otherwise the merge mistook an
            # agent edit for the plant, as when planted lines duplicate existing ones.
            if (
                merged is None
                or merged.count(event["text"]) > (before or "").count(event["text"])
                or (current is not None and merge3(before or "", merged, after) != current)
            ):
                raise ValueError(f"ambiguous agent/intervention overlap at {path}")
            env.write(path, None if merged == "" and None in (before, current) else merged)
        except RuntimeError as error:
            raise ValueError(f"ambiguous agent/intervention overlap at {path}: {error}") from error
    return env.collect_patch(base_commit)


class Runtime:
    def __init__(self, env, engine, log, session, memory_config, initial_memory, checks):
        self.env, self.engine, self.log, self.session = env, engine, log, session
        self.memory_config, self.memory = memory_config, dict(initial_memory)
        self.memory_start = dict(initial_memory)
        self.checks = checks
        self.action_id = None
        self.actions = 0
        self.journal: list[dict] = []
        self.observations: list[str] = []
        self.last_output = ""
        self.initial_memory_context = ""
        self.memory_observations: list[str] = []
        # Memory-reading observations with every memory-derived line removed, for fresh exposure.
        self.memory_stripped: list[tuple[str, str]] = []
        self.memory_lines: set[str] = set()
        self.remember_lines(initial_memory.values())
        self.remember_lines(e.text for e in engine.events if e.method == "memory")
        self.pending_commands: list[str] = []
        self.sent: list[str] = []

    @property
    def step(self) -> int:
        return self.log.step

    def emit(self, kind: str, **data) -> str:
        return self.log.emit(kind, session_id=self.session.id, action_id=self.action_id, action=self.actions, **data)

    def remember_lines(self, texts) -> None:
        self.memory_lines.update(line for text in texts for line in text.splitlines() if line.strip())

    def capture_memory(self, origin: str):
        files = self.env.snapshot_memory(self.memory_config.max_files, self.memory_config.max_bytes)
        for path in sorted(set(files) | set(self.memory)):
            if files.get(path) != self.memory.get(path):
                self.emit(
                    "memory_change",
                    origin=origin,
                    owner=owner(path),
                    path=path,
                    before=self.memory.get(path),
                    after=files.get(path),
                )
        self.memory = files
        self.remember_lines(files.values())
        return dict(files)

    def execute(self, command: str) -> dict[str, Any]:
        self.actions += 1
        self.action_id = f"{self.session.id}-a{self.actions:05d}"
        self.engine.apply(self, "before_action", command)
        self.pending_commands.append(command)
        self.emit("action", command=command)
        raw = self.env.execute(command)
        self.emit("raw_output", **raw)
        # Capture the agent's own writes before an after_observation memory edit, or they read as intervention writes.
        self.capture_memory("agent")
        output = self.engine.apply(self, "after_observation", command, dict(raw))
        self.last_output = raw["output"]
        # Control-flow submission is determined from actual execution, never injected text.
        lines = raw["output"].lstrip().splitlines()
        output["submitted"] = raw["returncode"] == 0 and bool(lines) and lines[0].strip() == SUBMIT
        return output

    def observation(self, messages: list[dict]) -> None:
        text = message_text(messages)
        self.observations.append(text)
        # Each action starts in /app, so any read of memory names the /memories root in the command itself.
        # One observation can carry several actions when a model call issues several tool calls.
        if any(MEMORY_ROOT.search(command) for command in self.pending_commands):
            self.memory_observations.append(text)
            stripped = text
            for form in sorted({f for line in self.memory_lines for f in renderings(line)}, key=len, reverse=True):
                stripped = stripped.replace(form, "")
            self.memory_stripped.append((text, stripped))
        self.pending_commands = []
        self.emit("observation", messages=messages)

    def context_sent(self, messages: list[dict]) -> None:
        # Only incoming context can be an exposure; assistant-generated marker echoes are not exposures.
        incoming = [m for m in messages if m.get("role") != "assistant"]
        text = message_text(incoming)
        # Fresh exposure needs a fire in this session; a carried memory copy is recall, not exposure,
        # except for memory interventions, whose own channel is the reminder and memory reads. Only
        # memory-derived content leaves: other output of a command that also read /memories stays.
        fresh = text.replace(self.initial_memory_context, "") if self.initial_memory_context else text
        for observed, stripped in self.memory_stripped:
            fresh = fresh.replace(observed, stripped)
        matched = [
            e.id
            for e in self.engine.events
            if self.session.exposure
            and e.marker
            and self.engine.session_fires[(e.id, self.session.id)]
            and e.marker in (text if e.method == "memory" else fresh)
        ]
        recall = {}
        for event in self.engine.events:
            routes = {
                route: evidence
                for route, sources in (("startup", [self.initial_memory_context]), ("read", self.memory_observations))
                if (evidence := recall_evidence(event, sources, text))
            }
            if routes:
                recall[event.id] = routes
        self.log.step += 1
        # Log only messages added since the previous call; a rewritten prefix logs the full list from offset 0.
        parts = [serialize(m) for m in messages]
        offset = len(self.sent) if parts[: len(self.sent)] == self.sent else 0
        self.sent = parts
        self.emit(
            "context_sent",
            offset=offset,
            message_count=len(messages),
            messages_sha256=fingerprint("[" + ", ".join(parts) + "]"),
            messages=messages[offset:],
            matched_interventions=matched,
            memory_recall=recall,
            memory_context_interventions=[i for i, routes in recall.items() if "marker" in routes.values()],
        )
