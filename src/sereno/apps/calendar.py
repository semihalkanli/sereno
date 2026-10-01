"""Calendar: the person's events, with list and create."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from sereno.apps import App
from sereno.tools import Tool, ToolError

if TYPE_CHECKING:
    from sereno.world import World


class Event(BaseModel):
    id: str
    title: str
    start: datetime
    end: datetime
    participants: list[str] = []
    location: str = ""
    description: str = ""


class Calendar(BaseModel):
    events: list[Event] = []


def _calendar(world: World) -> Calendar:
    return world.app("calendar")


def _event_view(e: Event) -> dict:
    return {
        "id": e.id,
        "title": e.title,
        "start": e.start.isoformat(timespec="minutes"),
        "end": e.end.isoformat(timespec="minutes"),
        "participants": e.participants,
        "location": e.location,
    }


class ListEventsArgs(BaseModel):
    start_date: date = Field(description="First day to list, YYYY-MM-DD.")
    end_date: date = Field(description="Last day to list (inclusive), YYYY-MM-DD.")


def list_events(world: World, args: ListEventsArgs) -> list[dict]:
    if args.end_date < args.start_date:
        raise ToolError("end_date is before start_date.")
    events = [e for e in _calendar(world).events if args.start_date <= e.start.date() <= args.end_date]
    events.sort(key=lambda e: e.start)
    return [_event_view(e) for e in events]


class CreateEventArgs(BaseModel):
    title: str
    start: datetime = Field(description="Start, YYYY-MM-DDTHH:MM.")
    end: datetime = Field(description="End, YYYY-MM-DDTHH:MM.")
    participants: list[str] = Field([], description="Email addresses to invite.")
    location: str = ""
    description: str = ""


def create_event(world: World, args: CreateEventArgs) -> dict:
    if args.end <= args.start:
        raise ToolError("end must be after start.")
    calendar = _calendar(world)
    event = Event(id=f"evt-{len(calendar.events) + 1}", **args.model_dump())
    calendar.events.append(event)
    return {"status": "created", **_event_view(event)}


APP = App(
    name="calendar",
    title="calendar",
    state=Calendar,
    keys={"events": "id"},
    tools=[
        Tool(
            "list_events", "calendar", "List the user's calendar events between two dates.", ListEventsArgs, list_events
        ),
        Tool(
            "create_event",
            "calendar",
            "Create an event in the user's calendar and invite participants.",
            CreateEventArgs,
            create_event,
            writes=True,
        ),
    ],
)
