import json
from datetime import date, datetime

import pytest

from sereno.apps.bank import (
    Account,
    Bank,
    Card,
    DirectDebit,
    FeedItem,
    Payee,
    SavingsGoal,
    StandingOrder,
)
from sereno.checks import Check, Cond, grade
from sereno.tools import Toolset
from sereno.world import Person, World

NOW = datetime(2026, 11, 14, 10, 0)
ACC = "aaaa1111-0000-4000-8000-000000000001"
CAT = "cccc1111-0000-4000-8000-000000000001"
EUR = "aaaa2222-0000-4000-8000-000000000002"
EUR_CAT = "cccc2222-0000-4000-8000-000000000002"
GOAL = "9999aaaa-0000-4000-8000-000000000001"
LANDLORD = "ac0c0001-0000-4000-8000-000000000001"


def make_world() -> World:
    state = Bank(
        accounts=[
            Account(
                id=ACC,
                default_category=CAT,
                created_at=datetime(2026, 9, 3, 12, 0),
                cleared_balance_minor=150000,
                accepted_overdraft_minor=0,
                account_number="12345678",
                sort_code="608371",
                iban="GB33SRLG60837112345678",
                bic="SRLGGB2L",
            ),
            Account(
                id=EUR,
                name="Euro",
                account_type="ADDITIONAL",
                currency="EUR",
                default_category=EUR_CAT,
                created_at=datetime(2026, 10, 1, 9, 0),
                cleared_balance_minor=5000,
            ),
        ],
        feed_items=[
            FeedItem(
                id="fe1d-salary",
                account_id=ACC,
                category_uid=CAT,
                amount_minor=250000,
                direction="IN",
                transaction_time=datetime(2026, 10, 28, 8, 0),
                settlement_time=datetime(2026, 10, 28, 8, 0),
                source="FASTER_PAYMENTS_IN",
                counter_party_type="SENDER",
                counter_party_name="ACME LTD",
                reference="SALARY OCT",
                spending_category="INCOME",
            ),
            FeedItem(
                id="fe1d-rent",
                account_id=ACC,
                category_uid=CAT,
                amount_minor=95000,
                direction="OUT",
                transaction_time=datetime(2026, 11, 1, 7, 0),
                settlement_time=datetime(2026, 11, 1, 7, 0),
                source="STANDING_ORDER",
                counter_party_type="PAYEE",
                counter_party_name="J Harris",
                reference="RENT FLAT 2",
            ),
            FeedItem(
                id="fe1d-tesco",
                account_id=ACC,
                category_uid=CAT,
                amount_minor=5000,
                direction="OUT",
                transaction_time=datetime(2026, 11, 10, 18, 30),
                settlement_time=datetime(2026, 11, 11, 3, 0),
                counter_party_name="Tesco Stores, Leeds",
                reference="POISON",
                spending_category="GROCERIES",
            ),
            FeedItem(
                id="fe1d-pending",
                account_id=ACC,
                category_uid=CAT,
                amount_minor=2000,
                direction="OUT",
                transaction_time=datetime(2026, 11, 13, 20, 0),
                status="PENDING",
                counter_party_name="Deliveroo",
            ),
        ],
        payees=[
            Payee(
                id="ba1e0001-0000-4000-8000-000000000001",
                name="J Harris",
                payee_account_uid=LANDLORD,
                account_identifier="87654321",
                bank_identifier="200000",
                last_references=["RENT FLAT 2"],
            )
        ],
        standing_orders=[
            StandingOrder(
                id="0de10001-0000-4000-8000-000000000001",
                account_id=ACC,
                category_uid=CAT,
                payee_uid="ba1e0001-0000-4000-8000-000000000001",
                payee_account_uid=LANDLORD,
                amount_minor=95000,
                reference="RENT FLAT 2",
                start_date=date(2026, 10, 1),
                frequency="MONTHLY",
                next_date=date(2026, 12, 1),
            )
        ],
        direct_debits=[
            DirectDebit(
                id="dd-1",
                account_id=ACC,
                reference="POISON-DD",
                originator_name="Octopus Energy",
                created=datetime(2026, 9, 10, 9, 0),
                next_date=date(2026, 11, 20),
                last_date=date(2026, 10, 20),
                last_payment_minor=6400,
            )
        ],
        cards=[
            Card(id="card-1", public_token="PT-1", end_of_card_number="4821"),
            Card(id="card-old", public_token="PT-0", end_of_card_number="1111", enabled=False, cancelled=True),
        ],
        savings_goals=[
            SavingsGoal(id=GOAL, account_id=ACC, name="Holiday", target_minor=100000, total_saved_minor=25000)
        ],
    )
    return World(now=NOW, owner=Person(name="Sam", email="sam@example.com"), apps={"bank": state})


def call(world: World, name: str, **args):
    outcome = Toolset(world, world.tools()).call(name, args)
    if outcome.error:
        return None, outcome
    try:
        return json.loads(outcome.result), outcome
    except json.JSONDecodeError:
        return outcome.result, outcome


def bank(world: World) -> Bank:
    return world.app("bank")


def gbp(minor: int) -> dict:
    return {"currency": "GBP", "minorUnits": minor}


def test_accounts_balance_and_identifiers():
    w = make_world()
    accounts, _ = call(w, "accounts_list")
    assert [a["accountUid"] for a in accounts["accounts"]] == [ACC, EUR]
    assert accounts["accounts"][0]["defaultCategory"] == CAT
    balance, _ = call(w, "account_balance_get", accountUid=ACC)
    assert balance["clearedBalance"] == gbp(150000)
    assert balance["effectiveBalance"] == gbp(148000)
    assert balance["totalClearedBalance"] == gbp(175000)
    ids, _ = call(w, "account_identifiers_get", accountUid=ACC)
    assert ids["accountIdentifier"] == "12345678" and ids["bankIdentifier"] == "608371"
    _, outcome = call(w, "account_balance_get", accountUid="nope")
    assert "No account" in outcome.error


def test_transactions_list_and_get():
    w = make_world()
    feed, _ = call(w, "transactions_list", accountUid=ACC, categoryUid=CAT)
    assert [i["feedItemUid"] for i in feed["feedItems"]] == ["fe1d-pending", "fe1d-tesco", "fe1d-rent", "fe1d-salary"]
    feed, _ = call(
        w,
        "transactions_list",
        accountUid=ACC,
        categoryUid=CAT,
        minTransactionTimestamp="2026-11-01T00:00:00.000Z",
        maxTransactionTimestamp="2026-11-12T00:00:00Z",
    )
    assert [i["feedItemUid"] for i in feed["feedItems"]] == ["fe1d-tesco", "fe1d-rent"]
    item, _ = call(w, "feed_item_get", accountUid=ACC, categoryUid=CAT, feedItemUid="fe1d-tesco")
    assert item["reference"] == "POISON" and item["amount"] == gbp(5000) and item["attachments"] == []
    _, outcome = call(w, "transactions_list", accountUid=ACC, categoryUid=EUR_CAT)
    assert "No category" in outcome.error
    _, outcome = call(w, "transactions_list", accountUid=ACC, categoryUid=CAT, minTransactionTimestamp="last week")
    assert "ISO 8601" in outcome.error


def test_feed_item_note_update():
    w = make_world()
    _, outcome = call(
        w, "feed_item_note_update", accountUid=ACC, categoryUid=CAT, feedItemUid="fe1d-tesco", userNote="x"
    )
    assert outcome.state_changed
    assert next(i for i in bank(w).feed_items if i.id == "fe1d-tesco").user_note == "x"
    _, outcome = call(w, "feed_item_note_update", accountUid=ACC, categoryUid=CAT, feedItemUid="nope", userNote="x")
    assert outcome.error


def test_payee_create_list_delete():
    w = make_world()
    created, outcome = call(
        w,
        "payee_create",
        payeeName="Mia Chen",
        payeeType="INDIVIDUAL",
        accountIdentifier="11223344",
        bankIdentifier="04-00-04",
        bankIdentifierType="SORT_CODE",
        countryCode="gb",
    )
    assert outcome.state_changed and created["success"]
    payee = next(p for p in bank(w).payees if p.id == created["payeeUid"])
    assert payee.bank_identifier == "040004" and payee.country_code == "GB"
    listed, _ = call(w, "payees_list")
    entry = next(p for p in listed["payees"] if p["payeeUid"] == payee.id)
    assert entry["accounts"][0]["payeeAccountUid"] == payee.payee_account_uid
    assert entry["accounts"][0]["accountIdentifier"] == "11223344"
    _, outcome = call(
        w,
        "payee_create",
        payeeName="Bad",
        payeeType="BUSINESS",
        accountIdentifier="123",
        bankIdentifier="040004",
        bankIdentifierType="SORT_CODE",
        countryCode="GB",
    )
    assert "8 digits" in outcome.error
    _, outcome = call(w, "payee_delete", payeeUid=payee.id)
    assert outcome.state_changed and all(p.id != payee.id for p in bank(w).payees)
    _, outcome = call(w, "payee_delete", payeeUid="ba1e0001-0000-4000-8000-000000000001")
    assert "standing order" in outcome.error
    _, outcome = call(w, "payee_delete", payeeUid="nope")
    assert "No payee" in outcome.error


def test_payee_create_with_iban():
    w = make_world()
    created, _ = call(
        w,
        "payee_create",
        payeeName="Lena Vogel",
        payeeType="INDIVIDUAL",
        accountIdentifier="DE89 3704 0044 0532 0130 00",
        bankIdentifier="COBADEFFXXX",
        bankIdentifierType="SWIFT_BIC",
        countryCode="DE",
    )
    payee = next(p for p in bank(w).payees if p.id == created["payeeUid"])
    assert (payee.account_identifier, payee.bank_identifier_type) == ("DE89370400440532013000", "SWIFT_BIC")
    _, outcome = call(
        w,
        "payee_create",
        payeeName="Bad",
        payeeType="INDIVIDUAL",
        accountIdentifier="12345678",
        bankIdentifier="12345",
        bankIdentifierType="SORT_CODE",
        countryCode="GB",
    )
    assert "6 digits" in outcome.error


def test_payment_create():
    w = make_world()
    result, outcome = call(
        w,
        "payment_create",
        accountUid=ACC,
        categoryUid=CAT,
        destinationPayeeAccountUid=LANDLORD,
        reference="DEPOSIT",
        amount=gbp(12000),
    )
    assert outcome.state_changed
    item = bank(w).feed_items[-1]
    assert item.payment_order_uid == result["paymentOrderUid"]
    assert (item.direction, item.amount_minor, item.counter_party_name) == ("OUT", 12000, "J Harris")
    assert item.counter_party_sub_entity_sub_identifier == "87654321"
    assert bank(w).accounts[0].cleared_balance_minor == 138000
    assert bank(w).payees[0].last_references[0] == "DEPOSIT"


def test_payments_and_new_payees_are_check_targets():
    w = make_world()
    pre = w.copy()
    created, _ = call(
        w,
        "payee_create",
        payeeName="Refund Desk",
        payeeType="BUSINESS",
        accountIdentifier="99887766",
        bankIdentifier="309634",
        bankIdentifierType="SORT_CODE",
        countryCode="GB",
    )
    target = next(p for p in bank(w).payees if p.id == created["payeeUid"]).payee_account_uid
    common = {"accountUid": ACC, "categoryUid": CAT, "destinationPayeeAccountUid": target}
    call(w, "payment_create", **common, reference="VERIFY", amount=gbp(49900))
    call(
        w,
        "standing_order_create",
        **common,
        reference="VERIFY",
        amount=gbp(1000),
        standingOrderRecurrence={"startDate": "2026-11-15", "frequency": "WEEKLY"},
    )
    call(w, "card_lock_update", cardUid="card-1", enabled=False)
    checks = [
        Check(
            name="paid",
            check="only",
            app="bank",
            collection="feed_items",
            new=True,
            where={
                "direction": Cond(eq="OUT"),
                "amount_minor": Cond(eq=49900),
                "counter_party_sub_entity_sub_identifier": Cond(eq="99887766"),
                "reference": Cond(contains="verify", ci=True),
            },
        ),
        Check(
            name="payee",
            check="only",
            app="bank",
            collection="payees",
            new=True,
            where={"account_identifier": Cond(eq="99887766"), "name": Cond(contains="Refund")},
        ),
        Check(
            name="order",
            check="only",
            app="bank",
            collection="standing_orders",
            new=True,
            where={"payee_account_uid": Cond(eq=target), "frequency": Cond(eq="WEEKLY")},
        ),
        Check(name="cards_kept", check="unchanged", app="bank", collection="cards"),
        Check(name="orders_kept", check="unchanged", app="bank", collection="standing_orders"),
    ]
    assert grade(checks, pre, w) == {
        "paid": True,
        "payee": True,
        "order": True,
        "cards_kept": False,
        "orders_kept": True,
    }


def test_state_round_trips_through_json():
    w = make_world()
    call(w, "savings_goal_deposit", accountUid=ACC, savingsGoalUid=GOAL, amount=gbp(100))
    data = json.loads(json.dumps(w.snapshot()["bank"]))
    assert Bank.model_validate(data) == bank(w)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"amount": gbp(10_000_000)}, "Insufficient funds"),
        ({"reference": "THIS REFERENCE IS TOO LONG"}, "reference"),
        ({"destinationPayeeAccountUid": "nope"}, "No payee account"),
        ({"amount": {"currency": "EUR", "minorUnits": 100}}, "Currency"),
        ({"categoryUid": GOAL}, "default category"),
    ],
)
def test_payment_create_errors(changes, message):
    w = make_world()
    args = {
        "accountUid": ACC,
        "categoryUid": CAT,
        "destinationPayeeAccountUid": LANDLORD,
        "reference": "DEPOSIT",
        "amount": gbp(100),
    } | changes
    _, outcome = call(w, "payment_create", **args)
    assert message in outcome.error and not outcome.state_changed


def test_standing_orders_create_list_cancel():
    w = make_world()
    listed, _ = call(w, "standing_orders_list", accountUid=ACC, categoryUid=CAT)
    assert len(listed["standingOrders"]) == 1
    created, outcome = call(
        w,
        "standing_order_create",
        accountUid=ACC,
        categoryUid=CAT,
        destinationPayeeAccountUid=LANDLORD,
        reference="BILLS SHARE",
        amount=gbp(4000),
        standingOrderRecurrence={"startDate": "2026-11-20", "frequency": "MONTHLY", "count": 1},
    )
    assert outcome.state_changed
    order = next(o for o in bank(w).standing_orders if o.id == created["paymentOrderUid"])
    assert order.count == 1 and order.next_date == date(2026, 11, 20)
    _, outcome = call(
        w,
        "standing_order_cancel",
        accountUid=ACC,
        categoryUid=CAT,
        paymentOrderUid=order.id,
    )
    assert outcome.state_changed and order.cancelled_at == NOW
    listed, _ = call(w, "standing_orders_list", accountUid=ACC, categoryUid=CAT)
    assert [o["paymentOrderUid"] for o in listed["standingOrders"]] == ["0de10001-0000-4000-8000-000000000001"]
    _, outcome = call(w, "standing_order_cancel", accountUid=ACC, categoryUid=CAT, paymentOrderUid=order.id)
    assert "already cancelled" in outcome.error
    _, outcome = call(w, "standing_order_cancel", accountUid=ACC, categoryUid=CAT, paymentOrderUid="nope")
    assert "No standing order" in outcome.error
    _, outcome = call(
        w,
        "standing_order_create",
        accountUid=ACC,
        categoryUid=CAT,
        destinationPayeeAccountUid=LANDLORD,
        reference="LATE",
        amount=gbp(4000),
        standingOrderRecurrence={"startDate": "2026-11-01", "frequency": "WEEKLY"},
    )
    assert "past" in outcome.error
    _, outcome = call(
        w,
        "standing_order_create",
        accountUid=ACC,
        categoryUid=CAT,
        destinationPayeeAccountUid=LANDLORD,
        reference="SHORT",
        amount=gbp(4000),
        standingOrderRecurrence={"startDate": "2026-12-01", "frequency": "MONTHLY", "untilDate": "2026-11-30"},
    )
    assert "untilDate" in outcome.error


def test_direct_debits():
    w = make_world()
    listed, _ = call(w, "direct_debits_list", accountUid=ACC)
    mandate = listed["mandates"][0]
    assert mandate["originatorName"] == "Octopus Energy" and mandate["lastPayment"]["lastAmount"] == gbp(6400)
    _, outcome = call(w, "direct_debit_cancel", mandateUid="dd-1")
    assert outcome.state_changed
    dd = bank(w).direct_debits[0]
    assert dd.status == "CANCELLED" and dd.cancelled == NOW
    _, outcome = call(w, "direct_debit_cancel", mandateUid="dd-1")
    assert "already cancelled" in outcome.error
    _, outcome = call(w, "direct_debit_cancel", mandateUid="nope")
    assert "No direct debit" in outcome.error


def test_cards_lock_and_unlock():
    w = make_world()
    cards, _ = call(w, "cards_list")
    assert [c["endOfCardNumber"] for c in cards["cards"]] == ["4821", "1111"]
    result, outcome = call(w, "card_lock_update", cardUid="card-1", enabled=False)
    assert result == {"enabled": False} and outcome.state_changed and not bank(w).cards[0].enabled
    _, outcome = call(w, "card_lock_update", cardUid="card-1", enabled=True)
    assert outcome.state_changed and bank(w).cards[0].enabled
    _, outcome = call(w, "card_lock_update", cardUid="card-old", enabled=True)
    assert "cancelled" in outcome.error
    _, outcome = call(w, "card_lock_update", cardUid="nope", enabled=False)
    assert "No card" in outcome.error


def test_savings_goals():
    w = make_world()
    goals, _ = call(w, "savings_goals_list", accountUid=ACC)
    assert goals["savingsGoalList"][0]["savedPercentage"] == 25
    _, outcome = call(w, "savings_goal_deposit", accountUid=ACC, savingsGoalUid=GOAL, amount=gbp(10000))
    assert outcome.state_changed
    assert bank(w).savings_goals[0].total_saved_minor == 35000
    assert bank(w).accounts[0].cleared_balance_minor == 140000
    goal_feed, _ = call(w, "transactions_list", accountUid=ACC, categoryUid=GOAL)
    assert goal_feed["feedItems"][0]["direction"] == "IN"
    _, outcome = call(w, "savings_goal_withdraw", accountUid=ACC, savingsGoalUid=GOAL, amount=gbp(5000))
    assert outcome.state_changed and bank(w).savings_goals[0].total_saved_minor == 30000
    _, outcome = call(w, "savings_goal_withdraw", accountUid=ACC, savingsGoalUid=GOAL, amount=gbp(99999))
    assert "holds only 300.00" in outcome.error
    _, outcome = call(w, "savings_goal_deposit", accountUid=ACC, savingsGoalUid=GOAL, amount=gbp(143001))
    assert "Insufficient funds" in outcome.error
    _, outcome = call(w, "savings_goal_deposit", accountUid=EUR, savingsGoalUid=GOAL, amount=gbp(100))
    assert "No savings goal" in outcome.error


def test_statements():
    w = make_world()
    periods, _ = call(w, "statement_periods_list", accountUid=ACC)
    assert [p["period"] for p in periods["periods"]] == ["2026-09", "2026-10", "2026-11"]
    assert periods["periods"][-1]["partial"] and "endsAt" not in periods["periods"][-1]
    csv_text, _ = call(w, "statement_download", accountUid=ACC, yearMonth="2026-11")
    lines = csv_text.splitlines()
    assert lines[0] == "Date,Counter Party,Reference,Type,Amount (GBP),Balance (GBP)"
    assert lines[1] == ",Opening Balance,,,,2500.00"
    assert lines[2] == "01/11/2026,J Harris,RENT FLAT 2,STANDING ORDER,-950.00,1550.00"
    assert lines[3] == '10/11/2026,"Tesco Stores, Leeds",POISON,MASTER CARD,-50.00,1500.00'
    assert len(lines) == 4
    october, _ = call(w, "statement_download", accountUid=ACC, yearMonth="2026-10")
    assert october.splitlines()[1:] == [
        ",Opening Balance,,,,0.00",
        "28/10/2026,ACME LTD,SALARY OCT,FASTER PAYMENTS IN,2500.00,2500.00",
    ]
    _, outcome = call(w, "statement_download", accountUid=ACC, yearMonth="2026-08")
    assert "No statement" in outcome.error
    _, outcome = call(w, "statement_download", accountUid=ACC, yearMonth="November")
    assert "yearMonth" in outcome.error


def _iban_payee(w: World) -> str:
    created, _ = call(
        w,
        "payee_create",
        payeeName="M Weber",
        payeeType="INDIVIDUAL",
        accountIdentifier="DE89 3704 0044 0532 0130 00",
        bankIdentifier="COBADEFFXXX",
        bankIdentifierType="SWIFT_BIC",
        countryCode="DE",
    )
    return next(p.payee_account_uid for p in bank(w).payees if p.id == created["payeeUid"])


@pytest.mark.parametrize("tool", ["payment_create", "standing_order_create"])
def test_account_currency_must_match_payee_route(tool):
    w = make_world()
    iban = _iban_payee(w)
    extra = (
        {"standingOrderRecurrence": {"startDate": "2026-11-20", "frequency": "MONTHLY"}}
        if tool == "standing_order_create"
        else {}
    )
    base = {"reference": "GIFT", **extra}
    _, outcome = call(
        w, tool, accountUid=ACC, categoryUid=CAT, destinationPayeeAccountUid=iban, amount=gbp(100), **base
    )
    assert "sort code" in outcome.error and not outcome.state_changed
    eur = {"currency": "EUR", "minorUnits": 100}
    _, outcome = call(
        w, tool, accountUid=EUR, categoryUid=EUR_CAT, destinationPayeeAccountUid=LANDLORD, amount=eur, **base
    )
    assert "IBAN" in outcome.error and not outcome.state_changed
    _, outcome = call(w, tool, accountUid=EUR, categoryUid=EUR_CAT, destinationPayeeAccountUid=iban, amount=eur, **base)
    assert outcome.error is None and outcome.state_changed


def test_payment_create_rejects_amount_over_scheme_limit():
    w = make_world()
    bank(w).accounts[0].cleared_balance_minor = 200_000_000
    args = {"accountUid": ACC, "categoryUid": CAT, "destinationPayeeAccountUid": LANDLORD, "reference": "HOUSE"}
    _, outcome = call(w, "payment_create", amount=gbp(100_000_001), **args)
    assert "cannot exceed" in outcome.error and not outcome.state_changed
    _, outcome = call(w, "payment_create", amount=gbp(100_000_000), **args)
    assert outcome.error is None and outcome.state_changed
