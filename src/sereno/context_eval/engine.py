"""Bounded intervention hooks, exact observation evidence, and patch separation."""

import json
import random
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sereno.context_eval.config import fingerprint
from sereno.context_eval.memory import owner

MEMORY_ROOT = re.compile(r"/memories(?![\w.-])")


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


class EventLog:
    def __init__(self, path: Path, run_id: str):
        self.path, self.run_id = path, run_id
        self.sequence = 0

    def emit(self, kind: str, **data) -> str:
        self.sequence += 1
        event_id = f"e{self.sequence:06d}"
        record = {
            "schema_version": "1.0",
            "id": event_id,
            "run_id": self.run_id,
            "ts": datetime.now(UTC).isoformat(),
            "kind": kind,
            **data,
        }
        with self.path.open("a") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return event_id


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
        for event in self.events:
            if (
                event.phase != phase
                or runtime.session.id not in event.sessions
                or not runtime.session.exposure
                or (event.command_contains and event.command_contains not in command)
                or (event.output_contains and event.output_contains not in (output or {}).get("output", ""))
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
            before = output["output"] if event.method == "output" else runtime.env.read(event.path)
            after = edit(before, event)
            if event.method == "output":
                output["output"] = after
            else:
                runtime.env.write(event.path, after)
            self.fires[event.id] += 1
            self.session_fires[(event.id, runtime.session.id)] += 1
            runtime.log.emit(
                "intervention",
                session_id=runtime.session.id,
                action_id=runtime.action_id,
                intervention_id=event.id,
                method=event.method,
                strategy=event.strategy,
                phase=phase,
                path=event.path,
                before_sha256=fingerprint(before or ""),
                after_sha256=fingerprint(after),
                occurrence=self.fires[event.id],
                origin="harness",
            )
            runtime.journal.append({"event": event.model_dump(mode="json"), "before": before, "after": after})
            if event.method == "memory":
                runtime.capture_memory("harness")
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


def separate_patch(env, journal: list[dict], base_commit: str) -> str:
    """Undo only identifiable harness edits; ambiguous overlapping agent edits are an export error."""
    for entry in reversed(journal):
        event = entry["event"]
        if event["method"] != "file":
            continue
        path, before, after = event["path"], entry["before"], entry["after"]
        current = env.read(path)
        if current == after:
            env.write(path, before)
        elif (
            before is not None
            and current is not None
            and (event["operation"] != "replace" or event["old_text"] is not None)
            and current.count(event["text"]) == 1
        ):
            inserted = event["text"]
            position = (
                len(before)
                if event["operation"] == "append"
                else (0 if event["operation"] == "prepend" else before.index(event["old_text"]))
            )
            removed_length = len(event["old_text"] or "")
            left = before[max(0, position - 200) : position]
            right = before[position + removed_length : position + removed_length + 200]
            location = current.index(inserted)
            if not current[:location].endswith(left) or not current[location + len(inserted) :].startswith(right):
                raise ValueError(f"ambiguous agent/harness overlap at {path}")
            env.write(path, current.replace(inserted, event["old_text"] or "", 1))
        else:
            raise ValueError(f"ambiguous agent/harness overlap at {path}")
    return env.collect_patch(base_commit)


class Runtime:
    def __init__(self, env, engine, log, session, memory_config, initial_memory, checks):
        self.env, self.engine, self.log, self.session = env, engine, log, session
        self.memory_config, self.memory = memory_config, dict(initial_memory)
        self.checks = checks
        self.action_id = None
        self.actions = 0
        self.journal: list[dict] = []
        self.observations: list[str] = []
        self.last_output = ""
        self.initial_memory_context = ""
        self.memory_observations: list[str] = []
        self.last_command = ""

    def capture_memory(self, origin: str):
        files = self.env.snapshot_memory(self.memory_config.max_files, self.memory_config.max_bytes)
        for path in sorted(set(files) | set(self.memory)):
            if files.get(path) != self.memory.get(path):
                self.log.emit(
                    "memory_change",
                    session_id=self.session.id,
                    action_id=self.action_id,
                    origin=origin,
                    owner=owner(path),
                    path=path,
                    before=self.memory.get(path),
                    after=files.get(path),
                )
        self.memory = files
        return dict(files)

    def execute(self, command: str) -> dict[str, Any]:
        self.actions += 1
        self.action_id = f"{self.session.id}-a{self.actions:05d}"
        self.engine.apply(self, "before_action", command)
        self.last_command = command
        self.log.emit("action", session_id=self.session.id, action_id=self.action_id, command=command)
        raw = self.env.execute(command)
        self.log.emit("raw_output", session_id=self.session.id, action_id=self.action_id, **raw)
        # Capture the agent's own writes before an after_observation memory edit, or they read as harness writes.
        self.capture_memory("agent")
        output = self.engine.apply(self, "after_observation", command, dict(raw))
        self.last_output = raw["output"]
        # Control-flow submission is determined from actual execution, never injected text.
        output["submitted"] = raw["returncode"] == 0 and raw["output"].lstrip().splitlines()[:1] == [
            "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
        ]
        return output

    def observation(self, messages: list[dict]) -> None:
        text = message_text(messages)
        self.observations.append(text)
        # Each action starts in /app, so any read of memory names the /memories root in the command itself.
        if MEMORY_ROOT.search(self.last_command):
            self.memory_observations.append(text)
        self.log.emit("observation", session_id=self.session.id, action_id=self.action_id, messages=messages)

    def context_sent(self, messages: list[dict]) -> None:
        # Only incoming context can be an exposure; assistant-generated marker echoes are not exposures.
        incoming = [m for m in messages if m.get("role") != "assistant"]
        text = message_text(incoming)
        matched = [e.id for e in self.engine.events if e.marker and e.marker in text] if self.session.exposure else []
        self.log.emit(
            "context_sent",
            session_id=self.session.id,
            action_id=self.action_id,
            messages=messages,
            matched_interventions=matched,
            memory_context_interventions=[
                e.id
                for e in self.engine.events
                if e.marker
                and e.marker in text
                and (
                    e.marker in self.initial_memory_context
                    or any(e.marker in observation for observation in self.memory_observations)
                )
            ],
        )
