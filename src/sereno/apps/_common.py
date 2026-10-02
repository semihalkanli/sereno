"""Small helpers shared by app modules.

Only behaviour that is identical across apps lives here; formats that copy a
particular real API stay in that app's module.
"""

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Annotated, Any

from pydantic import AfterValidator

from sereno.tools import ToolError


def find[T](items: Iterable[T], error: str, /, **match: Any) -> T:
    """The first item whose fields equal `match`, or a ToolError with the app's own message."""
    for item in items:
        if all(getattr(item, field) == value for field, value in match.items()):
            return item
    raise ToolError(error)


def fresh_id(make: Callable[[int], str], taken: Iterable[str], start: int) -> str:
    """`make(n)` for the first `n` from `start` whose id is not taken."""
    used = set(taken)
    n = start
    while make(n) in used:
        n += 1
    return make(n)


def has_words(words: Iterable[str], *fields: object) -> bool:
    """Every word occurs, case-insensitively, somewhere in the fields."""
    text = " ".join(map(str, fields)).lower()
    return all(w in text for w in words)


def local_time(t: datetime) -> datetime:
    """A time as naive local time: an incoming offset is dropped, not converted, as the world clock is local."""
    return t.replace(tzinfo=None)


LocalTime = Annotated[datetime, AfterValidator(local_time)]
"""A datetime argument or field read as the person's local time."""


def iso_seconds(t: datetime | None) -> str | None:
    return t.isoformat(timespec="seconds") if t else None


def plain_stamp(t: datetime | None) -> str | None:
    return t.strftime("%Y-%m-%d %H:%M:%S") if t else None


def money(amount: float) -> float:
    return round(amount, 2)
