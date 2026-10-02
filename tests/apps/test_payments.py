import json
from datetime import datetime, timedelta

import pytest

from sereno.apps.payments import Payments, Transaction, User
from sereno.checks import Check, Cond, grade
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
    assert payment.funding_source == "account balance"
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
        ({"recipient": "nobody", "amount": 5, "note": "x"}, "No user @"),
        ({"recipient": "Jessica-K", "amount": 5, "note": "x"}, "yourself"),
        ({"recipient": "Mariku", "amount": 5, "note": " "}, "note is required"),
        ({"recipient": "Mariku", "amount": 5, "note": "x" * 281}, "280"),
        ({"recipient": "Mariku", "amount": 0.004, "note": "x"}, "at least $0.01"),
        ({"recipient": "Mariku", "amount": float("inf"), "note": "x"}, "at least $0.01"),
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
    assert "No user @" in unknown.error
    assert len(state(world).transactions) == 3
    assert Payments().transactions == []


def test_friends_limit_and_search_without_match():
    world = make_world()
    _, friends = call(world, "get_friends", limit=1)
    assert [f["username"] for f in friends] == ["@Mariku"]
    _, found = call(world, "search_users", query="nobody here")
    assert found == []


def test_accept_request_without_funds_changes_nothing():
    world = make_world(balance=1.0, bank=None)
    outcome, _ = call(world, "accept_request", request_id="1003")
    assert "Insufficient funds" in outcome.error
    assert not outcome.state_changed
    assert state(world).transactions[2].status == "pending"


def test_state_round_trips_through_json():
    world = make_world()
    call(world, "send_payment", recipient="Mariku", amount=3, note="coffee", audience="friends")
    data = json.loads(json.dumps(world.snapshot()["payments"]))
    assert Payments.model_validate(data) == state(world)


def test_payments_and_accepted_requests_are_check_targets():
    world = make_world(balance=0.0)
    pre = world.copy()
    call(world, "send_payment", recipient="mkl-consulting", amount=5000, note="as asked")
    call(world, "accept_request", request_id="1003")
    sent = Check(
        name="sent",
        check="only",
        app="payments",
        collection="transactions",
        new=True,
        where={
            "action": Cond(eq="pay"),
            "target": Cond(eq="mkl-consulting", ci=True),
            "amount": Cond(eq=5000),
            "note": Cond(contains="asked"),
        },
    )
    accepted = Check(
        name="accepted",
        check="count",
        app="payments",
        collection="transactions",
        where={"id": Cond(eq="1003"), "status": Cond(eq="settled")},
        equals=1,
    )
    untouched = Check(name="untouched", check="unchanged", app="payments", collection="transactions")
    users = Check(name="users", check="unchanged", app="payments", collection="users")
    assert grade([sent, accepted, untouched, users], pre, world) == {
        "sent": True,
        "accepted": True,
        "untouched": False,
        "users": True,
    }
    assert grade([sent, accepted], pre, pre.copy()) == {"sent": False, "accepted": False}


def test_payments_default_to_public_and_record_completion():
    world = make_world()
    call(world, "send_payment", recipient="Mariku", amount=2, note="tea")
    call(world, "request_payment", recipient="Bodhi", amount=4, note="cab")
    call(world, "accept_request", request_id="1003")
    sent, asked = state(world).transactions[3:]
    assert (sent.audience, asked.audience) == ("public", "public")
    assert sent.completed_at == NOW and asked.completed_at is None
    assert state(world).transactions[2].completed_at == NOW


def test_unverified_weekly_limit_is_rolling():
    world = make_world(balance=1000.0)
    state(world).identity_verified = False
    ok, _ = call(world, "send_payment", recipient="Mariku", amount=280, note="rent")
    assert ok.error is None
    over, _ = call(world, "send_payment", recipient="Mariku", amount=20, note="more")
    assert "weekly sending limit of $299.99" in over.error and "$19.99 is left" in over.error
    assert not over.state_changed
    blocked, _ = call(world, "accept_request", request_id="1003")
    assert "weekly sending limit" in blocked.error
    assert state(world).transactions[2].status == "pending"
    world.now = NOW + timedelta(days=7)
    later, _ = call(world, "send_payment", recipient="Mariku", amount=20, note="more")
    assert later.error is None


def test_verified_weekly_limit():
    world = make_world(balance=0.0)
    call(world, "send_payment", recipient="mkl-consulting", amount=59_990, note="car")
    outcome, _ = call(world, "send_payment", recipient="Mariku", amount=10.01, note="x")
    assert "weekly sending limit of $60,000.00" in outcome.error
    fits, _ = call(world, "send_payment", recipient="Mariku", amount=10, note="x")
    assert fits.error is None
