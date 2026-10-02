from datetime import datetime

import pytest

from sereno.apps.tasks import Task, Tasks
from sereno.checks import Check, Cond, evaluate
from sereno.world import Person, World


@pytest.fixture
def world() -> World:
    tasks = Tasks(
        tasks=[
            Task(id="task_001", title="a", created_at=datetime(2025, 11, 1, 8, 0)),
            Task(id="task_002", title="b", created_at=datetime(2025, 11, 7, 8, 0)),
            Task(id="task_003", title="c"),
        ]
    )
    return World(
        now=datetime(2025, 11, 8, 9, 15),
        owner=Person(name="Sarah Chen", email="sarah.chen@zebia.com"),
        apps={"tasks": tasks},
    )


def count(world: World, **cond) -> int:
    """How many tasks match the condition on created_at."""
    for n in range(len(world.app("tasks").tasks) + 1):
        check = Check(
            name="n", check="count", app="tasks", collection="tasks", where={"created_at": Cond(**cond)}, equals=n
        )
        if evaluate(check, world, world):
            return n
    raise AssertionError("no count matched")


def test_contains_on_an_empty_field_fails_instead_of_crashing(world):
    assert count(world, contains="2025") == 2


def test_contains_matches_non_text_values_as_text(world):
    assert count(world, contains="2025-11-07") == 1


def test_comparisons_validate_the_bound_as_the_field_type(world):
    assert count(world, gt="2025-11-05T00:00") == 1
    assert count(world, gte="2025-11-01T08:00") == 2
    assert count(world, gte="2025-11-01", lt="2025-11-05") == 1
    assert count(world, lte="2025-12-01") == 2
