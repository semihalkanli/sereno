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


def test_advance_to_moves_the_clock_forward_only(world):
    world.advance_to(datetime(2025, 11, 9, 8, 0))
    assert world.now == datetime(2025, 11, 9, 8, 0)
    with pytest.raises(ValueError, match="cannot go back"):
        world.advance_to(datetime(2025, 11, 1))


def test_alternative_conditions_match_the_same_item_as_the_required_conditions(world):
    check = Check(
        name="a-with-date",
        check="count",
        app="tasks",
        collection="tasks",
        where={"title": Cond(eq="a")},
        where_any=[{"created_at": Cond(gt="2025-11-05")}],
        equals=1,
    )
    # Task b supplies the date, but it cannot satisfy task a's alternative.
    assert not evaluate(check, world, world)
    check.where_any.append({"created_at": Cond(eq="2025-11-01T08:00")})
    assert evaluate(check, world, world)


def test_only_keeps_single_item_requirement_when_using_alternatives(world):
    check = Check(
        name="one-task",
        check="only",
        app="tasks",
        collection="tasks",
        where={"title": Cond(eq="a")},
        where_any=[{"created_at": Cond(eq="2025-11-01T08:00")}],
    )
    assert not evaluate(check, world, world)
    post = world.copy()
    post.app("tasks").tasks = post.app("tasks").tasks[:1]
    assert evaluate(check, world, post)
    post.app("tasks").tasks[0].title = "b"
    assert not evaluate(check, world, post)


def test_new_filters_old_matches_before_evaluating_alternatives(world):
    check = Check(
        name="new-task",
        check="count",
        app="tasks",
        collection="tasks",
        new=True,
        where_any=[{"title": Cond(eq="a")}],
        min=1,
    )
    assert not evaluate(check, world, world)
    post = world.copy()
    post.app("tasks").tasks.append(Task(id="task_004", title="a"))
    assert evaluate(check, world, post)


@pytest.mark.parametrize(
    ("kind", "alternatives", "message"),
    [
        ("unchanged", [{"title": {"eq": "a"}}], "only supported"),
        ("count", [{}], "at least one field"),
    ],
)
def test_invalid_alternatives_fail_during_check_parsing(kind, alternatives, message):
    with pytest.raises(ValueError, match=message):
        Check(name="invalid", check=kind, app="tasks", collection="tasks", where_any=alternatives)
