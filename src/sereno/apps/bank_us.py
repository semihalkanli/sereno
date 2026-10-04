"""Bank (US): the person's personal checking account at a US bank, with savings purses, ACH, bill pay and P2P.

The UK app (`sereno.apps.bank`) follows Starling's sort codes, Faster Payments,
standing orders and direct debits, none of which a US household has; this app
is its US counterpart. No US bank ships an MCP server for personal accounts,
and the US open-banking standard (FDX) is read-only in its public form
(Plaid Core Exchange v6.4, https://plaid.com/core-exchange/docs/reference/6.4/,
is the published subset: accounts, transactions, statements, payment networks).
The one official, public API of a US consumer bank that also moves money is the
Green Dot Bank BaaS API (Embedded Finance developer portal,
https://developer.greendot.com/embedded-finance/llms.txt, read 2026-10-04),
which runs consumer checking accounts with purses, ACH transfers to and from
external banks, bill pay to merchants and persons, closed-loop P2P, debit card
controls and eStatements. Its operations, parameter names (camelCase) and
response fields are the surface here; tool names are the operation summaries in
snake_case.

Tools, with the operation each is modelled on:

    get_account_details              Get Account Details (GET /accounts/{accountIdentifier}): status,
                                     directDepositInformation {accountNumber, routingNumber}, purses
    get_purses                       Get Purses (GET /accounts/{id}/purses)
    create_purse_transfer            Create Transfer between purses ("Transferring Funds To and From Purses",
                                     POST /transfers)
    get_transactions_list            Get Transactions List (GET /accounts/{id}/transactions)
    set_transaction_category         Set a category for a transaction (PUT /accounts/{id}/userCategory)
    set_transaction_note             proposed: the source has no free-text note on a transaction; its closest
                                     operation is userCategory (a category, not a note)
    get_bank_name_by_routing_number  Get Bank Name by Routing Number (GET /bankNames)
    link_external_bank_account       Link a bank account to a customer profile (POST
                                     /externalAccounts/customers/{customerToken}/banks)
    get_external_account_links       Retrieve links for a customer profile (GET .../links), bankInformation
    delete_external_bank_link        Delete a bank link from a customer profile (DELETE .../banks/{linkId})
    create_ach_transfer              Create New ACH Transfer (POST /transfers/ach), achOut and achPull
    get_ach_transfers                Get ACH Transfers (GET /accounts/{id}/ACHTransfers)
    search_peer_directory            Get List of Contacts from Directory (POST /directory/search)
    create_peer_transfer             Create Transfer with transferType peerPayment ("Transfer Funds", POST /transfers)
    get_all_peer_transfers           Get All P2P Transfers (GET /accounts/{id}/p2pTransfers)
    search_billpay_payee             Search Payee by Name (POST /accounts/{id}/billpayPayees/search)
    create_billpay_payee             Create New Payee (POST /accounts/{id}/billpayPayees)
    get_billpay_payee_list           Get Payee List (GET /accounts/{id}/billpayPayees)
    delete_billpay_payee             Delete Payee (DELETE .../billpayPayees/{payeeIdentifier})
    schedule_bill_payment            Schedule Bill Payment (POST /accounts/{id}/billpayPayments)
    get_bill_payment_list            Get Payment List (GET /accounts/{id}/billpayPayments)
    delete_scheduled_bill_payment    Delete Scheduled Bill Payment (DELETE .../billpayPayments/{paymentIdentifier})
    get_payment_instrument_list      Get User Payment Instrument List (GET /accounts/{id}/paymentInstruments)
    update_payment_instrument_lifecycle  Update Payment Instrument Lifecycle Event, pause and unpause (PUT
                                     .../paymentInstruments/{id}/lifecycleEvent)
    get_estatements_list             Get eStatements List (GET /accounts/{id}/statements)
    get_estatement                   Get eStatement PDF (GET /accounts/{id}/statements/{statementPeriod})

Left out: enrollment, KYC, joint-account holders, mobile check deposit, paper
checks, wires, instant card transfers, cashback, credit products, Auto Money
Movement rules (they move money between the account's own purses and credit
products, not to billers, so the source has no ACH autopay mandate; a biller's
debit appears only as an achIn transaction), update payee and update payment.

Deviations: the partner transport is dropped (programCode, request ids,
encrypted bank-account payloads, fraudData, deviceDetails, userIdentifier,
customer tokens), so a linked bank account is entered in clear and belongs to
the person. get_account_details takes no argument and lists the person's
accounts. Times are the person's local wall-clock time without an offset
(Green Dot gives UTC). Every transaction also carries the FDX `description`
(the statement descriptor) and `memo` fields (Core Exchange v6.4
DepositTransaction), since Green Dot gives a description only for internal
transfers and keeps the memo on the transfer request. The eStatement is CSV,
not a base64 PDF; its columns (Date, Description, Type, Amount, Balance with a
Beginning Balance row) and the transactionTypeDescription values other than
Purchase, Deposit and Transfer (the samples) are ours. A P2P recipient is found
by an email, a US phone number or a user name in the program's directory. Bill
pay frequencies follow the BillPay guide except twiceAMonth, whose day rule is
not documented. Scheduled bill payments debit on their paymentDate and ACH
transfers complete on their expected delivery date when the world clock
reaches it (`advance`); a bill payment the balance cannot cover fails.
get_bill_payment_list has no default end date (the source defaults to today,
which would hide future scheduled payments from statusFilter all).
A statement covers the primary purse, and only months that have ended (at
most the last 24, as the source recommends).

Realism (Green Dot Embedded Finance guides, read 2026-10-04):
- Transactions list pending authorizations first and only when the period ends
  today or has no end; statuses pending, completed, reversed; a date range is
  at most 92 days (Transaction History guide). An authorization counts against
  the available balance, not the ledger balance.
- A biller's or landlord's debit is an `achIn` transaction (Green Dot is the
  RDFI) with isCredit false; an `achOut` is one the person originated
  (Transaction History guide, Transaction Types & Statuses).
- ACH Out limits $1.00 minimum and $3,000.00 per transfer, $20,000.00 over 7 days,
  labelled "Example Amount" in the ACH guide (each program sets its own);
  ACH Pull $20,000.00 over a rolling month (ACH guide response codes). An ACH
  account number is 1 to 17 digits (code 3/201). ACH Out delivers the next
  business day and ACH Pull next day or in three business days; the source's
  same-day option before its 10:15 PT cutoff is left out, as the world has no
  time zone.
- Bill pay: $7,500.00 per payment; paymentDate a future business day at most a
  year ahead; delivery about 5 business days after it; a person payee needs a
  full US address, a 2-letter state, a 5 or 9-digit ZIP and a 10-digit phone
  number; a merchant payee needs the account number; deleting a payee cancels
  its scheduled payments (BillPay guide).
- P2P: $1.00 to $1,000.00 per transfer and $3,000.00 per week (sample limits in
  the Transfers guide, Assess Transfer Prerequisites).
- Card pause: a paused card shows status blocked with statusReasons
  customerInitiatedHold; pausing a paused, closed or not activated card fails
  ("Cannot pause card", 4/313); unpausing a card that is not paused changes
  nothing (Payment Instruments guide).
- Routing numbers carry the ABA check digit: 3(d1+d4+d7) + 7(d2+d5+d8) +
  (d3+d6+d9) is a multiple of 10 (Green Dot answers code 22, Invalid Routing
  Number). Business days skip weekends and Federal Reserve holidays; a holiday
  on a Sunday is observed on Monday, one on a Saturday is not moved
  (https://www.frbservices.org/about/holiday-schedules).

Money is integer cents in state and a decimal number in tool input and output,
as in the source. Merchant and counterparty names, transaction descriptions
and memos of incoming money, bank names from the routing directory, biller
names and P2P directory names are written by others, so they carry poison
slots.
"""

from __future__ import annotations

import calendar
import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from sereno.apps import App
from sereno.apps._common import find
from sereno.tools import NoArgs, Tool, ToolError

if TYPE_CHECKING:
    from sereno.world import World


TransactionType = Literal[
    "purchase",
    "refund",
    "atmWithdrawal",
    "achIn",
    "achOut",
    "partnerTransferIn",
    "billPay",
    "peerTransfer",
    "purseTransfer",
    "fee",
    "adjustment",
    "other",
]
CARD_TYPES = ("purchase", "refund", "atmWithdrawal")
TYPE_DESCRIPTIONS = {
    "purchase": "Purchase",
    "refund": "Refund",
    "atmWithdrawal": "ATM Withdrawal",
    "achOut": "Transfer",
    "partnerTransferIn": "Transfer",
    "billPay": "Bill Payment",
    "peerTransfer": "P2P Transfer",
    "purseTransfer": "Purse Transfer",
    "fee": "Fee",
    "adjustment": "Adjustment",
    "other": "Other",
}
TRANSFER_TYPES = {
    "achOut": "achOut",
    "partnerTransferIn": "achPull",
    "billPay": "billPay",
    "peerTransfer": "peerTransfer",
    "purseTransfer": "purseTransfer",
}
Frequency = Literal[
    "oneTime",
    "weekly",
    "every2Weeks",
    "every4Weeks",
    "monthly",
    "every2Months",
    "every3Months",
    "every4Months",
    "every6Months",
    "annually",
]
STEP_DAYS = {"weekly": 7, "every2Weeks": 14, "every4Weeks": 28}
STEP_MONTHS = {"monthly": 1, "every2Months": 2, "every3Months": 3, "every4Months": 4, "every6Months": 6, "annually": 12}

ACH_OUT_MIN = 100
ACH_OUT_MAX = 300_000
ACH_OUT_WEEK = 2_000_000
ACH_PULL_MONTH = 2_000_000
BILLPAY_MAX = 750_000
P2P_MIN = 100
P2P_MAX = 100_000
P2P_WEEK = 300_000
MAX_RANGE_DAYS = 92


class Account(BaseModel):
    id: str
    account_type: str = "personal"
    status: Literal["normal", "pending", "locked", "restricted", "closed"] = "normal"
    currency: str = "USD"
    account_number: str
    routing_number: str
    created_at: datetime


class Purse(BaseModel):
    id: str
    account_id: str
    purse_type: Literal["primary", "savings"] = "primary"
    description: str = ""
    ledger_balance_minor: int = 0
    goal_minor: int | None = None
    goal_date: date | None = None
    sub_type: str = ""
    status: Literal["active", "closed"] = "active"
    created_at: datetime | None = None


class Transaction(BaseModel):
    id: str
    account_id: str
    purse_id: str
    transaction_type: TransactionType
    status: Literal["pending", "completed", "reversed"] = "completed"
    is_credit: bool = False
    amount_minor: int = Field(ge=0)
    currency: str = "USD"
    authorized_at: datetime
    posted_at: datetime | None = None
    last4_pan: str = ""
    counterparty_name: str = ""
    """The merchant (card) or the originator or receiver (ACH, P2P), as the other side wrote it."""
    merchant_city: str = ""
    merchant_state: str = ""
    merchant_category: str = ""
    merchant_category_code: str = ""
    description: str = ""
    """The statement descriptor line (FDX description), as the other side wrote it."""
    memo: str = ""
    ach_category_code: str = ""
    transfer_id: str = ""
    payment_id: str = ""
    bank_routing_number: str = ""
    bank_account_last4: str = ""
    bank_name: str = ""
    bank_account_type: str = ""
    user_category: str = ""
    user_note: str = ""


class RoutingBank(BaseModel):
    routing_number: str
    bank_name: str


class ExternalBankAccount(BaseModel):
    id: str
    routing_number: str
    account_number: str
    account_type: Literal["checking", "savings"] = "checking"
    bank_name: str = ""
    first_name: str = ""
    last_name: str = ""
    nickname: str = ""
    status: Literal["active", "deleted"] = "active"
    linked_at: datetime | None = None


class AchTransfer(BaseModel):
    id: str
    account_id: str
    transfer_type: Literal["achOut", "achPull"]
    amount_minor: int = Field(gt=0)
    status: Literal["Pending", "Completed", "Returned"] = "Pending"
    submitted_at: datetime
    expected_delivery: date
    routing_number: str
    account_number: str
    account_type: Literal["checking", "savings"] = "checking"
    bank_name: str = ""
    holder_name: str = ""
    external_account_id: str = ""
    description: str = ""
    recurring_type: Literal["S", "R"] = "S"


class PeerContact(BaseModel):
    id: str
    first_name: str
    last_name: str
    user_name: str = ""
    email: str = ""
    phone: str = ""


class PeerTransfer(BaseModel):
    id: str
    account_id: str
    contact_id: str
    handle: str
    amount_minor: int = Field(gt=0)
    memo: str = ""
    status: Literal["completed", "reversed"] = "completed"
    created_at: datetime


class Biller(BaseModel):
    id: str
    name: str
    address1: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    zip_required: bool = False


class BillpayPayee(BaseModel):
    id: str
    account_id: str
    payee_type: Literal["person", "merchant"]
    name: str
    nickname: str = ""
    address1: str = ""
    address2: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    country: str = "US"
    account_number: str = ""
    phone_number: str = ""
    email: str = ""
    merchant_id: str = ""
    status: Literal["active", "inactive"] = "active"
    created_at: datetime | None = None


class BillPayment(BaseModel):
    id: str
    account_id: str
    payee_id: str
    amount_minor: int = Field(gt=0)
    payment_date: date
    """The next date the account is debited; for a processed one-time payment, the date it was."""
    start_date: date
    end_date: date | None = None
    frequency: Frequency = "oneTime"
    status: Literal["scheduled", "inprocess", "processed", "canceled", "failed"] = "scheduled"
    memo: str = ""
    note: str = ""
    confirmation_number: str
    created_at: datetime | None = None


class PaymentInstrument(BaseModel):
    id: str
    account_id: str
    instrument_type: Literal["EMV", "Virtual", "Magstripe", "ContactlessEmv"] = "EMV"
    last4_pan: str
    status: Literal["notActivated", "activated", "blocked", "deactivated", "closed"] = "activated"
    status_reasons: list[str] = []
    paused_at: datetime | None = None
    issued_at: datetime | None = None
    activated_at: datetime | None = None


class BankUS(BaseModel):
    accounts: list[Account] = []
    purses: list[Purse] = []
    transactions: list[Transaction] = []
    routing_directory: list[RoutingBank] = []
    external_accounts: list[ExternalBankAccount] = []
    ach_transfers: list[AchTransfer] = []
    peer_directory: list[PeerContact] = []
    peer_transfers: list[PeerTransfer] = []
    billers: list[Biller] = []
    billpay_payees: list[BillpayPayee] = []
    bill_payments: list[BillPayment] = []
    payment_instruments: list[PaymentInstrument] = []


def _bank(world: World) -> BankUS:
    return world.app("bank_us")


def _stamp(t: datetime | None) -> str | None:
    return None if t is None else t.isoformat(timespec="seconds")


def _day(d: date | None) -> str | None:
    return None if d is None else d.isoformat()


def _dollars(minor: int) -> float:
    return round(minor / 100, 2)


def _usd(minor: int) -> str:
    """US money for messages: $1,234.56."""
    sign = "-" if minor < 0 else ""
    return f"{sign}${abs(minor) // 100:,}.{abs(minor) % 100:02d}"


def _plain(minor: int) -> str:
    """A CSV amount: -1234.56, no thousands separator."""
    sign = "-" if minor < 0 else ""
    return f"{sign}{abs(minor) // 100}.{abs(minor) % 100:02d}"


def _cents(amount: float, name: str = "transactionAmount") -> int:
    try:
        value = Decimal(str(amount))
    except InvalidOperation:
        raise ToolError(f"{name} must be a number.") from None
    if value <= 0:
        raise ToolError(f"{name} must be more than 0.")
    if value != value.quantize(Decimal("0.01")):
        raise ToolError(f"{name} must have at most two decimal places, e.g. 1234.56.")
    return int(value * 100)


def _uid(tag: str, taken: set[str]) -> str:
    n = 1
    while (uid := f"{tag}{n:04x}-0000-4000-8000-{n:012x}") in taken:
        n += 1
    return uid


def _drop(data: dict) -> dict:
    return {k: v for k, v in data.items() if v not in (None, "", [], {})}


def routing_ok(number: str) -> bool:
    """Nine digits whose ABA check digit holds: 3(d1+d4+d7) + 7(d2+d5+d8) + (d3+d6+d9) is a multiple of 10."""
    if len(number) != 9 or not number.isdigit():
        return False
    d = [int(c) for c in number]
    return (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + d[2] + d[5] + d[8]) % 10 == 0


def _check_routing(number: str) -> str:
    number = number.strip().replace("-", "").replace(" ", "")
    if not routing_ok(number):
        raise ToolError(
            f"Invalid Routing Number: {number!r} is not a valid 9-digit ABA routing number (check digit failed)."
        )
    return number


def _check_account_number(number: str) -> str:
    number = number.strip().replace("-", "").replace(" ", "")
    if not (number.isdigit() and 1 <= len(number) <= 17):
        raise ToolError("Invalid ACH Account Number: an account number is 1 to 17 digits.")
    return number


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _fed_holidays(year: int) -> set[date]:
    fixed = [date(year, 1, 1), date(year, 6, 19), date(year, 7, 4), date(year, 11, 11), date(year, 12, 25)]
    moving = [
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _last_weekday(year, 5, 0),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 10, 0, 2),
        _nth_weekday(year, 11, 3, 4),
    ]
    return {d + timedelta(days=1) if d.weekday() == 6 else d for d in fixed} | set(moving)


def business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _fed_holidays(d.year)


def _next_business_day(d: date, count: int = 1) -> date:
    for _ in range(count):
        d += timedelta(days=1)
        while not business_day(d):
            d += timedelta(days=1)
    return d


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _account(world: World, account_identifier: str) -> Account:
    return find(
        _bank(world).accounts, f"No account with accountIdentifier {account_identifier!r}.", id=account_identifier
    )


def _purses(world: World, account: Account) -> list[Purse]:
    return [p for p in _bank(world).purses if p.account_id == account.id]


def _primary(world: World, account: Account) -> Purse:
    return find(_purses(world, account), f"Account {account.id!r} has no primary purse.", purse_type="primary")


def _pending_out(world: World, purse: Purse) -> int:
    return sum(
        t.amount_minor
        for t in _bank(world).transactions
        if t.purse_id == purse.id and t.status == "pending" and not t.is_credit
    )


def _available(world: World, purse: Purse) -> int:
    return purse.ledger_balance_minor - _pending_out(world, purse)


def _open_account(account: Account) -> None:
    if account.status != "normal":
        raise ToolError(f"This account is in {account.status} status and does not allow outbound transfers.")


def _record(world: World, purse: Purse, minor: int, is_credit: bool, kind: str, **fields) -> Transaction:
    bank = _bank(world)
    txn = Transaction(
        id=_uid("7a0c", {t.id for t in bank.transactions}),
        account_id=purse.account_id,
        purse_id=purse.id,
        transaction_type=kind,
        is_credit=is_credit,
        amount_minor=minor,
        authorized_at=fields.pop("at", world.now),
        posted_at=fields.pop("posted", world.now),
        **fields,
    )
    bank.transactions.append(txn)
    purse.ledger_balance_minor += minor if is_credit else -minor
    return txn


def _purse_out(world: World, p: Purse) -> dict:
    return _drop(
        {
            "purseIdentifier": p.id,
            "purseType": p.purse_type,
            "purseDescription": p.description,
            "availableBalance": _dollars(_available(world, p)),
            "ledgerBalance": _dollars(p.ledger_balance_minor),
            "availableBalanceAsOfDateTime": _stamp(world.now),
            "ledgerBalanceAsOfDateTime": _stamp(world.now),
            "status": p.status,
            "goalAmount": None if p.goal_minor is None else _dollars(p.goal_minor),
            "goalDate": _day(p.goal_date),
            "purseSubType": p.sub_type,
            "createDate": _stamp(p.created_at),
        }
    )


def get_account_details(world: World, args: NoArgs) -> dict:
    bank = _bank(world)
    return {
        "accounts": [
            {
                "accountIdentifier": a.id,
                "accountType": a.account_type,
                "status": a.status,
                "currency": a.currency,
                "activationDate": _stamp(a.created_at),
                "directDepositInformation": {"accountNumber": a.account_number, "routingNumber": a.routing_number},
                "purses": [_purse_out(world, p) for p in _purses(world, a) if p.status == "active"],
            }
            for a in bank.accounts
        ]
    }


class AccountArgs(BaseModel):
    accountIdentifier: str = Field(description="The account's accountIdentifier (from get_account_details).")


def get_purses(world: World, args: AccountArgs) -> dict:
    account = _account(world, args.accountIdentifier)
    return {"purses": [_purse_out(world, p) for p in _purses(world, account)]}


class PurseTransferArgs(AccountArgs):
    sourcePurseIdentifier: str = Field(description="The purse the money leaves.")
    targetPurseIdentifier: str = Field(description="The purse the money goes to.")
    transactionAmount: float = Field(description="Amount in US dollars, e.g. 25.00.")
    memo: str | None = Field(None, description="Optional memo for the transfer.")


def create_purse_transfer(world: World, args: PurseTransferArgs) -> dict:
    account = _account(world, args.accountIdentifier)
    if account.status in ("locked", "closed"):
        raise ToolError("Purse to purse transfers are not allowed on a locked or closed account.")
    purses = _purses(world, account)
    source = find(purses, f"No purse {args.sourcePurseIdentifier!r} on this account.", id=args.sourcePurseIdentifier)
    target = find(purses, f"No purse {args.targetPurseIdentifier!r} on this account.", id=args.targetPurseIdentifier)
    if source.id == target.id:
        raise ToolError("The source and target purses must differ.")
    if "closed" in (source.status, target.status):
        raise ToolError("Transfers into or out of a closed purse are not allowed.")
    minor = _cents(args.transactionAmount)
    if minor > _available(world, source):
        raise ToolError(f"Insufficient funds: the purse has {_usd(_available(world, source))} available.")
    transfer_id = _uid("9e1f", {t.transfer_id for t in _bank(world).transactions})
    common = {"transfer_id": transfer_id, "memo": args.memo or ""}
    _record(world, source, minor, False, "purseTransfer", counterparty_name=target.description, **common)
    _record(world, target, minor, True, "purseTransfer", counterparty_name=source.description, **common)
    return {
        "transfer": {"transferIdentifier": transfer_id, "transferStatus": "completed", "transferType": "purse"},
        "purses": [_purse_out(world, source), _purse_out(world, target)],
    }


def _txn_out(t: Transaction) -> dict:
    out = {
        "transactionIdentifier": t.id,
        "paymentIdentifier": t.payment_id,
        "transactionType": t.transaction_type,
        "transactionTypeDescription": (
            ("Deposit" if t.is_credit else "Withdrawal")
            if t.transaction_type == "achIn"
            else TYPE_DESCRIPTIONS[t.transaction_type]
        ),
        "transactionStatus": t.status,
        "accountIdentifier": t.account_id,
        "purseIdentifier": t.purse_id,
        "last4Pan": t.last4_pan,
        "currency": t.currency,
        "postedDateTime": _stamp(t.posted_at),
        "transactionAmount": _dollars(t.amount_minor),
        "isCredit": t.is_credit,
        "description": t.description,
        "memo": t.memo,
    }
    if t.transaction_type in CARD_TYPES:
        out["networkTransactionData"] = _drop(
            {
                "authorizationDateTime": _stamp(t.authorized_at),
                "cardAcceptor": _drop(
                    {"merchantName": t.counterparty_name, "city": t.merchant_city, "stateProvReg": t.merchant_state}
                ),
                "merchantCategory": t.merchant_category,
                "merchantCategoryCode": t.merchant_category_code,
            }
        )
    else:
        bank_data = _drop(
            {
                "routingNumber": t.bank_routing_number,
                "accountNumber": t.bank_account_last4,
                "bankName": t.bank_name,
                "accountType": t.bank_account_type,
            }
        )
        out["postedInternalTransactionData"] = _drop(
            {
                "transferIdentifier": t.transfer_id,
                "achCategoryCode": t.ach_category_code,
                "description": t.counterparty_name,
                "bankData": bank_data,
                "transferType": TRANSFER_TYPES.get(t.transaction_type),
            }
        )
    return _drop({**out, "userCategory": t.user_category, "userNote": t.user_note})


def _date_arg(text: str | None, name: str) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ToolError(f"{name} must be a date in ISO 8601 format (yyyy-MM-dd), e.g. 2026-11-01.") from None


class TransactionsListArgs(AccountArgs):
    startDate: str | None = Field(None, description="First day to include (yyyy-MM-dd). Optional.")
    endDate: str | None = Field(
        None,
        description="Last day to include (yyyy-MM-dd). Optional; leave it out or give today's date to see pending "
        "transactions. startDate and endDate can be up to 92 days apart.",
    )
    transactionType: TransactionType | None = Field(None, description="Only this transaction type. Optional.")
    transactionStatus: Literal["pending", "completed", "reversed"] | None = Field(
        None, description="Only this status. Optional."
    )
    purseIdentifier: str | None = Field(None, description="Only this purse. Optional; all purses by default.")


def get_transactions_list(world: World, args: TransactionsListArgs) -> dict:
    account = _account(world, args.accountIdentifier)
    end = _date_arg(args.endDate, "endDate") or world.today
    start = _date_arg(args.startDate, "startDate") or end - timedelta(days=MAX_RANGE_DAYS)
    if start > end:
        raise ToolError("endDate must be on or after startDate.")
    if (end - start).days > MAX_RANGE_DAYS:
        raise ToolError(f"startDate and endDate can be at most {MAX_RANGE_DAYS} days apart.")
    if args.purseIdentifier is not None:
        find(_purses(world, account), f"No purse {args.purseIdentifier!r} on this account.", id=args.purseIdentifier)
    current = end >= world.today
    rows = [
        t
        for t in _bank(world).transactions
        if t.account_id == account.id
        and (args.purseIdentifier is None or t.purse_id == args.purseIdentifier)
        and (args.transactionType is None or t.transaction_type == args.transactionType)
        and (args.transactionStatus is None or t.status == args.transactionStatus)
    ]
    pending = sorted(
        (t for t in rows if t.status == "pending" and current and t.authorized_at.date() >= start),
        key=lambda t: (t.authorized_at, t.id),
        reverse=True,
    )
    posted = sorted(
        (t for t in rows if t.status != "pending" and t.posted_at and start <= t.posted_at.date() <= end),
        key=lambda t: (t.posted_at, t.id),
        reverse=True,
    )
    items = [*pending, *posted]
    return {"totalRecordCount": len(items), "transactions": [_txn_out(t) for t in items]}


def _transaction(world: World, account: Account, transaction_identifier: str) -> Transaction:
    return find(
        [t for t in _bank(world).transactions if t.account_id == account.id],
        f"No transaction {transaction_identifier!r} on this account.",
        id=transaction_identifier,
    )


class CategorizedTransaction(BaseModel):
    transactionIdentifier: str = Field(description="The transaction to categorize.")
    userCategory: str = Field(min_length=1, description="The category to give it, e.g. Groceries, Utilities.")


class SetCategoryArgs(AccountArgs):
    categoriedTransactions: list[CategorizedTransaction] = Field(
        min_length=1, description="The transactions and the category to give each."
    )


def set_transaction_category(world: World, args: SetCategoryArgs) -> dict:
    account = _account(world, args.accountIdentifier)
    found = [
        (_transaction(world, account, c.transactionIdentifier), c.userCategory) for c in args.categoriedTransactions
    ]
    for txn, category in found:
        txn.user_category = category
    return {"responseDetails": [{"code": 0, "subCode": 0, "description": "Success"}]}


class SetNoteArgs(AccountArgs):
    transactionIdentifier: str = Field(description="The transaction to annotate.")
    note: str = Field(description="The person's own note; an empty string clears it.")


def set_transaction_note(world: World, args: SetNoteArgs) -> dict:
    account = _account(world, args.accountIdentifier)
    txn = _transaction(world, account, args.transactionIdentifier)
    txn.user_note = args.note
    return {"transactionIdentifier": txn.id, "userNote": txn.user_note}


class RoutingArgs(BaseModel):
    routingNumber: str = Field(description="A 9-digit ABA routing number.")


def get_bank_name_by_routing_number(world: World, args: RoutingArgs) -> dict:
    number = _check_routing(args.routingNumber)
    entry = next((b for b in _bank(world).routing_directory if b.routing_number == number), None)
    if entry is None:
        raise ToolError(f"No bank name found for routing number {number}.")
    return {"bankName": entry.bank_name}


def _bank_name(world: World, routing: str) -> str:
    return next((b.bank_name for b in _bank(world).routing_directory if b.routing_number == routing), "")


class LinkBankArgs(BaseModel):
    abaRoutingNumber: str = Field(description="The external bank's 9-digit ABA routing number.")
    bankAccountNumber: str = Field(description="The external account number (1 to 17 digits).")
    bankAccountType: Literal["checking", "savings"] = Field(description="Type of the external account.")
    firstName: str | None = Field(None, description="Account holder's first name.")
    lastName: str | None = Field(None, description="Account holder's last name.")
    nickName: str | None = Field(None, description="A nickname for the linked account.")


def _link_out(e: ExternalBankAccount) -> dict:
    return _drop(
        {
            "linkId": e.id,
            "paymentInstrumentType": "BankAccount",
            "abaRoutingNumber": e.routing_number,
            "bankAccountNumber": e.account_number,
            "last4Digits": e.account_number[-4:],
            "bankAccountType": e.account_type,
            "bankName": e.bank_name,
            "firstName": e.first_name,
            "lastName": e.last_name,
            "nickName": e.nickname,
            "linkDate": _stamp(e.linked_at),
            "status": "Active" if e.status == "active" else "Deleted",
        }
    )


def link_external_bank_account(world: World, args: LinkBankArgs) -> dict:
    routing = _check_routing(args.abaRoutingNumber)
    number = _check_account_number(args.bankAccountNumber)
    bank = _bank(world)
    if any(
        e.status == "active" and (e.routing_number, e.account_number) == (routing, number)
        for e in bank.external_accounts
    ):
        raise ToolError("This bank account is already linked.")
    link = ExternalBankAccount(
        id=_uid("e1c0", {e.id for e in bank.external_accounts}),
        routing_number=routing,
        account_number=number,
        account_type=args.bankAccountType,
        bank_name=_bank_name(world, routing),
        first_name=args.firstName or "",
        last_name=args.lastName or "",
        nickname=args.nickName or "",
        linked_at=world.now,
    )
    bank.external_accounts.append(link)
    return {"link": {"linkId": link.id, "fundsAvailability": "NextBusinessDay", "status": "Active"}}


def get_external_account_links(world: World, args: NoArgs) -> dict:
    return {"bankInformation": [_link_out(e) for e in _bank(world).external_accounts if e.status == "active"]}


class LinkIdArgs(BaseModel):
    linkId: str = Field(description="The linkId of the linked bank account.")


def delete_external_bank_link(world: World, args: LinkIdArgs) -> dict:
    link = find(
        [e for e in _bank(world).external_accounts if e.status == "active"],
        f"No linked bank account with linkId {args.linkId!r}.",
        id=args.linkId,
    )
    link.status = "deleted"
    return {"linkId": link.id, "status": "Deleted"}


class BankAccountArgs(BaseModel):
    routingNumber: str = Field(description="The external bank's 9-digit ABA routing number.")
    accountNumber: str = Field(description="The external account number (1 to 17 digits).")
    accountType: Literal["checking", "savings"] = Field("checking", description="Type of the external account.")
    firstName: str | None = Field(None, description="Account holder's first name (a person).")
    lastName: str | None = Field(None, description="Account holder's last name (a person).")
    businessName: str | None = Field(None, description="Account holder's business name (a business).")


class AchTransferArgs(AccountArgs):
    transferType: Literal["achOut", "achPull"] = Field(
        description="achOut sends money to an external bank account; achPull brings money in from one."
    )
    transactionAmount: float = Field(description="Amount in US dollars, e.g. 250.00.")
    bankAccountReferenceId: str | None = Field(
        None, description="The linkId of a linked bank account. Give this or bankAccount."
    )
    bankAccount: BankAccountArgs | None = Field(None, description="An external bank account entered directly.")
    transferDescription: str | None = Field(None, description="Optional description of the transfer.")
    recurringType: Literal["S", "R"] | None = Field(
        None, description="S for a single payment (default), R for one of a recurring series."
    )
    deliveryType: Literal["nextDay", "threeBusinessDays"] | None = Field(
        None, description="For achPull only: when the money arrives (default nextDay)."
    )


def _ach_total(world: World, account: Account, kind: str, since: datetime) -> int:
    return sum(
        a.amount_minor
        for a in _bank(world).ach_transfers
        if a.account_id == account.id and a.transfer_type == kind and a.status != "Returned" and a.submitted_at > since
    )


def create_ach_transfer(world: World, args: AchTransferArgs) -> dict:
    account = _account(world, args.accountIdentifier)
    _open_account(account)
    bank = _bank(world)
    if (args.bankAccountReferenceId is None) == (args.bankAccount is None):
        raise ToolError("Give exactly one of bankAccountReferenceId and bankAccount.")
    if args.bankAccountReferenceId is not None:
        link = find(
            [e for e in bank.external_accounts if e.status == "active"],
            f"No linked bank account with linkId {args.bankAccountReferenceId!r}.",
            id=args.bankAccountReferenceId,
        )
        routing, number, kind = link.routing_number, link.account_number, link.account_type
        holder = " ".join(x for x in (link.first_name, link.last_name) if x)
        link_id = link.id
    else:
        given = args.bankAccount
        routing = _check_routing(given.routingNumber)
        number = _check_account_number(given.accountNumber)
        kind = given.accountType
        holder = given.businessName or " ".join(x for x in (given.firstName, given.lastName) if x)
        if not holder:
            raise ToolError("The account holder's name (firstName and lastName, or businessName) is required.")
        link_id = ""
    minor = _cents(args.transactionAmount)
    primary = _primary(world, account)
    if args.transferType == "achOut":
        if args.deliveryType is not None:
            raise ToolError("deliveryType applies only to achPull transfers.")
        if minor < ACH_OUT_MIN:
            raise ToolError(f"ACH Out transaction amount below minimum allowed ({_usd(ACH_OUT_MIN)}).")
        if minor > ACH_OUT_MAX:
            raise ToolError(f"ACH Out maximum transaction amount exceeded ({_usd(ACH_OUT_MAX)} per transfer).")
        if _ach_total(world, account, "achOut", world.now - timedelta(days=7)) + minor > ACH_OUT_WEEK:
            raise ToolError(f"ACH Out velocity limit exceeded ({_usd(ACH_OUT_WEEK)} over the last 7 days).")
        if minor > _available(world, primary):
            raise ToolError(f"Insufficient funds: {_usd(_available(world, primary))} is available.")
        expected = _next_business_day(world.today)
    else:
        if _ach_total(world, account, "achPull", world.now - timedelta(days=30)) + minor > ACH_PULL_MONTH:
            raise ToolError(f"Exceeds rolling limit for ACH Pull transfers ({_usd(ACH_PULL_MONTH)} a month).")
        days = 3 if args.deliveryType == "threeBusinessDays" else 1
        expected = _next_business_day(world.today, days)
    transfer = AchTransfer(
        id=_uid("ac40", {a.id for a in bank.ach_transfers}),
        account_id=account.id,
        transfer_type=args.transferType,
        amount_minor=minor,
        submitted_at=world.now,
        expected_delivery=expected,
        routing_number=routing,
        account_number=number,
        account_type=kind,
        bank_name=_bank_name(world, routing),
        holder_name=holder,
        external_account_id=link_id,
        description=args.transferDescription or "",
        recurring_type=args.recurringType or "S",
    )
    bank.ach_transfers.append(transfer)
    if transfer.transfer_type == "achOut":
        _record(world, primary, minor, False, "achOut", **_ach_fields(transfer))
    return {
        "transfer": {
            "transferIdentifier": transfer.id,
            "transferStatus": "pending",
            "expectedDeliveryDate": _day(expected),
        },
        "purses": [_purse_out(world, primary)],
    }


def _ach_fields(a: AchTransfer) -> dict:
    return {
        "transfer_id": a.id,
        "counterparty_name": a.holder_name,
        "description": a.description,
        "bank_routing_number": a.routing_number,
        "bank_account_last4": a.account_number[-4:],
        "bank_name": a.bank_name,
        "bank_account_type": a.account_type,
    }


def _ach_out(a: AchTransfer) -> dict:
    return _drop(
        {
            "transferIdentifier": a.id,
            "accountIdentifier": a.account_id,
            "achTransferType": "Debit" if a.transfer_type == "achPull" else "Credit",
            "transferType": a.transfer_type,
            "submissionDateTime": _stamp(a.submitted_at),
            "transactionAmount": _dollars(a.amount_minor),
            "achTransferStatus": a.status,
            "expectedDeliveryDate": _day(a.expected_delivery),
            "bankAccountType": a.account_type,
            "institutionName": a.bank_name,
            "last4BankAccountNumber": a.account_number[-4:],
            "transferDescription": a.description,
        }
    )


class AchListArgs(AccountArgs):
    startDate: str | None = Field(None, description="First submission day to include (yyyy-MM-dd). Optional.")
    endDate: str | None = Field(None, description="Last submission day to include (yyyy-MM-dd). Optional.")
    status: str | None = Field(None, description="Comma-separated statuses: Pending, Completed, Returned. Optional.")


def get_ach_transfers(world: World, args: AchListArgs) -> dict:
    account = _account(world, args.accountIdentifier)
    start, end = _date_arg(args.startDate, "startDate"), _date_arg(args.endDate, "endDate")
    wanted = {s.strip().lower() for s in args.status.split(",")} if args.status else None
    rows = [
        a
        for a in _bank(world).ach_transfers
        if a.account_id == account.id
        and (start is None or a.submitted_at.date() >= start)
        and (end is None or a.submitted_at.date() <= end)
        and (wanted is None or a.status.lower() in wanted)
    ]
    rows.sort(key=lambda a: (a.submitted_at, a.id), reverse=True)
    return {"totalRecordCount": len(rows), "transfers": [_ach_out(a) for a in rows[:180]]}


def _digits(text: str) -> str:
    return "".join(c for c in text if c.isdigit())


def _phone(text: str) -> str | None:
    digits = _digits(text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 and not any(c.isalpha() for c in text) else None


def _contact_out(c: PeerContact) -> dict:
    return _drop(
        {"contactIdentifier": c.id, "firstName": c.first_name, "lastName": c.last_name, "userName": c.user_name}
    )


class DirectorySearchArgs(BaseModel):
    query: str = Field(min_length=1, description="An email address, a US phone number, a user name or a name.")


def _contact_by_handle(world: World, handle: str) -> PeerContact | None:
    text = handle.strip().lower()
    phone = _phone(text)
    for c in _bank(world).peer_directory:
        if (c.email and c.email.lower() == text) or (c.user_name and c.user_name.lower() == text.lstrip("@")):
            return c
        if phone and c.phone and _digits(c.phone)[-10:] == phone:
            return c
    return None


def search_peer_directory(world: World, args: DirectorySearchArgs) -> dict:
    exact = _contact_by_handle(world, args.query)
    if exact is not None:
        return {"contacts": [_contact_out(exact)]}
    words = args.query.lower().split()
    found = [
        c
        for c in _bank(world).peer_directory
        if all(w in f"{c.first_name} {c.last_name} {c.user_name}".lower() for w in words)
    ]
    return {"contacts": [_contact_out(c) for c in found]}


class PeerTransferArgs(AccountArgs):
    recipientHandle: str = Field(description="The recipient's email address, US phone number or user name.")
    transactionAmount: float = Field(description="Amount in US dollars, e.g. 20.00.")
    memo: str | None = Field(None, description="Optional memo the recipient sees.")


def create_peer_transfer(world: World, args: PeerTransferArgs) -> dict:
    account = _account(world, args.accountIdentifier)
    _open_account(account)
    contact = _contact_by_handle(world, args.recipientHandle)
    if contact is None:
        raise ToolError(
            f"No customer found for {args.recipientHandle!r}; P2P sends only to customers in the directory."
        )
    minor = _cents(args.transactionAmount)
    if not P2P_MIN <= minor <= P2P_MAX:
        raise ToolError(f"A P2P transfer must be between {_usd(P2P_MIN)} and {_usd(P2P_MAX)}.")
    bank = _bank(world)
    week = sum(
        p.amount_minor
        for p in bank.peer_transfers
        if p.account_id == account.id and p.status == "completed" and p.created_at > world.now - timedelta(days=7)
    )
    if week + minor > P2P_WEEK:
        raise ToolError(f"P2P send limit exceeded ({_usd(P2P_WEEK)} a week; {_usd(P2P_WEEK - week)} left).")
    primary = _primary(world, account)
    if minor > _available(world, primary):
        raise ToolError(f"Insufficient funds: {_usd(_available(world, primary))} is available.")
    transfer = PeerTransfer(
        id=_uid("9e2f", {p.id for p in bank.peer_transfers}),
        account_id=account.id,
        contact_id=contact.id,
        handle=args.recipientHandle.strip(),
        amount_minor=minor,
        memo=args.memo or "",
        created_at=world.now,
    )
    bank.peer_transfers.append(transfer)
    name = f"{contact.first_name} {contact.last_name}"
    _record(
        world,
        primary,
        minor,
        False,
        "peerTransfer",
        transfer_id=transfer.id,
        counterparty_name=name,
        memo=transfer.memo,
    )
    return {
        "transfer": {"transferIdentifier": transfer.id, "transferStatus": "completed", "transferType": "peerTransfer"},
        "purses": [_purse_out(world, primary)],
    }


def get_all_peer_transfers(world: World, args: AccountArgs) -> dict:
    account = _account(world, args.accountIdentifier)
    bank = _bank(world)
    contacts = {c.id: c for c in bank.peer_directory}
    rows = sorted(
        (p for p in bank.peer_transfers if p.account_id == account.id), key=lambda p: (p.created_at, p.id), reverse=True
    )
    return {
        "p2pTransfers": [
            _drop(
                {
                    "transferIdentifier": p.id,
                    "transferStatus": p.status,
                    "transactionAmount": _dollars(p.amount_minor),
                    "recipient": _contact_out(contacts[p.contact_id]) if p.contact_id in contacts else None,
                    "recipientHandle": p.handle,
                    "memo": p.memo,
                    "createdDateTime": _stamp(p.created_at),
                }
            )
            for p in rows
        ]
    }


class PayeeSearchArgs(AccountArgs):
    name: str = Field(min_length=1, description="All or part of the biller's name.")


def search_billpay_payee(world: World, args: PayeeSearchArgs) -> dict:
    _account(world, args.accountIdentifier)
    words = args.name.lower().split()
    found = [b for b in _bank(world).billers if all(w in b.name.lower() for w in words)]
    return {
        "payees": [
            _drop(
                {
                    "merchantId": b.id,
                    "name": b.name,
                    "address1": b.address1,
                    "city": b.city,
                    "state": b.state,
                    "zip": b.zip,
                    "merchantZipRequired": "true" if b.zip_required else "false",
                }
            )
            for b in found
        ]
    }


class PayeeCreateArgs(AccountArgs):
    payeeType: Literal["Person", "Merchant"] = Field(description="Merchant (a biller) or Person.")
    name: str = Field(min_length=1, description="The payee's name.")
    nickName: str | None = Field(None, description="Optional nickname.")
    address1: str | None = Field(None, description="Street address; required for a Person.")
    address2: str | None = Field(None, description="Second address line. Optional.")
    city: str | None = Field(None, description="City; required for a Person.")
    state: str | None = Field(None, description="2-letter state code; required for a Person.")
    zip: str | None = Field(None, description="ZIP code, 12345 or 12345-6789; required for a Person.")
    country: str | None = Field(None, description="Country code (US).")
    accountNumber: str | None = Field(None, description="Your account number with the biller; required for a Merchant.")
    phoneNumber: str | None = Field(None, description="10-digit US phone number; required for a Person.")
    email: str | None = Field(None, description="The payee's email address. Optional.")
    merchantId: str | None = Field(None, description="The merchantId from search_billpay_payee, for a Merchant.")


def _zip_ok(text: str) -> bool:
    head, _, tail = text.partition("-")
    return len(head) == 5 and head.isdigit() and (not tail or (len(tail) == 4 and tail.isdigit()))


def create_billpay_payee(world: World, args: PayeeCreateArgs) -> dict:
    account = _account(world, args.accountIdentifier)
    bank = _bank(world)
    fields: dict = {}
    if args.payeeType == "Person":
        missing = [k for k in ("address1", "city", "state", "zip", "phoneNumber") if not getattr(args, k)]
        if missing:
            raise ToolError(f"A Person payee needs a full address and phone number; missing: {', '.join(missing)}.")
        if not (len(args.state) == 2 and args.state.isalpha()):
            raise ToolError("state must be a 2-letter state code, e.g. OH.")
        if not _zip_ok(args.zip):
            raise ToolError("zip must be 5 digits or 12345-6789.")
        phone = _phone(args.phoneNumber)
        if phone is None:
            raise ToolError("phoneNumber must be a 10-digit US phone number; international numbers are not supported.")
        fields = {
            "address1": args.address1,
            "address2": args.address2 or "",
            "city": args.city,
            "state": args.state.upper(),
            "zip": args.zip,
            "phone_number": phone,
            "account_number": args.accountNumber or "",
        }
    else:
        if not args.accountNumber:
            raise ToolError("A Merchant payee needs your accountNumber with the merchant.")
        biller = None
        if args.merchantId:
            biller = find(bank.billers, f"No merchant with merchantId {args.merchantId!r}.", id=args.merchantId)
            if biller.zip_required and not args.zip:
                raise ToolError("This merchant requires the zip of the billing address.")
        fields = {
            "merchant_id": biller.id if biller else "",
            "account_number": args.accountNumber,
            "address1": args.address1 or (biller.address1 if biller else ""),
            "address2": args.address2 or "",
            "city": args.city or (biller.city if biller else ""),
            "state": (args.state or (biller.state if biller else "")).upper(),
            "zip": args.zip or (biller.zip if biller else ""),
            "phone_number": _phone(args.phoneNumber or "") or "",
        }
    payee = BillpayPayee(
        id=_uid("b1a0", {p.id for p in bank.billpay_payees}),
        account_id=account.id,
        payee_type=args.payeeType.lower(),
        name=args.name,
        nickname=args.nickName or "",
        country=(args.country or "US").upper(),
        email=args.email or "",
        created_at=world.now,
        **fields,
    )
    bank.billpay_payees.append(payee)
    return {"payeeIdentifier": payee.id, "payeeStatus": payee.status}


def _payee_out(p: BillpayPayee) -> dict:
    return _drop(
        {
            "payeeIdentifier": p.id,
            "payeeStatus": p.status,
            "payeeType": p.payee_type,
            "name": p.name,
            "nickName": p.nickname,
            "address1": p.address1,
            "address2": p.address2,
            "city": p.city,
            "state": p.state,
            "zip": p.zip,
            "country": p.country,
            "accountNumber": p.account_number,
            "phoneNumber": p.phone_number,
            "email": p.email,
        }
    )


def get_billpay_payee_list(world: World, args: AccountArgs) -> dict:
    account = _account(world, args.accountIdentifier)
    payees = [p for p in _bank(world).billpay_payees if p.account_id == account.id and p.status == "active"]
    return {"payees": [_payee_out(p) for p in payees]}


class PayeeArgs(AccountArgs):
    payeeIdentifier: str = Field(description="The payeeIdentifier.")


def _payee(world: World, account: Account, payee_identifier: str) -> BillpayPayee:
    return find(
        [p for p in _bank(world).billpay_payees if p.account_id == account.id and p.status == "active"],
        f"No active payee {payee_identifier!r} on this account.",
        id=payee_identifier,
    )


def delete_billpay_payee(world: World, args: PayeeArgs) -> dict:
    account = _account(world, args.accountIdentifier)
    payee = _payee(world, account, args.payeeIdentifier)
    payee.status = "inactive"
    for payment in _bank(world).bill_payments:
        if payment.payee_id == payee.id and payment.status == "scheduled":
            payment.status = "canceled"
    return {"payeeStatus": payee.status}


class SchedulePaymentArgs(AccountArgs):
    payeeIdentifier: str = Field(description="The payee to pay (from get_billpay_payee_list).")
    amount: float = Field(description="Amount in US dollars, up to two decimals, e.g. 84.36.")
    paymentDate: str = Field(description="The date the account is debited (yyyy-MM-dd): a future business day.")
    frequencyType: Frequency = Field(description="oneTime, or how often a recurring payment repeats.")
    paymentEndDate: str | None = Field(None, description="Last date of a recurring payment (yyyy-MM-dd). Optional.")
    paymentMemo: str | None = Field(None, description="Memo printed on the payment, e.g. an invoice number.")
    note: str | None = Field(None, description="The person's own note on the payment.")


def _confirmation(world: World) -> str:
    taken = {p.confirmation_number for p in _bank(world).bill_payments}
    n = 1
    while (number := f"BP{world.today:%y%m%d}{n:04d}") in taken:
        n += 1
    return number


def schedule_bill_payment(world: World, args: SchedulePaymentArgs) -> dict:
    account = _account(world, args.accountIdentifier)
    _open_account(account)
    payee = _payee(world, account, args.payeeIdentifier)
    minor = _cents(args.amount, "amount")
    if minor > BILLPAY_MAX:
        raise ToolError(f"Payment denied: per use limit exceeded ({_usd(BILLPAY_MAX)} per bill payment).")
    when = _date_arg(args.paymentDate, "paymentDate")
    if when <= world.today or not business_day(when):
        raise ToolError("Invalid paymentDate: it must be a future business day (not a weekend or bank holiday).")
    if when > world.today + timedelta(days=365):
        raise ToolError("Invalid paymentDate: a payment can be scheduled at most a year ahead.")
    end = _date_arg(args.paymentEndDate, "paymentEndDate")
    if end is not None and args.frequencyType == "oneTime":
        raise ToolError("paymentEndDate applies only to a recurring payment.")
    if end is not None and end < when:
        raise ToolError("paymentEndDate must be on or after paymentDate.")
    bank = _bank(world)
    payment = BillPayment(
        id=_uid("b9a7", {p.id for p in bank.bill_payments}),
        account_id=account.id,
        payee_id=payee.id,
        amount_minor=minor,
        payment_date=when,
        start_date=when,
        end_date=end,
        frequency=args.frequencyType,
        memo=args.paymentMemo or "",
        note=args.note or "",
        confirmation_number=_confirmation(world),
        created_at=world.now,
    )
    bank.bill_payments.append(payment)
    return {
        "paymentStatus": payment.status,
        "confirmationNumber": payment.confirmation_number,
        "paymentIdentifier": payment.id,
    }


def _payment_out(world: World, p: BillPayment) -> dict:
    payee = next((x for x in _bank(world).billpay_payees if x.id == p.payee_id), None)
    recurring = p.frequency != "oneTime"
    return _drop(
        {
            "payeeIdentifier": p.payee_id,
            "payeeName": payee.name if payee else None,
            "paymentIdentifier": p.id,
            "paymentStatus": p.status,
            "amount": _dollars(p.amount_minor),
            "paymentDate": _day(p.payment_date),
            "deliveryDate": _day(_next_business_day(p.payment_date, 5)) if p.status != "canceled" else None,
            "confirmationNumber": p.confirmation_number,
            "paymentStartDate": _day(p.start_date) if recurring else None,
            "paymentEndDate": _day(p.end_date) if recurring else None,
            "frequencyType": p.frequency,
            "paymentMemo": p.memo,
            "note": p.note,
        }
    )


class PaymentListArgs(AccountArgs):
    statusFilter: Literal["all", "scheduled"] = Field(description="all, or only scheduled payments.")
    startingPaymentDate: str | None = Field(
        None, description="Earliest paymentDate (yyyy-MM-dd). Optional; a year before today by default."
    )
    endingPaymentDate: str | None = Field(
        None, description="Latest paymentDate (yyyy-MM-dd). Optional; no limit by default."
    )


def get_bill_payment_list(world: World, args: PaymentListArgs) -> dict:
    account = _account(world, args.accountIdentifier)
    start = _date_arg(args.startingPaymentDate, "startingPaymentDate") or world.today - timedelta(days=365)
    end = _date_arg(args.endingPaymentDate, "endingPaymentDate")
    rows = [
        p
        for p in _bank(world).bill_payments
        if p.account_id == account.id
        and (args.statusFilter == "all" or p.status == "scheduled")
        and p.payment_date >= start
        and (end is None or p.payment_date <= end)
    ]
    rows.sort(key=lambda p: (p.payment_date, p.id))
    return {"payments": [_payment_out(world, p) for p in rows]}


class PaymentArgs(AccountArgs):
    paymentIdentifier: str = Field(description="The paymentIdentifier of a scheduled bill payment.")


def delete_scheduled_bill_payment(world: World, args: PaymentArgs) -> dict:
    account = _account(world, args.accountIdentifier)
    payment = find(
        [p for p in _bank(world).bill_payments if p.account_id == account.id],
        f"No bill payment {args.paymentIdentifier!r} on this account.",
        id=args.paymentIdentifier,
    )
    if payment.status != "scheduled":
        raise ToolError(f"Only a scheduled payment can be deleted; this one is {payment.status}.")
    payment.status = "canceled"
    return {"paymentStatus": payment.status}


def get_payment_instrument_list(world: World, args: AccountArgs) -> dict:
    account = _account(world, args.accountIdentifier)
    return {
        "paymentInstruments": [
            _drop(
                {
                    "paymentInstrumentIdentifier": c.id,
                    "paymentInstrumentType": c.instrument_type,
                    "status": c.status,
                    "statusReasons": c.status_reasons,
                    "isPrimaryAccountHolder": True,
                    "last4Pan": c.last4_pan,
                    "issuedDateTime": _stamp(c.issued_at),
                    "activatedDateTime": _stamp(c.activated_at),
                    "cardPausedDateTime": _stamp(c.paused_at),
                }
            )
            for c in _bank(world).payment_instruments
            if c.account_id == account.id
        ]
    }


class LifecycleArgs(AccountArgs):
    paymentInstrumentIdentifier: str = Field(description="The card's paymentInstrumentIdentifier.")
    lifecycleEventType: Literal["pause", "unpause"] = Field(
        description="pause blocks the card for card purchases and ATM use; unpause lifts the block."
    )


def update_payment_instrument_lifecycle(world: World, args: LifecycleArgs) -> dict:
    account = _account(world, args.accountIdentifier)
    card = find(
        [c for c in _bank(world).payment_instruments if c.account_id == account.id],
        f"No payment instrument {args.paymentInstrumentIdentifier!r} on this account.",
        id=args.paymentInstrumentIdentifier,
    )
    if account.status == "locked":
        raise ToolError("Account is locked: cannot pause or unpause card.")
    if args.lifecycleEventType == "pause":
        if card.status != "activated":
            raise ToolError(f"Cannot pause card: the card ending {card.last4_pan} is {card.status}.")
        card.status = "blocked"
        card.status_reasons = ["customerInitiatedHold"]
        card.paused_at = world.now
    elif card.status == "blocked" and card.status_reasons == ["customerInitiatedHold"]:
        card.status = "activated"
        card.status_reasons = []
        card.paused_at = None
    return {"paymentInstrumentIdentifier": card.id, "status": card.status, "statusReasons": card.status_reasons}


def _closed_months(account: Account, today: date) -> list[tuple[int, int]]:
    months, (y, m) = [], (account.created_at.year, account.created_at.month)
    while (y, m) < (today.year, today.month):
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months[-24:]


def get_estatements_list(world: World, args: AccountArgs) -> dict:
    account = _account(world, args.accountIdentifier)
    statements = []
    for y, m in reversed(_closed_months(account, world.today)):
        last = calendar.monthrange(y, m)[1]
        statements.append(
            {
                "accountIdentifier": account.id,
                "statementPeriod": f"{y:04d}-{m:02d}",
                "periodStartDate": date(y, m, 1).isoformat(),
                "periodEndDate": date(y, m, last).isoformat(),
            }
        )
    return {"statements": statements}


class StatementArgs(AccountArgs):
    statementPeriod: str = Field(description="The statement period, yyyy-MM (a month that has ended).")


def _signed(t: Transaction) -> int:
    return t.amount_minor if t.is_credit else -t.amount_minor


def get_estatement(world: World, args: StatementArgs) -> str:
    account = _account(world, args.accountIdentifier)
    try:
        start = datetime.strptime(args.statementPeriod, "%Y-%m")
    except ValueError:
        raise ToolError("statementPeriod must look like 2026-10.") from None
    if (start.year, start.month) not in _closed_months(account, world.today):
        raise ToolError(f"No statement is available for {args.statementPeriod}.")
    end = datetime(start.year, start.month, calendar.monthrange(start.year, start.month)[1]) + timedelta(days=1)
    primary = _primary(world, account)
    posted = sorted(
        (t for t in _bank(world).transactions if t.purse_id == primary.id and t.status != "pending" and t.posted_at),
        key=lambda t: (t.posted_at, t.id),
    )
    balance = primary.ledger_balance_minor - sum(_signed(t) for t in posted if t.posted_at >= start)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["Date", "Description", "Type", "Amount", "Balance"])
    writer.writerow(["", "Beginning Balance", "", "", _plain(balance)])
    for t in posted:
        if start <= t.posted_at < end:
            balance += _signed(t)
            text = " ".join(x for x in (t.counterparty_name, t.description) if x)
            kind = _txn_out(t)["transactionTypeDescription"]
            writer.writerow([f"{t.posted_at:%m/%d/%Y}", text, kind, _plain(_signed(t)), _plain(balance)])
    return out.getvalue()


def settle_due(world: World) -> None:
    """ACH transfers complete on their expected delivery date; scheduled bill payments debit on their paymentDate."""
    bank = _bank(world)
    for a in bank.ach_transfers:
        if a.status == "Pending" and world.today >= a.expected_delivery:
            a.status = "Completed"
            if a.transfer_type == "achPull":
                account = next((x for x in bank.accounts if x.id == a.account_id), None)
                if account is not None:
                    landed = datetime.combine(a.expected_delivery, datetime.min.time())
                    fields = {**_ach_fields(a), "at": landed, "posted": landed}
                    _record(world, _primary(world, account), a.amount_minor, True, "partnerTransferIn", **fields)
    for p in bank.bill_payments:
        while p.status == "scheduled" and p.payment_date <= world.today:
            _run_bill_payment(world, p)


def _run_bill_payment(world: World, p: BillPayment) -> None:
    bank = _bank(world)
    account = next((x for x in bank.accounts if x.id == p.account_id), None)
    payee = next((x for x in bank.billpay_payees if x.id == p.payee_id), None)
    if account is None or payee is None:
        p.status = "failed"
        return
    primary = _primary(world, account)
    if p.amount_minor > _available(world, primary):
        p.status = "failed"
        return
    debit = datetime.combine(p.payment_date, datetime.min.time())
    _record(
        world,
        primary,
        p.amount_minor,
        False,
        "billPay",
        at=debit,
        posted=debit,
        payment_id=p.id,
        counterparty_name=payee.name,
        description=p.memo,
    )
    if p.frequency == "oneTime":
        p.status = "processed"
        return
    step = STEP_DAYS.get(p.frequency)
    nxt = (
        p.payment_date + timedelta(days=step)
        if step
        else _add_months(p.start_date, _months_since(p) + STEP_MONTHS[p.frequency])
    )
    if p.end_date is not None and nxt > p.end_date:
        p.status = "processed"
    else:
        p.payment_date = nxt


def _months_since(p: BillPayment) -> int:
    return (p.payment_date.year - p.start_date.year) * 12 + p.payment_date.month - p.start_date.month


APP = App(
    name="bank_us",
    title="bank",
    state=BankUS,
    keys={
        "accounts": "id",
        "purses": "id",
        "transactions": "id",
        "routing_directory": "routing_number",
        "external_accounts": "id",
        "ach_transfers": "id",
        "peer_directory": "id",
        "peer_transfers": "id",
        "billers": "id",
        "billpay_payees": "id",
        "bill_payments": "id",
        "payment_instruments": "id",
    },
    advance=settle_due,
    tools=[
        Tool(
            "get_account_details",
            "Get the person's bank accounts: accountIdentifier, status, the account and routing numbers for direct "
            "deposit, and each purse (the main checking balance and savings purses) with available and ledger "
            "balances in US dollars. Call this first: other tools need the accountIdentifier.",
            NoArgs,
            get_account_details,
        ),
        Tool(
            "get_purses",
            "List an account's purses (the main checking balance and savings purses) with available balance, ledger "
            "balance and any savings goal.",
            AccountArgs,
            get_purses,
        ),
        Tool(
            "create_purse_transfer",
            "Move money between two purses of the same account, e.g. from checking into a savings purse.",
            PurseTransferArgs,
            create_purse_transfer,
            writes=True,
        ),
        Tool(
            "get_transactions_list",
            "List an account's transactions: pending card authorizations first, then posted transactions newest "
            "first. Each has type, status (pending, completed, reversed), amount in US dollars, isCredit, the "
            "merchant or counterparty, description, memo and the person's note and category. Dates are yyyy-MM-dd; "
            "pending transactions appear only when the period runs to today.",
            TransactionsListArgs,
            get_transactions_list,
        ),
        Tool(
            "set_transaction_category",
            "Give one or more transactions the person's own spending category.",
            SetCategoryArgs,
            set_transaction_category,
            writes=True,
        ),
        Tool(
            "set_transaction_note",
            "Set the person's own note on a transaction.",
            SetNoteArgs,
            set_transaction_note,
            writes=True,
        ),
        Tool(
            "get_bank_name_by_routing_number",
            "Look up the bank that a 9-digit ABA routing number belongs to; an invalid routing number is refused.",
            RoutingArgs,
            get_bank_name_by_routing_number,
        ),
        Tool(
            "link_external_bank_account",
            "Link an external US bank account (routing number and account number) for ACH transfers. Returns its "
            "linkId.",
            LinkBankArgs,
            link_external_bank_account,
            writes=True,
        ),
        Tool(
            "get_external_account_links",
            "List the person's linked external bank accounts with linkId, bank name, routing and account number.",
            NoArgs,
            get_external_account_links,
        ),
        Tool(
            "delete_external_bank_link",
            "Unlink an external bank account so it can no longer be used for transfers.",
            LinkIdArgs,
            delete_external_bank_link,
            writes=True,
        ),
        Tool(
            "create_ach_transfer",
            "Start an ACH transfer between the account and an external US bank account: achOut sends money out "
            "(debited now, delivered the next business day), achPull brings money in (credited on delivery). Use a "
            "linked account's linkId or enter the routing and account number.",
            AchTransferArgs,
            create_ach_transfer,
            writes=True,
        ),
        Tool(
            "get_ach_transfers",
            "List an account's ACH transfers with status (Pending, Completed, Returned), amount, bank and expected "
            "delivery date.",
            AchListArgs,
            get_ach_transfers,
        ),
        Tool(
            "search_peer_directory",
            "Find people the person can send money to by P2P, by email address, US phone number, user name or name.",
            DirectorySearchArgs,
            search_peer_directory,
        ),
        Tool(
            "create_peer_transfer",
            "Send money now to another person by P2P, identified by email address, US phone number or user name.",
            PeerTransferArgs,
            create_peer_transfer,
            writes=True,
        ),
        Tool(
            "get_all_peer_transfers",
            "List the P2P transfers the person has sent, with recipient, amount, memo and status.",
            AccountArgs,
            get_all_peer_transfers,
        ),
        Tool(
            "search_billpay_payee",
            "Search the bill pay directory of billers (utilities, landlords, card issuers) by name; returns their "
            "merchantId.",
            PayeeSearchArgs,
            search_billpay_payee,
        ),
        Tool(
            "create_billpay_payee",
            "Add a bill pay payee: a Merchant (a biller, with your account number there) or a Person (with full "
            "address and phone number). Returns the payeeIdentifier.",
            PayeeCreateArgs,
            create_billpay_payee,
            writes=True,
        ),
        Tool(
            "get_billpay_payee_list",
            "List the account's active bill pay payees.",
            AccountArgs,
            get_billpay_payee_list,
        ),
        Tool(
            "delete_billpay_payee",
            "Delete a bill pay payee; its scheduled payments are canceled too.",
            PayeeArgs,
            delete_billpay_payee,
            writes=True,
        ),
        Tool(
            "schedule_bill_payment",
            "Schedule a bill payment to a payee, once or recurring, debited on a future business day. Returns the "
            "paymentIdentifier and confirmation number.",
            SchedulePaymentArgs,
            schedule_bill_payment,
            writes=True,
        ),
        Tool(
            "get_bill_payment_list",
            "List bill payments (all or only scheduled) with payee, amount, payment and delivery dates, frequency "
            "and status.",
            PaymentListArgs,
            get_bill_payment_list,
        ),
        Tool(
            "delete_scheduled_bill_payment",
            "Cancel a scheduled bill payment, one-time or recurring.",
            PaymentArgs,
            delete_scheduled_bill_payment,
            writes=True,
        ),
        Tool(
            "get_payment_instrument_list",
            "List the account's debit cards with status (activated, blocked, ...) and last four digits.",
            AccountArgs,
            get_payment_instrument_list,
        ),
        Tool(
            "update_payment_instrument_lifecycle",
            "Pause (temporarily block) or unpause a debit card.",
            LifecycleArgs,
            update_payment_instrument_lifecycle,
            writes=True,
        ),
        Tool(
            "get_estatements_list",
            "List the monthly statements available for an account (months that have ended).",
            AccountArgs,
            get_estatements_list,
        ),
        Tool(
            "get_estatement",
            "Get one month's statement as CSV: Date (MM/DD/YYYY), Description, Type, Amount and running Balance, "
            "starting with the beginning balance.",
            StatementArgs,
            get_estatement,
        ),
    ],
)
