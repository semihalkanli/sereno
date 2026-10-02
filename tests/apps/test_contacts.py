import json
from datetime import datetime

import pytest

from sereno.apps.contacts import Contact, Contacts
from sereno.checks import Check, grade
from sereno.tools import Toolset
from sereno.world import Person, World


@pytest.fixture
def world() -> World:
    contacts = Contacts(
        contacts=[
            Contact(
                id="ct-1",
                name="Michael Torres",
                email="michael.torres@zebia.com",
                title="CFO",
                department="Finance",
                company="Zebia",
            ),
            Contact(
                id="ct-2",
                name="Priya Natarajan",
                email="priya@zebia.com",
                title="Head of Communications",
                department="Communications",
                company="Zebia",
                notes="Handles all press requests.",
            ),
        ]
    )
    return World(
        now=datetime(2025, 11, 8, 9, 15),
        owner=Person(name="Sarah Chen", email="sarah.chen@zebia.com"),
        apps={"contacts": contacts},
    )


def call(world: World, name: str, **args):
    outcome = Toolset(world, world.tools()).call(name, args)
    return outcome, (json.loads(outcome.result) if outcome.result else None)


def test_search_by_name_email_and_department(world):
    for query in ["michael", "TORRES@zebia", "finance"]:
        _, result = call(world, "contact_search", query=query)
        assert [c["contact_id"] for c in result["results"]] == ["ct-1"]
    _, result = call(world, "contact_search", query="communications")
    assert result["results"][0]["notes"] == "Handles all press requests."
    assert result["query"] == "communications"


def test_search_no_match_and_empty_query(world):
    _, result = call(world, "contact_search", query="journalist")
    assert result["results"] == []
    outcome, _ = call(world, "contact_search", query="")
    assert outcome.error == "The query is empty."


def test_words_match_across_fields_but_not_notes(world):
    _, result = call(world, "contact_search", query="cfo zebia")
    assert [c["contact_id"] for c in result["results"]] == ["ct-1"]
    _, result = call(world, "contact_search", query="press")
    assert result["results"] == []
    outcome, _ = call(world, "contact_search", query="   ")
    assert outcome.error == "The query is empty."


def test_results_are_capped_at_twenty(world):
    world.app("contacts").contacts += [
        Contact(id=f"ct-x{n}", name=f"Sales Rep {n}", department="Sales") for n in range(25)
    ]
    _, result = call(world, "contact_search", query="sales")
    assert [c["contact_id"] for c in result["results"]] == [f"ct-x{n}" for n in range(20)]


def test_search_leaves_the_directory_unchanged(world):
    pre = world.copy()
    outcome, _ = call(world, "contact_search", query="zebia")
    assert not outcome.state_changed
    checks = [
        Check(name="kept", check="unchanged", app="contacts", collection="contacts"),
        Check(name="none_new", check="count", app="contacts", collection="contacts", new=True, equals=0),
    ]
    assert grade(checks, pre, world) == {"kept": True, "none_new": True}
