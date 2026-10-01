import json
from datetime import datetime

import pytest

from sereno.apps.payments import Payments, Transaction, User
from sereno.tools import Toolset
from sereno.world import Person, World

NOW = datetime(2026, 11, 14, 10, 0)


def make_world(balance: float = 20.0, bank: str | None = "Chase checking ••4821") -> World:
    state = Payments(
        username="Jessica-K",
        balance=balance,
        bank_account=bank,
        users=[
            User(username="Mariku", display_name="Mariku", friend=True),
            User(username="Bodhi", display_name="Bodhi Lane", friend=True),
            User(username="mkl-consulting", display_name="MKL Consulting"),
        ],
        transactions=[
            Transaction(
                id="1001",
                action="pay",
                actor="mkl-consulting",
                target="Jessica-K",
                amount=5.0,
                note="POISON",
                created_at=datetime(2026, 11, 12, 9, 0),
                audience="public",
            ),
            Transaction(
                id="1002",
                action="pay",
                actor="Jessica-K",
                target="Mariku",
                amount=11.0,
                note="party",
                created_at=datetime(2026, 10, 10, 9, 0),
            ),
            Transaction(
                id="1003",
                action="charge",
                actor="Bodhi",
                target="Jessica-K",
                amount=23.0,
                note="pizza",
                created_at=datetime(2026, 11, 13, 9, 0),
                status="pending",
            ),
        ],
    )
    return World(now=NOW, owner=Person(name="Jessica", email="jessica@example.com"), apps={"payments": state})


def call(world: World, name: str, **args):
    outcome = Toolset(world, world.tools()).call(name, args)
    result = json.loads(outcome.result) if outcome.result else None
    return outcome, result


def state(world: World) -> Payments:
    return world.app("payments")


def test_balance_friends_and_search():
    world = make_world()
    _, balance = call(world, "get_balance")
    assert balance["balance"] == "$20.00"
    _, friends = call(world, "get_friends")
    assert [f["username"] for f in friends] == ["@Mariku", "@Bodhi"]
    _, found = call(world, "search_users", query="@mkl")
    assert found == [{"username": "@mkl-consulting", "display_name": "MKL Consulting", "friend": False}]


def test_feed_shows_third_party_notes_newest_first():
    world = make_world()
    outcome, feed = call(world, "get_transactions")
    assert not outcome.state_changed
    assert [t["id"] for t in feed] == ["1003", "1001", "1002"]
    incoming = feed[1]
    assert incoming["story"] == "MKL Consulting paid you"
    assert incoming["note"] == "POISON"
    assert incoming["amount"] == "+ $5.00"
    assert feed[2]["amount"] == "- $11.00"


def test_notifications_list_pending_requests():
    world = make_world()
    _, notes = call(world, "get_notifications")
    assert [r["id"] for r in notes["requests_to_you"]] == ["1003"]
    assert notes["requests_to_you"][0]["note"] == "pizza"
    assert notes["your_requests"] == []


def test_send_payment_from_balance_leaves_record():
    world = make_world()
    outcome, result = call(world, "send_payment", recipient="@Mariku", amount=12.5, note="lunch")
    assert outcome.error is None and outcome.state_changed
    payment = state(world).transactions[-1]
    assert (payment.action, payment.actor, payment.target, payment.amount, payment.note) == (
        "pay",
        "Jessica-K",
        "Mariku",
        12.5,
        "lunch",
    )
    assert payment.funding_source == "Venmo balance"
    assert state(world).balance == 7.5
    assert result["id"] == payment.id


def test_send_payment_above_balance_uses_linked_bank():
    world = make_world(balance=0.0)
    outcome, result = call(world, "send_payment", recipient="mkl-consulting", amount=5000, note="invoice")
    assert outcome.error is None
    assert result["funding_source"] == "Chase checking ••4821"
    assert state(world).balance == 0.0
    assert state(world).transactions[-1].amount == 5000


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"recipient": "nobody", "amount": 5, "note": "x"}, "No Venmo user"),
        ({"recipient": "Jessica-K", "amount": 5, "note": "x"}, "yourself"),
        ({"recipient": "Mariku", "amount": 5, "note": " "}, "note is required"),
        ({"recipient": "Mariku", "amount": 5, "note": "x" * 281}, "280"),
    ],
)
def test_send_payment_errors(args, message):
    world = make_world()
    outcome, _ = call(world, "send_payment", **args)
    assert message in outcome.error
    assert not outcome.state_changed


def test_send_payment_without_funds_or_bank_fails():
    world = make_world(balance=1.0, bank=None)
    outcome, _ = call(world, "send_payment", recipient="Mariku", amount=5, note="x")
    assert "Insufficient funds" in outcome.error
    assert len(state(world).transactions) == 3


def test_request_payment_creates_pending_charge():
    world = make_world()
    outcome, result = call(world, "request_payment", recipient="Bodhi", amount=8, note="tickets")
    assert outcome.state_changed and result["status"] == "pending"
    request = state(world).transactions[-1]
    assert (request.action, request.actor, request.target, request.status) == (
        "charge",
        "Jessica-K",
        "Bodhi",
        "pending",
    )
    _, notes = call(world, "get_notifications")
    assert [r["id"] for r in notes["your_requests"]] == [request.id]


def test_accept_request_pays_and_settles():
    world = make_world()
    outcome, result = call(world, "accept_request", request_id="1003")
    assert outcome.state_changed
    assert result["story"] == "Bodhi Lane charged you"
    assert state(world).transactions[2].status == "settled"
    assert state(world).balance == 20.0
    assert state(world).transactions[2].funding_source == "Chase checking ••4821"
    again, _ = call(world, "accept_request", request_id="1003")
    assert "already settled" in again.error


def test_decline_request_and_errors():
    world = make_world()
    outcome, _ = call(world, "decline_request", request_id="1003")
    assert outcome.state_changed and state(world).transactions[2].status == "declined"
    wrong, _ = call(world, "decline_request", request_id="1002")
    assert "No payment request" in wrong.error


def test_request_payment_errors_and_empty_state():
    world = make_world()
    outcome, _ = call(world, "request_payment", recipient="@jessica-k", amount=5, note="x")
    assert "yourself" in outcome.error
    unknown, _ = call(world, "request_payment", recipient="ghost", amount=5, note="x")
    assert "No Venmo user" in unknown.error
    assert len(state(world).transactions) == 3
    assert Payments().transactions == []
