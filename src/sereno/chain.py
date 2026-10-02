"""Chain files: one person's long task over several sessions, read from disk.

A chain lives in its own directory under `chains/` and is made by copying
`chains/_template/`. The format is described field by field in
`chains/README.md`. Apps are written once in `sereno.apps`; a chain only names
the ones it links.

Placeholders use double braces. `{{slot:<id>}}` marks a place where a poison
can go, in any text of the world data, a session change or the history; with
no attack it becomes the slot's default text. The system prompt also takes
`{{owner_name}}`, `{{owner_email}}`, `{{now}}` and `{{apps}}`, filled per
session.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, TypeAdapter

from sereno.apps import get_app
from sereno.checks import Check, collection
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
    checks: list[Check] = []


class Chain(BaseModel, extra="forbid"):
    id: str
    name: str
    summary: str
    objective: str = ""
    apps: list[str] = Field(min_length=1)
    system_prompt: str
    slots: list[Slot] = []
    sessions: list[Session] = Field(min_length=1)
    final_checks: list[Check] = []


class Attack(BaseModel, extra="forbid"):
    id: str
    objective: str
    marker: str
    fills: dict[str, str]
    checks: list[Check] = []


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


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


class LoadedChain:
    """A chain with its slots filled, ready to run: the chain, its world data and the attack, if any."""

    def __init__(self, chain: Chain, world_data: dict[str, Any], attack: Attack | None, solution: list | None):
        self.chain = chain
        self.world_data = world_data
        self.attack = attack
        self.solution = solution

    def initial_world(self) -> World:
        return World.load(self.world_data, self.chain.apps)

    def system_prompt(self, world: World) -> str:
        values = {
            "owner_name": world.owner.name,
            "owner_email": world.owner.email,
            "now": f"{world.now:%A %d %B %Y, %H:%M}",
            "apps": _join([get_app(n).title for n in self.chain.apps]),
        }

        def sub(m: re.Match) -> str:
            if m.group(1) not in values:
                raise ValueError(f"unknown placeholder {m.group(0)} in system_prompt")
            return values[m.group(1)]

        return PLACEHOLDER.sub(sub, self.chain.system_prompt)


def chain_ids() -> list[str]:
    return sorted(p.name for p in CHAINS_DIR.iterdir() if (p / "chain.json").exists())


def load_chain(chain_id: str, attack_id: str | None = None, root: Path = CHAINS_DIR) -> LoadedChain:
    directory = root / chain_id
    raw_chain = json.loads((directory / "chain.json").read_text(encoding="utf-8"))
    raw_world = json.loads((directory / "world.json").read_text(encoding="utf-8"))
    solution_path = directory / "solution.json"
    solution = json.loads(solution_path.read_text(encoding="utf-8")) if solution_path.exists() else None

    slots = {s.id: s for s in Chain.model_validate(raw_chain).slots}
    fills = {sid: s.default for sid, s in slots.items()}
    attack = None
    if attack_id is not None:
        attack = Attack.model_validate_json((directory / "attacks" / f"{attack_id}.json").read_text(encoding="utf-8"))
        unknown = [s for s in attack.fills if s not in slots]
        if unknown:
            raise ValueError(f"attack {attack_id} fills undeclared slots: {', '.join(unknown)}")
        if not any(attack.marker in text for text in attack.fills.values()):
            raise ValueError(f"attack {attack_id}: no fill carries the marker {attack.marker!r}")
        fills.update(attack.fills)

    used: set[str] = set()
    chain = Chain.model_validate(_fill_slots(raw_chain, fills, used))
    world_data = _fill_slots(raw_world, fills, used)
    unused = [s for s in slots if s not in used]
    if unused:
        raise ValueError(f"slots declared but never placed: {', '.join(unused)}")
    if chain.id != chain_id:
        raise ValueError(f"chain id {chain.id!r} does not match its directory {chain_id!r}")
    return LoadedChain(chain, world_data, attack, solution)


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
        raise ValueError(f"{change.op}: nothing in {change.app}.{change.collection} matches {change.match}")
    for index in reversed(hits):
        if change.op == "remove":
            del items[index]
        else:
            items[index] = kind.model_validate({**items[index].model_dump(), **(change.set or {})})
