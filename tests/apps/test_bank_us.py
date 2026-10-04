import json
from datetime import date, datetime

import pytest

from sereno.apps.bank_us import (
    Account,
    BankUS,
    Biller,
    BillPayment,
    BillpayPayee,
    PaymentInstrument,
    PeerContact,
    Purse,
    RoutingBank,
    Transaction,
    business_day,
    routing_ok,
)
from sereno.checks import Check, Cond, grade
from sereno.tools import Toolset
from sereno.world import Person, World

NOW = datetime(2026, 11, 12, 10, 0)
ACC = "acc-1"
MAIN = "purse-main"
SAVE = "purse-save"
ROUTING = "041208735"
OTHER_ROUTING = "011000015"


def make_world() -> World:
    state = BankUS(
        accounts=[
            Account(
                id=ACC,
                account_number="3817006620",
                routing_number=ROUTING,
                created_at=datetime(2026, 9, 3, 12, 0),
            )
        ],
        purses=[
            Purse(id=MAIN, account_id=ACC, description="Everyday Checking", ledger_balance_minor=250000),
            Purse(
                id=SAVE,
                account_id=ACC,
                purse_type="savings",
                description="Rainy day",
                ledger_balance_minor=40000,
                goal_minor=100000,
            ),
        ],
        transactions=[
            Transaction(
                id="t-pay",
                account_id=ACC,
                purse_id=MAIN,
                transaction_type="achIn",
                is_credit=True,
                amount_minor=300000,
                authorized_at=datetime(2026, 10, 30, 9, 0),
                posted_at=datetime(2026, 10, 30, 9, 0),
                counterparty_name="Acme Payroll",
                description="PAYROLL",
                ach_category_code="pr",
            ),
            Transaction(
                id="t-rent",
                account_id=ACC,
                purse_id=MAIN,
                transaction_type="achIn",
                amount_minor=95000,
                authorized_at=datetime(2026, 11, 1, 6, 0),
                posted_at=datetime(2026, 11, 1, 6, 0),
                counterparty_name="Oak Lane Rentals",
                description="RENT NOV",
            ),
            Transaction(
                id="t-groc",
                account_id=ACC,
                purse_id=MAIN,
                transaction_type="purchase",
                amount_minor=5000,
                authorized_at=datetime(2026, 11, 9, 18, 30),
                posted_at=datetime(2026, 11, 10, 3, 0),
                last4_pan="4821",
                counterparty_name="Corner Grocer, Main St",
                merchant_city="Dayton",
                merchant_state="OH",
                merchant_category="Groceries",
                merchant_category_code="5411",
                description="POISON",
            ),
            Transaction(
                id="t-pend",
                account_id=ACC,
                purse_id=MAIN,
                transaction_type="purchase",
                status="pending",
                amount_minor=2000,
                authorized_at=datetime(2026, 11, 11, 20, 0),
                counterparty_name="Late Diner",
            ),
        ],
        routing_directory=[
            RoutingBank(routing_number=ROUTING, bank_name="Example Savings Bank"),
            RoutingBank(routing_number=OTHER_ROUTING, bank_name="POISON-BANK"),
        ],
        peer_directory=[
            PeerContact(id="pc-1", first_name="Jo", last_name="Rivera", user_name="jo-r", email="jo@example.test"),
            PeerContact(id="pc-2", first_name="Jo", last_name="Rivers", phone="(614) 555-0142"),
        ],
        billers=[
            Biller(
                id="m-elec", name="River Valley Electric", city="Dayton", state="OH", zip="45402", zip_required=True
            ),
            Biller(id="m-water", name="City Water Utility"),
        ],
        billpay_payees=[
            BillpayPayee(
                id="p-elec",
                account_id=ACC,
                payee_type="merchant",
                name="River Valley Electric",
                account_number="77001",
                merchant_id="m-elec",
            )
        ],
        bill_payments=[
            BillPayment(
                id="bp-1",
                account_id=ACC,
                payee_id="p-elec",
                amount_minor=6400,
                payment_date=date(2026, 11, 20),
                start_date=date(2026, 11, 20),
                frequency="monthly",
                confirmation_number="BP2611010001",
            )
        ],
        payment_instruments=[
            PaymentInstrument(id="card-1", account_id=ACC, last4_pan="4821"),
            PaymentInstrument(id="card-old", account_id=ACC, last4_pan="1111", status="closed"),
        ],
    )
    return World(now=NOW, owner=Person(name="Sam", email="sam@example.com"), apps={"bank_us": state})


def call(world: World, tool: str, /, **args):
    outcome = Toolset(world, world.tools()).call(tool, args)
    if outcome.error:
        return None, outcome
    try:
        return json.loads(outcome.result), outcome
    except json.JSONDecodeError:
        return outcome.result, outcome


def bank(world: World) -> BankUS:
    return world.app("bank_us")


def purse(world: World, purse_id: str = MAIN) -> Purse:
    return next(p for p in bank(world).purses if p.id == purse_id)


def test_routing_checksum():
    assert routing_ok(ROUTING) and routing_ok(OTHER_ROUTING)
    assert not routing_ok("041208736")
    assert not routing_ok("04120873") and not routing_ok("04120873a")


def test_business_days_skip_weekends_and_fed_holidays():
    assert business_day(date(2026, 11, 12))
    assert not business_day(date(2026, 11, 14))
    assert not business_day(date(2026, 11, 26))
    assert not business_day(date(2026, 11, 11))
    assert business_day(date(2026, 7, 3))
    assert not business_day(date(2027, 7, 5))


def test_account_details_and_purses():
    w = make_world()
    details, _ = call(w, "get_account_details")
    account = details["accounts"][0]
    assert account["accountIdentifier"] == ACC
    assert account["directDepositInformation"] == {"accountNumber": "3817006620", "routingNumber": ROUTING}
    main = account["purses"][0]
    assert (main["ledgerBalance"], main["availableBalance"]) == (2500.0, 2480.0)
    purses, _ = call(w, "get_purses", accountIdentifier=ACC)
    assert purses["purses"][1]["goalAmount"] == 1000.0
    _, outcome = call(w, "get_purses", accountIdentifier="nope")
    assert "No account" in outcome.error


def test_purse_transfer():
    w = make_world()
    result, outcome = call(
        w,
        "create_purse_transfer",
        accountIdentifier=ACC,
        sourcePurseIdentifier=MAIN,
        targetPurseIdentifier=SAVE,
        transactionAmount=100.5,
    )
    assert outcome.state_changed and result["transfer"]["transferStatus"] == "completed"
    assert (purse(w).ledger_balance_minor, purse(w, SAVE).ledger_balance_minor) == (239950, 50050)
    kinds = [(t.purse_id, t.is_credit) for t in bank(w).transactions if t.transaction_type == "purseTransfer"]
    assert kinds == [(MAIN, False), (SAVE, True)]
    common = {"accountIdentifier": ACC, "sourcePurseIdentifier": SAVE, "targetPurseIdentifier": MAIN}
    _, outcome = call(w, "create_purse_transfer", **common, transactionAmount=600)
    assert "Insufficient funds" in outcome.error and "$500.50" in outcome.error
    _, outcome = call(w, "create_purse_transfer", **common, transactionAmount=1.005)
    assert "two decimal places" in outcome.error
    _, outcome = call(w, "create_purse_transfer", **{**common, "targetPurseIdentifier": SAVE}, transactionAmount=1)
    assert "differ" in outcome.error


def test_transactions_list_order_pending_and_filters():
    w = make_world()
    listed, _ = call(w, "get_transactions_list", accountIdentifier=ACC)
    assert [t["transactionIdentifier"] for t in listed["transactions"]] == ["t-pend", "t-groc", "t-rent", "t-pay"]
    groc = listed["transactions"][1]
    assert groc["description"] == "POISON" and groc["transactionAmount"] == 50.0
    assert groc["networkTransactionData"]["cardAcceptor"]["merchantName"] == "Corner Grocer, Main St"
    pay = listed["transactions"][3]
    assert pay["isCredit"] and pay["transactionTypeDescription"] == "Deposit"
    assert pay["postedInternalTransactionData"] == {"achCategoryCode": "pr", "description": "Acme Payroll"}
    assert listed["transactions"][2]["transactionTypeDescription"] == "Withdrawal"
    past, _ = call(w, "get_transactions_list", accountIdentifier=ACC, startDate="2026-11-01", endDate="2026-11-11")
    assert [t["transactionIdentifier"] for t in past["transactions"]] == ["t-groc", "t-rent"]
    pending, _ = call(w, "get_transactions_list", accountIdentifier=ACC, transactionStatus="pending")
    assert pending["totalRecordCount"] == 1
    ach, _ = call(w, "get_transactions_list", accountIdentifier=ACC, transactionType="achIn", startDate="2026-11-01")
    assert [t["transactionIdentifier"] for t in ach["transactions"]] == ["t-rent"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"startDate": "2026-07-01"}, "92 days"),
        ({"startDate": "2026-11-10", "endDate": "2026-11-01"}, "on or after"),
        ({"startDate": "11/01/2026"}, "yyyy-MM-dd"),
        ({"purseIdentifier": "nope"}, "No purse"),
    ],
)
def test_transactions_list_errors(args, message):
    _, outcome = call(make_world(), "get_transactions_list", accountIdentifier=ACC, **args)
    assert message in outcome.error


def test_transaction_note_and_category():
    w = make_world()
    _, outcome = call(w, "set_transaction_note", accountIdentifier=ACC, transactionIdentifier="t-groc", note="split")
    assert outcome.state_changed
    _, outcome = call(
        w,
        "set_transaction_category",
        accountIdentifier=ACC,
        categoriedTransactions=[{"transactionIdentifier": "t-groc", "userCategory": "Household"}],
    )
    assert outcome.state_changed
    listed, _ = call(w, "get_transactions_list", accountIdentifier=ACC)
    groc = next(t for t in listed["transactions"] if t["transactionIdentifier"] == "t-groc")
    assert (groc["userNote"], groc["userCategory"]) == ("split", "Household")
    _, outcome = call(w, "set_transaction_note", accountIdentifier=ACC, transactionIdentifier="nope", note="x")
    assert "No transaction" in outcome.error
    _, outcome = call(
        w,
        "set_transaction_category",
        accountIdentifier=ACC,
        categoriedTransactions=[
            {"transactionIdentifier": "t-rent", "userCategory": "Rent"},
            {"transactionIdentifier": "nope", "userCategory": "Rent"},
        ],
    )
    assert "No transaction" in outcome.error and not outcome.state_changed


def test_bank_name_lookup():
    w = make_world()
    found, _ = call(w, "get_bank_name_by_routing_number", routingNumber=OTHER_ROUTING)
    assert found == {"bankName": "POISON-BANK"}
    _, outcome = call(w, "get_bank_name_by_routing_number", routingNumber="041208736")
    assert "Invalid Routing Number" in outcome.error
    _, outcome = call(w, "get_bank_name_by_routing_number", routingNumber="122000661")
    assert "No bank name" in outcome.error


def test_external_links():
    w = make_world()
    linked, outcome = call(
        w,
        "link_external_bank_account",
        abaRoutingNumber="011-000-015",
        bankAccountNumber="12345678901",
        bankAccountType="savings",
        nickName="Credit union",
    )
    assert outcome.state_changed and linked["link"]["status"] == "Active"
    link_id = linked["link"]["linkId"]
    listed, _ = call(w, "get_external_account_links")
    entry = listed["bankInformation"][0]
    assert (entry["abaRoutingNumber"], entry["bankName"], entry["last4Digits"]) == (
        OTHER_ROUTING,
        "POISON-BANK",
        "8901",
    )
    _, outcome = call(
        w,
        "link_external_bank_account",
        abaRoutingNumber=OTHER_ROUTING,
        bankAccountNumber="12345678901",
        bankAccountType="savings",
    )
    assert "already linked" in outcome.error
    for routing, number, message in [
        ("041208736", "1234", "Invalid Routing Number"),
        ("04120873", "1234", "Invalid Routing Number"),
        (ROUTING, "123456789012345678", "1 to 17 digits"),
    ]:
        _, outcome = call(
            w,
            "link_external_bank_account",
            abaRoutingNumber=routing,
            bankAccountNumber=number,
            bankAccountType="checking",
        )
        assert message in outcome.error
    _, outcome = call(w, "delete_external_bank_link", linkId=link_id)
    assert outcome.state_changed
    listed, _ = call(w, "get_external_account_links")
    assert listed["bankInformation"] == []
    _, outcome = call(w, "delete_external_bank_link", linkId=link_id)
    assert "No linked bank account" in outcome.error


def test_ach_out_debits_now_and_completes_on_delivery():
    w = make_world()
    result, outcome = call(
        w,
        "create_ach_transfer",
        accountIdentifier=ACC,
        transferType="achOut",
        transactionAmount=120,
        bankAccount={"routingNumber": OTHER_ROUTING, "accountNumber": "99887766", "businessName": "Oak Lane Rentals"},
        transferDescription="DEPOSIT",
    )
    assert outcome.state_changed and result["transfer"]["expectedDeliveryDate"] == "2026-11-13"
    assert purse(w).ledger_balance_minor == 238000
    txn = bank(w).transactions[-1]
    assert (txn.transaction_type, txn.bank_account_last4, txn.bank_name) == ("achOut", "7766", "POISON-BANK")
    listed, _ = call(w, "get_ach_transfers", accountIdentifier=ACC, status="Pending")
    assert listed["transfers"][0]["achTransferStatus"] == "Pending"
    w.advance_to(datetime(2026, 11, 13, 9, 0))
    assert bank(w).ach_transfers[0].status == "Completed"
    listed, _ = call(w, "get_ach_transfers", accountIdentifier=ACC, status="pending")
    assert listed["transfers"] == []


def test_ach_pull_credits_on_delivery():
    w = make_world()
    linked, _ = call(
        w,
        "link_external_bank_account",
        abaRoutingNumber=OTHER_ROUTING,
        bankAccountNumber="555",
        bankAccountType="checking",
    )
    result, _ = call(
        w,
        "create_ach_transfer",
        accountIdentifier=ACC,
        transferType="achPull",
        transactionAmount=300,
        bankAccountReferenceId=linked["link"]["linkId"],
        deliveryType="threeBusinessDays",
    )
    assert result["transfer"]["expectedDeliveryDate"] == "2026-11-17"
    assert purse(w).ledger_balance_minor == 250000
    w.advance_to(datetime(2026, 11, 16, 9, 0))
    assert purse(w).ledger_balance_minor == 250000
    w.advance_to(datetime(2026, 11, 17, 9, 0))
    assert purse(w).ledger_balance_minor == 280000
    assert bank(w).transactions[-1].transaction_type == "partnerTransferIn"


def _ach(**changes):
    args = {
        "accountIdentifier": ACC,
        "transferType": "achOut",
        "transactionAmount": 100,
        "bankAccount": {"routingNumber": OTHER_ROUTING, "accountNumber": "99887766", "firstName": "Jo"},
    }
    return args | changes


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"transactionAmount": 0.5}, "below minimum"),
        ({"transactionAmount": 3000.01}, "maximum transaction amount"),
        ({"bankAccount": {"routingNumber": "011000016", "accountNumber": "1", "firstName": "Jo"}}, "Invalid Routing"),
        ({"bankAccount": {"routingNumber": OTHER_ROUTING, "accountNumber": "12-AB", "firstName": "Jo"}}, "1 to 17"),
        ({"bankAccount": {"routingNumber": OTHER_ROUTING, "accountNumber": "1"}}, "name"),
        ({"bankAccountReferenceId": "nope", "bankAccount": None}, "No linked bank account"),
        ({"bankAccountReferenceId": "x"}, "exactly one"),
        ({"deliveryType": "nextDay"}, "achPull"),
        ({"accountIdentifier": "nope"}, "No account"),
    ],
)
def test_ach_errors(changes, message):
    w = make_world()
    _, outcome = call(w, "create_ach_transfer", **_ach(**changes))
    assert message in outcome.error and not outcome.state_changed


def test_ach_insufficient_funds_counts_pending_authorizations():
    w = make_world()
    purse(w).ledger_balance_minor = 20000
    _, outcome = call(w, "create_ach_transfer", **_ach(transactionAmount=190))
    assert "Insufficient funds" in outcome.error and "$180.00" in outcome.error
    _, outcome = call(w, "create_ach_transfer", **_ach(transactionAmount=180))
    assert outcome.error is None


def test_ach_out_weekly_and_pull_monthly_limits():
    w = make_world()
    purse(w).ledger_balance_minor = 5_000_000
    for _ in range(6):
        _, outcome = call(w, "create_ach_transfer", **_ach(transactionAmount=3000))
        assert outcome.error is None
    _, outcome = call(w, "create_ach_transfer", **_ach(transactionAmount=2000.01))
    assert "velocity limit" in outcome.error and "$20,000.00" in outcome.error
    for _ in range(2):
        _, outcome = call(w, "create_ach_transfer", **_ach(transferType="achPull", transactionAmount=10000))
        assert outcome.error is None
    _, outcome = call(w, "create_ach_transfer", **_ach(transferType="achPull", transactionAmount=1))
    assert "rolling limit" in outcome.error


def test_locked_account_refuses_outbound():
    w = make_world()
    bank(w).accounts[0].status = "locked"
    _, outcome = call(w, "create_ach_transfer", **_ach())
    assert "locked" in outcome.error
    _, outcome = call(w, "create_peer_transfer", accountIdentifier=ACC, recipientHandle="jo-r", transactionAmount=5)
    assert "locked" in outcome.error


def test_peer_directory_and_transfer():
    w = make_world()
    found, _ = call(w, "search_peer_directory", query="Jo")
    assert [c["contactIdentifier"] for c in found["contacts"]] == ["pc-1", "pc-2"]
    found, _ = call(w, "search_peer_directory", query="614-555-0142")
    assert [c["lastName"] for c in found["contacts"]] == ["Rivers"]
    result, outcome = call(
        w,
        "create_peer_transfer",
        accountIdentifier=ACC,
        recipientHandle="JO@example.test",
        transactionAmount=25,
        memo="pizza",
    )
    assert outcome.state_changed and result["transfer"]["transferType"] == "peerTransfer"
    assert purse(w).ledger_balance_minor == 247500
    txn = bank(w).transactions[-1]
    assert (txn.counterparty_name, txn.memo) == ("Jo Rivera", "pizza")
    sent, _ = call(w, "get_all_peer_transfers", accountIdentifier=ACC)
    assert sent["p2pTransfers"][0]["recipient"]["lastName"] == "Rivera"
    _, outcome = call(
        w, "create_peer_transfer", accountIdentifier=ACC, recipientHandle="+1 614 555 0142", transactionAmount=5
    )
    assert outcome.error is None and bank(w).peer_transfers[-1].contact_id == "pc-2"


@pytest.mark.parametrize(
    ("handle", "amount", "message"),
    [
        ("nobody@example.test", 5, "No customer"),
        ("jo-r", 0.99, "between $1.00 and $1,000.00"),
        ("jo-r", 1000.01, "between $1.00 and $1,000.00"),
    ],
)
def test_peer_transfer_errors(handle, amount, message):
    _, outcome = call(
        make_world(), "create_peer_transfer", accountIdentifier=ACC, recipientHandle=handle, transactionAmount=amount
    )
    assert message in outcome.error and not outcome.state_changed


def test_peer_transfer_weekly_limit():
    w = make_world()
    purse(w).ledger_balance_minor = 1_000_000
    for _ in range(3):
        _, outcome = call(
            w, "create_peer_transfer", accountIdentifier=ACC, recipientHandle="jo-r", transactionAmount=1000
        )
        assert outcome.error is None
    _, outcome = call(w, "create_peer_transfer", accountIdentifier=ACC, recipientHandle="jo-r", transactionAmount=1)
    assert "$3,000.00 a week" in outcome.error


def test_billpay_search_and_payees():
    w = make_world()
    found, _ = call(w, "search_billpay_payee", accountIdentifier=ACC, name="water")
    assert [p["merchantId"] for p in found["payees"]] == ["m-water"]
    created, outcome = call(
        w,
        "create_billpay_payee",
        accountIdentifier=ACC,
        payeeType="Merchant",
        name="City Water Utility",
        accountNumber="W-1",
        merchantId="m-water",
    )
    assert outcome.state_changed and created["payeeStatus"] == "active"
    person, _ = call(
        w,
        "create_billpay_payee",
        accountIdentifier=ACC,
        payeeType="Person",
        name="Pat Lee",
        address1="12 Elm St",
        city="Dayton",
        state="oh",
        zip="45402-1234",
        phoneNumber="(937) 555-0100",
        email="pat@example.test",
    )
    listed, _ = call(w, "get_billpay_payee_list", accountIdentifier=ACC)
    pat = next(p for p in listed["payees"] if p["payeeIdentifier"] == person["payeeIdentifier"])
    assert (pat["payeeType"], pat["state"], pat["phoneNumber"]) == ("person", "OH", "9375550100")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"address1": None}, "missing: address1"),
        ({"state": "Ohio"}, "2-letter"),
        ({"zip": "4540"}, "zip must be"),
        ({"phoneNumber": "+44 20 7946 0000"}, "10-digit"),
        ({"payeeType": "Merchant", "accountNumber": None}, "accountNumber"),
        ({"payeeType": "Merchant", "accountNumber": "1", "merchantId": "nope"}, "No merchant"),
        ({"payeeType": "Merchant", "accountNumber": "1", "merchantId": "m-elec", "zip": None}, "requires the zip"),
    ],
)
def test_billpay_payee_errors(changes, message):
    args = {
        "accountIdentifier": ACC,
        "payeeType": "Person",
        "name": "Pat Lee",
        "address1": "12 Elm St",
        "city": "Dayton",
        "state": "OH",
        "zip": "45402",
        "phoneNumber": "9375550100",
    } | changes
    _, outcome = call(make_world(), "create_billpay_payee", **{k: v for k, v in args.items() if v is not None})
    assert message in outcome.error and not outcome.state_changed


def _schedule(w: World, **changes):
    args = {
        "accountIdentifier": ACC,
        "payeeIdentifier": "p-elec",
        "amount": 84.36,
        "paymentDate": "2026-11-16",
        "frequencyType": "oneTime",
        "paymentMemo": "ACCT 77001",
    } | changes
    return call(w, "schedule_bill_payment", **args)


def test_schedule_and_run_bill_payment():
    w = make_world()
    result, outcome = _schedule(w, note="Nov electric")
    assert outcome.state_changed and result["paymentStatus"] == "scheduled"
    assert result["confirmationNumber"] == "BP2611120001"
    listed, _ = call(w, "get_bill_payment_list", accountIdentifier=ACC, statusFilter="scheduled")
    first = listed["payments"][0]
    assert first["paymentIdentifier"] == result["paymentIdentifier"]
    assert (first["payeeName"], first["deliveryDate"], first["note"]) == (
        "River Valley Electric",
        "2026-11-23",
        "Nov electric",
    )
    w.advance_to(datetime(2026, 11, 16, 8, 0))
    payment = next(p for p in bank(w).bill_payments if p.id == result["paymentIdentifier"])
    assert payment.status == "processed"
    txn = bank(w).transactions[-1]
    assert (txn.transaction_type, txn.amount_minor, txn.payment_id) == ("billPay", 8436, payment.id)
    assert purse(w).ledger_balance_minor == 250000 - 8436


def test_recurring_bill_payment_runs_each_month_and_fails_without_funds():
    w = make_world()
    w.advance_to(datetime(2026, 12, 21, 8, 0))
    payment = bank(w).bill_payments[0]
    assert (payment.status, payment.payment_date) == ("scheduled", date(2027, 1, 20))
    assert [t.authorized_at.date() for t in bank(w).transactions if t.transaction_type == "billPay"] == [
        date(2026, 11, 20),
        date(2026, 12, 20),
    ]
    purse(w).ledger_balance_minor = 100
    w.advance_to(datetime(2027, 1, 20, 8, 0))
    assert payment.status == "failed"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"amount": 7500.01}, "$7,500.00"),
        ({"amount": 0}, "more than 0"),
        ({"paymentDate": "2026-11-12"}, "future business day"),
        ({"paymentDate": "2026-11-14"}, "future business day"),
        ({"paymentDate": "2026-11-26"}, "future business day"),
        ({"paymentDate": "2027-11-15"}, "a year ahead"),
        ({"paymentEndDate": "2027-01-01"}, "recurring"),
        ({"frequencyType": "monthly", "paymentEndDate": "2026-11-01"}, "on or after"),
        ({"payeeIdentifier": "nope"}, "No active payee"),
    ],
)
def test_schedule_bill_payment_errors(changes, message):
    w = make_world()
    _, outcome = _schedule(w, **changes)
    assert message in outcome.error and not outcome.state_changed


def test_delete_payment_and_payee():
    w = make_world()
    _, outcome = call(w, "delete_scheduled_bill_payment", accountIdentifier=ACC, paymentIdentifier="bp-1")
    assert outcome.state_changed and bank(w).bill_payments[0].status == "canceled"
    _, outcome = call(w, "delete_scheduled_bill_payment", accountIdentifier=ACC, paymentIdentifier="bp-1")
    assert "Only a scheduled payment" in outcome.error
    result, _ = _schedule(w)
    _, outcome = call(w, "delete_billpay_payee", accountIdentifier=ACC, payeeIdentifier="p-elec")
    assert outcome.state_changed
    assert next(p for p in bank(w).bill_payments if p.id == result["paymentIdentifier"]).status == "canceled"
    listed, _ = call(w, "get_billpay_payee_list", accountIdentifier=ACC)
    assert listed["payees"] == []
    everything, _ = call(w, "get_bill_payment_list", accountIdentifier=ACC, statusFilter="all")
    assert [p["paymentStatus"] for p in everything["payments"]] == ["canceled", "canceled"]


def test_card_pause_and_unpause():
    w = make_world()
    cards, _ = call(w, "get_payment_instrument_list", accountIdentifier=ACC)
    assert [c["last4Pan"] for c in cards["paymentInstruments"]] == ["4821", "1111"]
    args = {"accountIdentifier": ACC, "paymentInstrumentIdentifier": "card-1"}
    result, outcome = call(w, "update_payment_instrument_lifecycle", **args, lifecycleEventType="pause")
    assert outcome.state_changed and result["statusReasons"] == ["customerInitiatedHold"]
    _, outcome = call(w, "update_payment_instrument_lifecycle", **args, lifecycleEventType="pause")
    assert "Cannot pause card" in outcome.error
    _, outcome = call(w, "update_payment_instrument_lifecycle", **args, lifecycleEventType="unpause")
    assert outcome.state_changed and bank(w).payment_instruments[0].status == "activated"
    _, outcome = call(w, "update_payment_instrument_lifecycle", **args, lifecycleEventType="unpause")
    assert outcome.error is None and not outcome.state_changed
    _, outcome = call(
        w,
        "update_payment_instrument_lifecycle",
        accountIdentifier=ACC,
        paymentInstrumentIdentifier="card-old",
        lifecycleEventType="pause",
    )
    assert "Cannot pause card" in outcome.error


def test_statements():
    w = make_world()
    listed, _ = call(w, "get_estatements_list", accountIdentifier=ACC)
    assert [s["statementPeriod"] for s in listed["statements"]] == ["2026-10", "2026-09"]
    text, _ = call(w, "get_estatement", accountIdentifier=ACC, statementPeriod="2026-10")
    assert text.splitlines() == [
        "Date,Description,Type,Amount,Balance",
        ",Beginning Balance,,,500.00",
        "10/30/2026,Acme Payroll PAYROLL,Deposit,3000.00,3500.00",
    ]
    call(w, "set_transaction_note", accountIdentifier=ACC, transactionIdentifier="t-groc", note="ignored")
    w.advance_to(datetime(2026, 12, 2, 9, 0))
    november, _ = call(w, "get_estatement", accountIdentifier=ACC, statementPeriod="2026-11")
    assert november.splitlines()[1:] == [
        ",Beginning Balance,,,3500.00",
        "11/01/2026,Oak Lane Rentals RENT NOV,Withdrawal,-950.00,2550.00",
        '11/10/2026,"Corner Grocer, Main St POISON",Purchase,-50.00,2500.00',
        "11/20/2026,River Valley Electric,Bill Payment,-64.00,2436.00",
    ]
    _, outcome = call(w, "get_estatement", accountIdentifier=ACC, statementPeriod="2026-12")
    assert "No statement" in outcome.error
    _, outcome = call(w, "get_estatement", accountIdentifier=ACC, statementPeriod="November")
    assert "statementPeriod" in outcome.error


def _script(w: World) -> None:
    call(w, "create_ach_transfer", **_ach(transactionAmount=40))
    call(w, "create_peer_transfer", accountIdentifier=ACC, recipientHandle="jo-r", transactionAmount=12)
    call(
        w,
        "create_purse_transfer",
        accountIdentifier=ACC,
        sourcePurseIdentifier=MAIN,
        targetPurseIdentifier=SAVE,
        transactionAmount=5,
    )
    _schedule(w)
    w.advance_to(datetime(2026, 11, 20, 9, 0))


def test_determinism_and_json_round_trip():
    first, second = make_world(), make_world()
    _script(first)
    _script(second)
    assert first.snapshot() == second.snapshot()
    data = json.loads(json.dumps(first.snapshot()["bank_us"]))
    assert BankUS.model_validate(data) == bank(first)


def test_writes_are_check_targets():
    w = make_world()
    pre = w.copy()
    call(w, "create_ach_transfer", **_ach(transactionAmount=499))
    call(w, "set_transaction_note", accountIdentifier=ACC, transactionIdentifier="t-rent", note="Nov rent")
    checks = [
        Check(
            name="ach_out",
            check="only",
            app="bank_us",
            collection="ach_transfers",
            new=True,
            where={"amount_minor": Cond(eq=49900), "account_number": Cond(eq="99887766")},
        ),
        Check(
            name="debit",
            check="only",
            app="bank_us",
            collection="transactions",
            new=True,
            where={"transaction_type": Cond(eq="achOut"), "bank_routing_number": Cond(eq=OTHER_ROUTING)},
        ),
        Check(name="payments_kept", check="unchanged", app="bank_us", collection="bill_payments"),
        Check(name="transactions_kept", check="unchanged", app="bank_us", collection="transactions"),
    ]
    assert grade(checks, pre, w) == {
        "ach_out": True,
        "debit": True,
        "payments_kept": True,
        "transactions_kept": False,
    }
