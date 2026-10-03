"""The deterministic, stateful world one person lives in.

The world holds the clock, the person and one state per linked app. Tools read
and change it; nothing here calls a model. A chain builds the initial world
from its data file, and checks compare the world before and after a session.
"""

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from sereno.apps import get_app


class Person(BaseModel):
    name: str
    email: str


class World:
    def __init__(self, now: datetime, owner: Person, apps: dict[str, BaseModel]) -> None:
        self.now = now
        self.owner = owner
        self.apps = apps

    @property
    def today(self) -> date:
        return self.now.date()

    @classmethod
    def load(cls, data: dict[str, Any], app_names: list[str]) -> "World":
        """Build a world from a chain's data: {now, owner, apps: {name: state}}."""
        app_data = data.get("apps", {})
        unlinked = [n for n in app_data if n not in app_names]
        if unlinked:
            raise ValueError(f"data for apps the chain does not link: {', '.join(unlinked)}")
        return cls(
            now=datetime.fromisoformat(data["now"]),
            owner=Person.model_validate(data["owner"]),
            apps={n: get_app(n).state.model_validate(app_data.get(n, {})) for n in app_names},
        )

    def advance_to(self, now: datetime) -> None:
        """Move the clock forward and let each app update the state that depends on it."""
        if now < self.now:
            raise ValueError(f"the clock cannot go back from {self.now} to {now}")
        self.now = now
        for name in self.apps:
            hook = get_app(name).advance
            if hook is not None:
                hook(self)

    def start_session(self) -> None:
        """Let each app clear the state that belongs to one conversation."""
        for name in self.apps:
            hook = get_app(name).start_session
            if hook is not None:
                hook(self)

    def app(self, name: str, required: bool = True) -> Any:
        state = self.apps.get(name)
        if state is None and required:
            raise KeyError(f"app {name!r} is not linked")
        return state

    def tools(self) -> list:
        return [tool for name in self.apps for tool in get_app(name).tools]

    def snapshot(self) -> dict:
        return {
            "today": self.today.isoformat(),
            "now": self.now.isoformat(),
            "owner": self.owner.model_dump(),
            **{name: state.model_dump(mode="json") for name, state in self.apps.items()},
        }

    def copy(self) -> "World":
        return World(self.now, self.owner.model_copy(), {n: s.model_copy(deep=True) for n, s in self.apps.items()})
