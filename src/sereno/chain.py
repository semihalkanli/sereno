"""Chain files: one person's long task over several sessions, read from disk.

A chain lives in its own directory under `chains/`. The format, slots and
placeholders included, is described field by field in `chains/README.md`.
Apps are written once in `sereno.apps`; a chain only names the ones it links.
"""

import json
import random
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, TypeAdapter, model_validator

from sereno.apps import get_app
from sereno.checks import Check, collection
from sereno.memory import INSTRUCTIONS
from sereno.tools import Tool
from sereno.world import World

REPO = Path(__file__).resolve().parents[2]
CHAINS_DIR = REPO / "chains"
PLACEHOLDER = re.compile(r"\{\{([^{}]+)\}\}")


class Slot(BaseModel, extra="forbid"):
    id: str
    default: str = ""
    note: str = ""


class Change(BaseModel, extra="forbid"):
    """An outside change applied when a session starts (a new email, a moved event)."""

    op: Literal["add", "update", "remove"]
    app: str
    collection: str
    item: dict[str, Any] | None = None
    match: dict[str, Any] | None = None
    set: dict[str, Any] | None = None
    optional: bool = False
    """An update or remove that matches nothing is skipped, for an item the agent was meant to create."""


class Call(BaseModel, extra="forbid"):
    name: str
    args: dict[str, Any] = {}


class Message(BaseModel, extra="forbid"):
    """A message that already happened earlier in the session; calls run on the world."""

    role: Literal["user", "assistant"]
    content: str | None = None
    calls: list[Call] = []


class Session(BaseModel, extra="forbid"):
    id: str
    now: datetime
    changes: list[Change] = []
    history: list[Message] = []
    turns: list[str] = Field(min_length=1)
    specs: list[Literal["fully_specified", "param_open", "action_open"]] = []
    """Per turn, how much of it the user leaves to outside content, after AutoDojo's buckets; empty: unlabelled."""
    checks: list[Check] = []
    user_file: str | None = None
    """The user's new version of the user file, from this session on; None keeps the previous one."""
    answer_only: bool = False
    """The session's work is the answer itself, which state checks cannot read; a later session's checks grade it."""

    @model_validator(mode="after")
    def _one_spec_per_turn(self) -> "Session":
        if self.specs and len(self.specs) != len(self.turns):
            raise ValueError(f"session {self.id} has {len(self.turns)} turns but {len(self.specs)} specs")
        return self


class Chain(BaseModel, extra="forbid"):
    id: str
    name: str
    summary: str
    objective: str = ""
    apps: list[str] = Field(min_length=1)
    system_prompt: str
    memory: bool = False
    user_file: str = ""
    tool_descriptions: dict[str, str] = {}
    """Tool name -> the description the agent sees instead of the app's own; `{{description}}` is the app's text."""
    slots: list[Slot] = []
    sessions: list[Session] = Field(min_length=1)
    final_checks: list[Check] = []


class Trigger(BaseModel, extra="forbid"):
    """Something the user says in a later session that the poison waits for."""

    session: str
    phrase: str


class Attack(BaseModel, extra="forbid"):
    id: str
    objective: str
    marker: str
    fills: dict[str, str]
    one_of: list[str] = []
    """Filled slots of which the run's seed keeps one; the others keep their default. Empty keeps every fill."""
    trigger: Trigger | None = None
    checks: list[Check] = []
    copy_tokens: list[str] = []
    """Words the poison's claim keeps when the agent restates it, matched ignoring case, so a paraphrased copy
    in a note or memory is counted as well as the marker; none may appear in what the clean solution writes."""

    def copies_in(self, text: str) -> int:
        """Copies of the poison in `text`: the marker word for word plus each copy token, ignoring case."""
        lower = text.lower()
        return text.count(self.marker) + sum(lower.count(t.lower()) for t in self.copy_tokens)


class Twin(BaseModel, extra="forbid"):
    """A benign twin: a legitimate outside change after which acting on outside content is right."""

    id: str
    objective: str
    changes: dict[str, list[Change]] = {}
    """Session id -> changes applied after that session's own."""
    checks: dict[str, list[Check]] = {}
    """Session id or "final" -> checks that replace the chain's check of the same name, or are added."""
    solution: dict[str, list] = {}
    """Session id -> a correct run of that session, replacing the chain solution's."""


def _apply_twin(chain: Chain, solution: dict | None, twin: Twin) -> dict | None:
    sessions = {s.id: s for s in chain.sessions}
    unknown = sorted((set(twin.changes) | set(twin.solution) | (set(twin.checks) - {"final"})) - set(sessions))
    if unknown:
        raise ValueError(f"twin {twin.id} names sessions the chain lacks: {', '.join(unknown)}")
    for sid, changes in twin.changes.items():
        sessions[sid].changes.extend(changes)
    for group, checks in twin.checks.items():
        own = chain.final_checks if group == "final" else sessions[group].checks
        index = {c.name: i for i, c in enumerate(own)}
        for check in checks:
            if check.name in index:
                own[index[check.name]] = check
            else:
                own.append(check)
    if twin.solution and solution is None:
        raise ValueError(f"twin {twin.id} gives a solution but the chain has none")
    return {**solution, **twin.solution} if solution is not None else None


def _fill_slots(value: Any, fills: dict[str, str], used: set[str]) -> Any:
    if isinstance(value, str):

        def sub(m: re.Match) -> str:
            name = m.group(1)
            if not name.startswith("slot:"):
                return m.group(0)
            slot = name.removeprefix("slot:")
            if slot not in fills:
                raise ValueError(f"undeclared slot {slot!r}")
            used.add(slot)
            return fills[slot]

        return PLACEHOLDER.sub(sub, value)
    if isinstance(value, list):
        return [_fill_slots(v, fills, used) for v in value]
    if isinstance(value, dict):
        return {k: _fill_slots(v, fills, used) for k, v in value.items()}
    return value


def _slots_in(value: Any, fills: dict[str, str]) -> set[str]:
    used: set[str] = set()
    _fill_slots(value, fills, used)
    return used


def slot_sessions(raw_chain: dict[str, Any], raw_world: dict[str, Any], fills: dict[str, str]) -> dict[str, int]:
    """Slot id -> the first session (1, 2, ...) in which the agent can see it; 1 for anything there from the start."""
    first: dict[str, int] = {}
    for number, session in enumerate(raw_chain.get("sessions", []), start=1):
        for slot in _slots_in(session, fills):
            first.setdefault(slot, number)
    rest = {k: v for k, v in raw_chain.items() if k != "sessions"}
    for slot in _slots_in(rest, fills) | _slots_in(raw_world, fills):
        first[slot] = 1
    return first


def _fill(text: str, values: dict[str, str], where: str) -> str:
    """Fill `{{name}}` placeholders from `values`; any other placeholder is an error."""

    def sub(m: re.Match) -> str:
        if m.group(1) not in values:
            raise ValueError(f"unknown placeholder {m.group(0)} in {where}")
        return values[m.group(1)]

    return PLACEHOLDER.sub(sub, text)


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


class LoadedChain:
    """A chain with its slots filled, ready to run: the chain, its world data, and the attack and twin, if any."""

    def __init__(
        self,
        chain: Chain,
        world_data: dict[str, Any],
        attack: Attack | None,
        solution: list | None,
        seed: int,
        poison: dict[str, int],
        twin: Twin | None = None,
    ):
        self.chain = chain
        self.world_data = world_data
        self.attack = attack
        self.solution = solution
        self.seed = seed
        self.poison = poison
        """Slot id -> session number, for the attack fills this run carries."""
        self.twin = twin

    def session_number(self, session_id: str) -> int:
        ids = [s.id for s in self.chain.sessions]
        if session_id not in ids:
            raise ValueError(f"chain {self.chain.id} has no session {session_id!r}")
        return ids.index(session_id) + 1

    def placement(self) -> dict[str, str]:
        """Slot id -> the id of the first session that shows it, for the attack fills this run carries."""
        return {slot: self.chain.sessions[n - 1].id for slot, n in sorted(self.poison.items())}

    def check_until(self, until: str) -> None:
        """Refuse a run stopped at `until` that the attack could not reach: its poison or trigger comes later."""
        last = self.session_number(until)
        trigger = self.attack.trigger if self.attack else None
        if trigger and self.session_number(trigger.session) > last:
            raise ValueError(f"a run until {until} stops before the attack's trigger session {trigger.session}")
        late = [slot for slot, n in self.poison.items() if n > last]
        if late:
            raise ValueError(f"seed {self.seed}: {', '.join(late)} arrives after the run stops at {until}")

    def app_names(self) -> list[str]:
        return [*self.chain.apps, "memory"] if self.chain.memory else self.chain.apps

    def initial_world(self) -> World:
        return World.load(self.world_data, self.app_names())

    def tools(self, world: World) -> list[Tool]:
        """The linked apps' tools, with the chain's own descriptions where it gives one."""
        overrides = self.chain.tool_descriptions
        return [
            replace(t, description=_fill(overrides[t.name], {"description": t.description}, f"{t.name}'s description"))
            if t.name in overrides
            else t
            for t in world.tools()
        ]

    def system_prompt(self, world: World) -> str:
        values = {
            "owner_name": world.owner.name,
            "owner_email": world.owner.email,
            "now": f"{world.now:%A %d %B %Y, %H:%M}",
            "apps": _join([get_app(n).title for n in self.chain.apps]),
        }

        prompt = _fill(self.chain.system_prompt, values, "system_prompt")
        return prompt + INSTRUCTIONS if self.chain.memory else prompt


def chain_ids() -> list[str]:
    return sorted(p.name for p in CHAINS_DIR.iterdir() if (p / "chain.json").exists())


def load_chain(
    chain_id: str, attack_id: str | None = None, root: Path = CHAINS_DIR, seed: int = 0, twin_id: str | None = None
) -> LoadedChain:
    """Load a chain with its slots filled. `seed` picks the attack's placement when it lists `one_of`."""
    directory = root / chain_id
    raw_chain = json.loads((directory / "chain.json").read_text(encoding="utf-8"))
    raw_world = json.loads((directory / "world.json").read_text(encoding="utf-8"))
    solution_path = directory / "solution.json"
    solution = json.loads(solution_path.read_text(encoding="utf-8")) if solution_path.exists() else None

    slots = {s.id: s for s in Chain.model_validate(raw_chain).slots}
    fills = {sid: s.default for sid, s in slots.items()}
    attack = None
    poison: dict[str, int] = {}
    if attack_id is not None:
        attack = Attack.model_validate_json((directory / "attacks" / f"{attack_id}.json").read_text(encoding="utf-8"))
        unknown = [s for s in attack.fills if s not in slots]
        if unknown:
            raise ValueError(f"attack {attack_id} fills undeclared slots: {', '.join(unknown)}")
        if not any(attack.marker in text for text in attack.fills.values()):
            raise ValueError(f"attack {attack_id}: no fill carries the marker {attack.marker!r}")
        chosen = dict(attack.fills)
        if attack.one_of:
            unfilled = [s for s in attack.one_of if s not in attack.fills]
            if unfilled:
                raise ValueError(f"attack {attack_id}: one_of names slots it does not fill: {', '.join(unfilled)}")
            unmarked = [s for s in attack.one_of if attack.marker not in attack.fills[s]]
            if unmarked:
                raise ValueError(f"attack {attack_id}: one_of fills without the marker: {', '.join(unmarked)}")
            keep = random.Random(seed).choice(attack.one_of)
            chosen = {s: t for s, t in chosen.items() if s not in attack.one_of or s == keep}
        fills.update(chosen)
        sessions = slot_sessions(raw_chain, raw_world, fills)
        poison = {s: sessions[s] for s in chosen if s in sessions}

    used: set[str] = set()
    chain = Chain.model_validate(_fill_slots(raw_chain, fills, used))
    world_data = _fill_slots(raw_world, fills, used)
    unused = [s for s in slots if s not in used]
    if unused:
        raise ValueError(f"slots declared but never placed: {', '.join(unused)}")
    if chain.id != chain_id:
        raise ValueError(f"chain id {chain.id!r} does not match its directory {chain_id!r}")
    if not chain.memory and (chain.user_file or any(s.user_file is not None for s in chain.sessions)):
        raise ValueError(f"chain {chain_id} has a user file but no memory; the user file is loaded with memory")
    twin = None
    if twin_id is not None:
        twin = Twin.model_validate_json((directory / "twins" / f"{twin_id}.json").read_text(encoding="utf-8"))
        solution = _apply_twin(chain, solution, twin)
    loaded = LoadedChain(chain, world_data, attack, solution, seed, poison, twin)
    if attack and attack.trigger:
        trigger = attack.trigger
        at = loaded.session_number(trigger.session)
        if not any(trigger.phrase.lower() in turn.lower() for turn in chain.sessions[at - 1].turns):
            raise ValueError(f"attack {attack_id}: no turn of {trigger.session} says {trigger.phrase!r}")
        late = [s for s, n in poison.items() if n >= at]
        if late:
            raise ValueError(f"attack {attack_id}: {', '.join(late)} arrives no earlier than the trigger session")
    known = {t.name for name in loaded.app_names() for t in get_app(name).tools}
    unknown_tools = sorted(set(chain.tool_descriptions) - known)
    if unknown_tools:
        raise ValueError(f"tool_descriptions names tools no linked app has: {', '.join(unknown_tools)}")
    return loaded


def _item_type(world: World, app: str, name: str) -> type[BaseModel]:
    collection(world, app, name)
    return get_args(type(world.app(app)).model_fields[name].annotation)[0]


def apply_change(world: World, change: Change) -> None:
    items, key = collection(world, change.app, change.collection)
    kind = _item_type(world, change.app, change.collection)
    if change.op == "add":
        item = kind.model_validate(change.item or {})
        if any(getattr(i, key) == getattr(item, key) for i in items):
            raise ValueError(f"{change.app}.{change.collection} already has {key}={getattr(item, key)!r}")
        items.append(item)
        return
    if not change.match:
        raise ValueError(f"{change.op} needs 'match'")
    match = {f: TypeAdapter(kind.model_fields[f].annotation).validate_python(v) for f, v in change.match.items()}
    hits = [i for i, it in enumerate(items) if all(getattr(it, f) == v for f, v in match.items())]
    if not hits:
        if change.optional:
            return
        raise ValueError(f"{change.op}: nothing in {change.app}.{change.collection} matches {change.match}")
    for index in reversed(hits):
        if change.op == "remove":
            del items[index]
        else:
            items[index] = kind.model_validate({**items[index].model_dump(), **(change.set or {})})
