"""Apps a person can connect, each written once and linked by chains.

An app is a pydantic model for its state plus the tools that read and change
it, defined as `APP` in its own module `sereno.apps.<name>`. A chain names the
apps its person has connected; the world holds one state per linked app, and
the agent sees only those apps' tools. Apps are imported by name on first use,
so adding an app means adding one module and nothing else.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from importlib import import_module
from pkgutil import iter_modules
from typing import Any

from pydantic import BaseModel

from sereno.tools import Tool


@dataclass(frozen=True)
class App:
    name: str
    title: str
    state: type[BaseModel]
    tools: list[Tool]
    keys: dict[str, str]
    """Collection name -> the field that identifies an item, for checks and changes."""
    advance: Callable[[Any], None] | None = None
    """Brings state that depends on the clock up to `world.now` (a charge taken on its date, a job that ran).
    Called whenever the world clock moves, so the stored state matches what the tools show."""

    def __post_init__(self) -> None:
        object.__setattr__(self, "tools", [replace(t, app=self.name) for t in self.tools])


_cache: dict[str, App] = {}


def get_app(name: str) -> App:
    """The app defined as `APP` in `sereno.apps.<name>`, imported on first use."""
    if name not in _cache:
        if not name.isidentifier() or name.startswith("_"):
            raise ValueError(f"unknown app {name!r}")
        try:
            module = import_module(f"sereno.apps.{name}")
        except ModuleNotFoundError as e:
            if e.name == f"sereno.apps.{name}":
                raise ValueError(f"unknown app {name!r}") from None
            raise
        app = module.APP
        if app.name != name:
            raise ValueError(f"sereno.apps.{name} defines APP named {app.name!r}")
        _cache[name] = app
    return _cache[name]


def app_names() -> list[str]:
    return sorted(m.name for m in iter_modules(__path__) if not m.name.startswith("_"))
