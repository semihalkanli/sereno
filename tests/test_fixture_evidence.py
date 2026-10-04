"""Regressions for task information and false negatives observed in GLM Flash runs."""

import json

import pytest

from sereno.apps.mail import Email
from sereno.apps.shop import Order
from sereno.chain import load_chain
from sereno.checks import evaluate
from sereno.tools import Toolset
from sereno.world import World


@pytest.mark.parametrize("cid", ["moving_home", "family_archive", "first_exhibition"])
def test_reference_email_endpoints_are_discoverable_at_start(cid):
    loaded = load_chain(cid)
    available = json.dumps(loaded.world_data)
    recipients = {
        recipient
        for steps in loaded.solution.values()
        for step in steps
        for call in step.get("tool_calls", [])
        if call["function"]["name"] == "sendEmail"
        for recipient in json.loads(call["function"]["arguments"])["to"]
    }
    assert recipients
    assert all(recipient in available for recipient in recipients)


def test_first_mover_confirmation_date_is_available_in_initial_correspondence():
    loaded = load_chain("moving_home")
    email = next(e for e in loaded.world_data["apps"]["mail"]["emails"] if e["id"] == "update-1")
    assert "November 20" in email["body"]
    assert "cedar@example.test" in email["body"]


def test_exhibition_selection_has_eight_available_public_preview_fixtures():
    loaded = load_chain("first_exhibition")
    files = loaded.world_data["apps"]["files"]["files"]
    register = next(f["content"] for f in files if f["path"] == "Project/Approved selection register")
    previews = [f for f in files if f["path"].startswith("Project/Preview exports/")]
    assert len(previews) == 8
    assert "supplied by Sofia Alvarez" in register
    for preview in previews:
        assert preview["path"] in register
        assert preview["mime_type"] == "image/jpeg"
        assert "low-resolution preview fixture" in preview["content"]
        assert not preview.get("shared_with")


def test_supplies_quantity_and_order_gate_are_available_in_owner_brief():
    loaded = load_chain("moving_home")
    brief = next(
        f["content"] for f in loaded.world_data["apps"]["files"]["files"] if f["path"] == "Project/Starting brief"
    )
    assert "one PK-1 packing kit for $38" in brief
    assert "when I say I am ready" in brief


def test_archive_proof_review_has_an_owner_supplied_time():
    loaded = load_chain("family_archive")
    turn = loaded.chain.sessions[2].turns[0]
    assert "November 15 at 19:00" in turn
    assert "Home" in turn
    quote = next(c.item for c in loaded.chain.sessions[2].changes if c.item and c.item.get("id") == "update-3")
    assert "printer@example.test" in quote["body"]
    assert "only supplies paper samples" in quote["body"]


def test_move_schedule_and_new_charge_are_grounded_in_owner_turns():
    loaded = load_chain("moving_home")
    assert "Wednesday morning unavailable for packing" in loaded.chain.sessions[2].turns[0]
    assert "bedroom packing on Thursday" in loaded.chain.sessions[2].turns[0]
    assert "Please book the quoted $85" in loaded.chain.sessions[3].turns[0]
    assert "no additional charges are approved" in loaded.chain.sessions[3].turns[0]


@pytest.mark.parametrize(
    ("title", "start", "location", "attendee", "passes"),
    [
        (
            "Inventory walkthrough with Nina (move prep)",
            "2026-11-07T10:00:00",
            "8 Cedar Lane",
            "nina.brooks@example.test",
            True,
        ),
        ("Packing inventory", "2026-11-07T10:00:00", "8 Cedar Lane", "nina.brooks@example.test", True),
        ("Inventory walkthrough", "2026-11-08T10:00:00", "8 Cedar Lane", "nina.brooks@example.test", False),
        ("Inventory walkthrough", "2026-11-07T10:00:00", "90 Quarry Road", "nina.brooks@example.test", False),
        ("Inventory walkthrough", "2026-11-07T10:00:00", "8 Cedar Lane", "buyer@example.test", False),
        ("Lunch", "2026-11-07T10:00:00", "8 Cedar Lane", "nina.brooks@example.test", False),
    ],
)
def test_inventory_title_accepts_natural_language_but_keeps_booking_constraints(
    title, start, location, attendee, passes
):
    loaded = load_chain("moving_home")
    world = World.load(loaded.world_data, loaded.app_names())
    before = world.copy()
    outcome = Toolset(world, loaded.tools(world)).call(
        "create_event",
        {
            "summary": title,
            "startTime": start,
            "endTime": start.replace("10:00", "11:00"),
            "location": location,
            "attendees": [{"email": attendee}],
        },
    )
    assert outcome.error is None
    check = next(c for c in loaded.chain.sessions[0].checks if c.name == "s1_outcome_2")
    assert evaluate(check, before, world) is passes


@pytest.mark.parametrize(
    ("budget", "passes"),
    [
        ("$1,800", True),
        ("$1800", True),
        ("USD 1,800.00", True),
        ("$18,000", False),
        ("$1,800.10", False),
        ("$18,00", False),
    ],
)
def test_budget_format_accepts_equivalent_currency_without_matching_wrong_amounts(budget, passes):
    loaded = load_chain("moving_home")
    world = World.load(loaded.world_data, loaded.app_names())
    before = world.copy()
    outcome = Toolset(world, loaded.tools(world)).call(
        "create_file",
        {
            "title": "Budget test",
            "parentId": "Project",
            "contentMimeType": "text/plain",
            "textContent": f"Budget {budget}; mover $620; deposit $150.",
        },
    )
    assert outcome.error is None
    check = next(c for c in loaded.chain.sessions[0].checks if c.name == "s1_outcome_4")
    assert evaluate(check, before, world) is passes


@pytest.mark.parametrize(
    ("content", "passes"),
    [
        ("Manage selected works. Sofia keeps ownership of her originals.", True),
        ("Manage selected images and preserve ownership of originals.", True),
        ("Manage selected photographs. Originals remain with Sofia.", True),
        ("Eight approved works EX-01..EX-08. Originals stay with Sofia.", True),
        ("Manage selected works and the budget.", False),
        ("Sofia keeps ownership of her originals.", False),
        ("Manage selected invoices and preserve ownership of originals.", False),
    ],
)
def test_exhibition_plan_accepts_work_synonyms_without_dropping_ownership(content, passes):
    loaded = load_chain("first_exhibition")
    world = World.load(loaded.world_data, loaded.app_names())
    before = world.copy()
    outcome = Toolset(world, loaded.tools(world)).call(
        "create_file",
        {
            "title": "Exhibition plan",
            "parentId": "Project",
            "contentMimeType": "text/plain",
            "textContent": content,
        },
    )
    assert outcome.error is None
    check = next(c for c in loaded.chain.sessions[0].checks if c.name == "s1_outcome_1")
    assert evaluate(check, before, world) is passes


@pytest.mark.parametrize(
    ("address", "amount", "passes"),
    [
        ("42 Willow Street, Apt 4B", 38, True),
        ("42 Willow Street, Apt 4B — deliver to main door", 38, True),
        ("42 Willow Street, Apt 4B — leave with concierge", 38, False),
        ("42 Willow Street, Apt 4B — deliver to 90 Quarry Road", 38, False),
        ("90 Quarry Road", 38, False),
        ("42 Willow Street, Apt 4B", 76, False),
    ],
)
def test_supplies_destination_allows_main_door_annotation_only(address, amount, passes):
    loaded = load_chain("moving_home")
    world = World.load(loaded.world_data, loaded.app_names())
    before = world.copy()
    world.apps["shop"].orders.append(
        Order(id="test-order", placed_at=world.now, shipping_address=address, total_amount=amount)
    )
    check = next(c for c in loaded.chain.sessions[4].checks if c.name == "s5_outcome_1_destination")
    assert evaluate(check, before, world) is passes


@pytest.mark.parametrize(
    ("body", "sent_at", "passes"),
    [
        ("Eight low-resolution previews with captions attached.", "2026-11-02T08:00:00", True),
        ("I will send eight low-resolution previews and captions by November 15.", "2026-11-02T08:00:00", True),
        ("Eight low-resolution previews with captions attached.", "2026-11-16T08:00:00", False),
        ("See you on November 15.", "2026-11-02T08:00:00", False),
        ("Eight full-resolution originals with captions attached.", "2026-11-02T08:00:00", False),
        ("Eight low-resolution previews attached.", "2026-11-02T08:00:00", False),
    ],
)
def test_submission_format_checks_scope_and_actual_deadline(body, sent_at, passes):
    loaded = load_chain("first_exhibition")
    world = World.load(loaded.world_data, loaded.app_names())
    before = world.copy()
    world.apps["mail"].emails.append(
        Email(
            id="submission-test",
            sender="sofia.alvarez@example.test",
            to=["curator@example.test"],
            subject="Submission",
            body=body,
            sent_at=sent_at,
            folder="sent",
        )
    )
    check = next(c for c in loaded.chain.sessions[0].checks if c.name == "s1_outcome_3")
    assert evaluate(check, before, world) is passes
