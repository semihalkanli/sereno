"""Tool definitions and the tool set an agent sees.

A tool is a plain function over the world with a pydantic model for its
arguments; the model also gives the JSON schema sent to the LLM. The loop only
needs `schemas()` and `call()`, so it does not know which world it runs in.
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError


class ToolError(Exception):
    """An error the agent should see as the tool's answer, not a crash."""


class NoArgs(BaseModel):
    pass


def _inline_refs(schema: dict) -> dict:
    """Replace local `$ref`s with their definitions, since not every provider resolves them."""
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                target = resolve(defs[ref.removeprefix("#/$defs/")])
                return {**target, **{k: resolve(v) for k, v in node.items() if k != "$ref"}}
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(schema)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args: type[BaseModel]
    fn: Callable[[Any, Any], Any]
    writes: bool = False
    app: str = ""
    """Set by the `App` that lists the tool."""

    def schema(self) -> dict:
        parameters = _inline_refs(self.args.model_json_schema())
        parameters.pop("title", None)
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": parameters},
        }


@dataclass
class ToolOutcome:
    result: str
    error: str | None
    provenance: dict[str, Any]
    state_changed: bool


def _to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


class Toolset:
    def __init__(self, world: Any, tools: list[Tool]) -> None:
        self.world = world
        self.tools = {t.name: t for t in tools}
        if len(self.tools) != len(tools):
            names = [t.name for t in tools]
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"two linked apps define the same tool name: {', '.join(dupes)}")

    def schemas(self) -> list[dict]:
        return [t.schema() for t in self.tools.values()]

    def call(self, name: str, raw_args: dict[str, Any]) -> ToolOutcome:
        tool = self.tools.get(name)
        if tool is None:
            return self._outcome(name, "unknown", "", f"Unknown tool {name!r}.", False)
        before = self.world.snapshot() if tool.writes else None
        try:
            args = tool.args.model_validate(raw_args)
            result, error = _to_text(tool.fn(self.world, args)), None
        except ValidationError as e:
            result, error = "", f"Invalid arguments: {e.errors(include_url=False)}"
        except ToolError as e:
            result, error = "", str(e)
        changed = tool.writes and self.world.snapshot() != before
        return self._outcome(name, tool.app, result, error, changed)

    @staticmethod
    def _outcome(name: str, app: str, result: str, error: str | None, changed: bool) -> ToolOutcome:
        provenance = {
            "channel": f"{app}.{name}",
            "sha256": hashlib.sha256(result.encode()).hexdigest()[:16],
        }
        return ToolOutcome(result, error, provenance, changed)
