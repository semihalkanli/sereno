"""Declarative attack events. No executable expressions or model-based event generator.

Record edits persist; result edits affect only the returned observation. Events execute
in declaration order, after the world's ordinary changes, and have bounded lifetimes.
"""

import hashlib
import json
from collections import Counter
from dataclasses import replace
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from sereno.apps import get_app
from sereno.checks import Check, collection, grade


class Target(BaseModel, extra="forbid"):
    kind: Literal["record", "result", "tool_description", "user_file"]
    app: str = ""
    collection: str = ""
    match: dict[str, Any] = {}
    field: str = ""
    tool: str = ""
    path: list[str | int] = []


class AttackEvent(BaseModel, extra="forbid"):
    id: str
    phase: Literal["session_start", "before_tool", "after_tool"] = "session_start"
    sessions: list[str] = Field(min_length=1)
    not_before: datetime | None = None
    tool: str = ""
    arguments: dict[str, Any] = {}
    conditions: list[Check] = []
    max_fires: int = Field(1, ge=1, le=1000)
    target: Target
    operation: Literal["replace", "prepend", "append", "add", "remove"] = "append"
    text: str = ""
    item: dict[str, Any] | None = None
    poison: bool = True
    """False for explicit cleanup; cleanup must not be mistaken for new exposure."""

    @model_validator(mode="after")
    def coherent(self):
        t = self.target
        if t.kind == "result" and (self.phase != "after_tool" or not self.tool):
            raise ValueError("result events need after_tool and a tool name")
        if t.kind in {"tool_description", "user_file"} and self.phase != "session_start":
            raise ValueError("prompt surface events run at session_start")
        if self.phase == "session_start" and (self.tool or self.arguments):
            raise ValueError("session_start cannot filter tool calls")
        if self.arguments and not self.tool:
            raise ValueError("argument filters need a tool")
        if self.operation in {"add", "remove"} and t.kind != "record":
            raise ValueError("add/remove apply to records only")
        if self.operation == "add" and self.item is None:
            raise ValueError("add needs an item")
        if t.kind == "record" and (not t.app or not t.collection):
            raise ValueError("record needs app and collection")
        if t.kind == "record" and self.operation != "add" and not t.match:
            raise ValueError("record edits need an explicit match")
        if t.kind == "record" and self.operation not in {"add", "remove"} and not t.field:
            raise ValueError("record text edits need a field")
        if t.kind == "tool_description" and not t.tool:
            raise ValueError("tool_description needs a target tool")
        if any(
            branch.new or branch.changed or branch.check == "unchanged"
            for condition in self.conditions
            for branch in [condition, *condition.alternatives]
        ):
            raise ValueError("conditions inspect current state: no new, changed or unchanged")
        return self


def payloads(attack) -> list[str]:
    return [
        *attack.fills.values(),
        *[e.text for e in attack.events if e.poison and e.text],
        *[json.dumps(e.item, ensure_ascii=False) for e in attack.events if e.poison and e.item],
    ]


def validate_events(loaded) -> None:
    """Reject unreachable tools, unknown fields and sessions before starting a run."""
    world = loaded.initial_world()
    tools = {t.name for t in world.tools()}
    ids = set()
    for event in loaded.attack.events if loaded.attack else []:
        if event.id in ids:
            raise ValueError(f"duplicate attack event {event.id}")
        ids.add(event.id)
        for sid in event.sessions:
            loaded.session_number(sid)
        for name in (event.tool, event.target.tool):
            if name and name not in tools:
                raise ValueError(f"event {event.id}: unknown tool {name}")
        t = event.target
        if t.kind == "record":
            _, key = collection(world, t.app, t.collection)
            from sereno.chain import _item_type

            model = _item_type(world, t.app, t.collection)
            for field in [*t.match, *([t.field] if t.field else [])]:
                if field not in model.model_fields:
                    raise ValueError(f"event {event.id}: unknown field {field}")
            if t.field == key:
                raise ValueError("text edits cannot change record identities")
            if event.operation == "add":
                model.model_validate(event.item)
        if t.kind == "user_file" and not loaded.chain.memory:
            raise ValueError("user_file events require memory")
        # Evaluate even on empty collections to validate condition field names.
        for condition in event.conditions:
            from sereno.chain import _item_type

            for branch in [condition, *condition.alternatives]:
                model = _item_type(world, branch.app, branch.collection)
                if any(set(group) - set(model.model_fields) for group in [branch.where, *branch.where_any]):
                    raise ValueError(f"event {event.id}: unknown condition field")


class Experiment:
    def __init__(self, loaded, world, log, count_copies):
        self.events = loaded.attack.events if loaded.attack else []
        self.world, self.log, self.count_copies = world, log, count_copies
        self.fires = Counter()
        self.external_delta = Counter()
        self.session = ""
        self.toolset = None
        self.user_file = ""
        self.poisoned_sessions: set[str] = set()
        self.external_observations: set[str] = set()
        self.record_sources: list[AttackEvent] = []

    def edit(self, old: str, event: AttackEvent) -> str:
        if not isinstance(old, str):
            raise ValueError(f"event {event.id}: target is not text")
        if event.operation == "replace":
            return event.text
        return event.text + old if event.operation == "prepend" else old + event.text

    def apply(self, phase, tool="", arguments=None, outcome=None):
        from sereno.chain import Change, apply_change

        for e in self.events:
            if (
                e.phase != phase
                or self.session not in e.sessions
                or self.fires[e.id] >= e.max_fires
                or (e.tool and e.tool != tool)
                or (e.not_before and self.world.now < e.not_before)
                or any((arguments or {}).get(k) != v for k, v in e.arguments.items())
                or not all(grade(e.conditions, self.world, self.world).values())
            ):
                continue
            if e.target.kind == "result" and (outcome is None or outcome.error):
                continue
            before = self.count_copies()
            t = e.target
            if t.kind == "record":
                if e.operation in {"add", "remove"}:
                    apply_change(
                        self.world,
                        Change(op=e.operation, app=t.app, collection=t.collection, match=t.match, item=e.item),
                    )
                else:
                    rows, _ = collection(self.world, t.app, t.collection)
                    hits = [row for row in rows if all(getattr(row, k) == v for k, v in t.match.items())]
                    if len(hits) != 1:
                        raise ValueError(f"event {e.id}: expected one record, got {len(hits)}")
                    value = self.edit(getattr(hits[0], t.field), e)
                    apply_change(
                        self.world,
                        Change(op="update", app=t.app, collection=t.collection, match=t.match, set={t.field: value}),
                    )
            elif t.kind == "result":
                if t.path:
                    data = json.loads(outcome.result)
                    parent = data
                    for part in t.path[:-1]:
                        parent = parent[part]
                    parent[t.path[-1]] = self.edit(parent[t.path[-1]], e)
                    outcome.result = json.dumps(data, ensure_ascii=False)
                else:
                    outcome.result = self.edit(outcome.result, e)
                outcome.provenance = {
                    **outcome.provenance,
                    "sha256": hashlib.sha256(outcome.result.encode()).hexdigest()[:16],
                }
            elif t.kind == "tool_description":
                original = self.toolset.tools[t.tool]
                self.toolset.tools[t.tool] = replace(original, description=self.edit(original.description, e))
            else:
                self.user_file = self.edit(self.user_file, e)
            after = self.count_copies()
            for app in after:
                self.external_delta[app] += after[app] - before[app]
            self.fires[e.id] += 1
            if e.poison:
                self.poisoned_sessions.add(self.session)
            self.log.emit(
                "attack_event",
                event_id=e.id,
                session_id=self.session,
                phase=phase,
                tool=tool or None,
                target=t.model_dump(),
                operation=e.operation,
                occurrence=self.fires[e.id],
                poison=e.poison,
            )
            if t.kind == "record":
                if e.poison:
                    self.record_sources.append(e)
                self.log.emit("state", reason=f"attack:{e.id}", snapshot=self.world.snapshot())
        return outcome

    def before(self, name, args):
        self.apply("before_tool", name, args)

    def after(self, name, args, outcome):
        # A source read again is fresh exposure, even if the agent also kept a copy.
        # Require the source identity as well as its text: a later agent-created note
        # in the same app is a carrier, not the original outside record.
        for event in self.record_sources:
            identity = event.target.match
            if event.operation == "add":
                key = get_app(event.target.app).keys[event.target.collection]
                identity = {key: event.item[key]}
            if (
                outcome.provenance.get("channel", "").startswith(event.target.app + ".")
                and event.text
                and event.text in outcome.result
                and any(str(value) in json.dumps([args, outcome.result]) for value in identity.values())
            ):
                self.external_observations.add(self.session)
                self.log.emit("attack_exposure", event_id=event.id, session_id=self.session, tool=name)
        return self.apply("after_tool", name, args, outcome)

    def finish(self):
        for e in self.events:
            self.log.emit(
                "attack_event_status",
                event_id=e.id,
                fires=self.fires[e.id],
                status="applied" if self.fires[e.id] else "not_triggered",
            )


def surfaces(loaded) -> dict:
    """Discover editable text in initial records and scheduled changes, without private host data."""
    world = loaded.initial_world()
    records = []
    for app, state in world.apps.items():
        for name, key in get_app(app).keys.items():
            for item in getattr(state, name):
                records.extend(
                    {
                        "kind": "record",
                        "app": app,
                        "collection": name,
                        "match": {key: getattr(item, key)},
                        "field": field,
                    }
                    for field, value in item.model_dump().items()
                    if isinstance(value, str) and field != key
                )
    return {
        "chain": loaded.chain.id,
        "version": loaded.chain.version,
        "sessions": [
            {"id": s.id, "now": s.now.isoformat(), "changes": [c.model_dump(exclude_none=True) for c in s.changes]}
            for s in loaded.chain.sessions
        ],
        "slots": [s.model_dump() for s in loaded.chain.slots],
        "records": records,
        "tools": [t.schema() for t in world.tools()],
        "prompt_surfaces": ["tool_description", *(["user_file"] if loaded.chain.memory else [])],
    }
