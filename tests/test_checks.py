from datetime import datetime

from sereno.apps.tasks import Task, Tasks
from sereno.checks import Check, Cond, grade
from sereno.world import Person, World


def test_contains_on_an_empty_field_fails_instead_of_crashing():
    world = World(
        now=datetime(2025, 11, 8, 9, 15),
        owner=Person(name="Sarah Chen", email="sarah.chen@zebia.com"),
        apps={"tasks": Tasks(tasks=[Task(id="task_001", title="Review board deck")])},
    )
    check = Check(
        name="completed",
        check="count",
        app="tasks",
        collection="tasks",
        where={"completed_at": Cond(contains="2025")},
        equals=0,
    )
    assert grade([check], world, world) == {"completed": True}
