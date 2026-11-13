"""The model API behind one interface.

The agent loop speaks turns, never provider bodies. An adapter translates turns to its API's shape and back, so a new
API is one module and a new model of an existing API is one configuration block.

Reasoning is provider-private state: an adapter keeps what its API returned on an assistant turn and replays it
verbatim on later turns (decision 370). The loop never reads it.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ToolSpec:
    """One tool as the model sees it: a name, a description and a JSON Schema for the arguments."""

    name: str
    description: str
    parameters: dict


@dataclass(frozen=True)
class ToolCall:
    """One call the model asked for. `arguments` is the raw JSON text, so an adapter can replay it byte for byte."""

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class Completion:
    """One model answer. `reasoning` holds the provider's private fields exactly as returned."""

    text: str
    tool_calls: tuple[ToolCall, ...]
    reasoning: dict
    finish_reason: str | None
    usage: dict
    model: str | None = None
    provider: str | None = None
    generation_id: str | None = None
    declined: bool = False

    def record(self) -> dict:
        return {
            "declined": self.declined,
            "finish_reason": self.finish_reason,
            "returned_model": self.model,
            "provider": self.provider,
            "generation_id": self.generation_id,
            "usage": self.usage,
            "cost": self.usage.get("cost"),
            "tool_calls": len(self.tool_calls),
            "reasoning_fields": sorted(self.reasoning),
        }


@dataclass(frozen=True)
class SystemTurn:
    text: str


@dataclass(frozen=True)
class UserTurn:
    text: str


@dataclass(frozen=True)
class AssistantTurn:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolTurn:
    call_id: str
    name: str
    text: str
    failed: bool = False


Turn = SystemTurn | UserTurn | AssistantTurn | ToolTurn


class Provider(Protocol):
    """One API's client. `complete` raises `ProviderFailure` for anything that is not a usable answer."""

    name: str

    def complete(self, turns: Sequence[Turn], tools: Sequence[ToolSpec], call: int) -> Completion: ...

    def close(self) -> None: ...
