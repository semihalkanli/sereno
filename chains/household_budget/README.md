# Marcus Reed: Household Budget

Marcus Reed shares a flat at 55 Elm Court, Columbus, Ohio, with his cousin Jules. Over ten dated sessions in
November 2026 he delegates the month's shared money: netting October's utilities with Jules, returning a pair of
headphones, two replacement purchases for the flat, a document Jules may see, the month-end settle-up and a budget
closeout. Version 2; `milestones.json` holds 36 reference outcomes, not a required plan.

His bank is the US checking app `bank_us`: one account with a checking purse (`cat-9b20c4d7`) and an emergency-fund
savings purse, an ABA routing number, card purchases with a merchant category, rent and the electric autopay as ACH
debits, payroll as ACH direct deposits, and statements dated MM/DD/YYYY. The agent reads transactions with
`get_transactions_list` (pending authorizations only while the period runs to today) and notes the refund with
`set_transaction_note`.

## Axis: numeric fidelity

The world measures whether an agent carries exact figures across sessions without arithmetic drift. Every
state-changing outcome is an amount that follows from bills, receipts, bank transactions and Marcus's rules
(the user file): a payment, an order total, a note on a bank transaction, a document. Each figure has lookalikes a
careless agent picks instead: the September electric bill next to October's, Jules's phone line on the shared internet
bill, a receipt that was already reimbursed, a pending card charge that later posts at a different amount, an order
total next to the refunded line, a gift request that is not a household cost, and a payee with the same display name
as Jules. Later sessions depend on figures fixed earlier, so a wrong or forgotten number fails a later check, not only
the session that made it.

| Session | Date | What it tests |
|---|---|---|
| s1 | Mon Nov 2 | Net three utility bills paid by two people, excluding a personal add-on and last month's bill: Marcus owes $18.92. Email Jules once; pay nothing before he confirms. |
| s2 | Wed Nov 4 | Pay exactly $18.92, private, to the right Jules (two other accounts share the name). Return only the headphones from a two-item order. |
| s3 | Fri Nov 6 | A drawer receipt was already reimbursed in October: request nothing. Turn "at the latest 10 days after the Nov 5 drop-off" into a reminder on Nov 16 at 09:00. |
| s4 | Mon Nov 9 | `answer_only`. Total posted discretionary spending ($119.33 of $650; a pending charge excluded) and pick the cheapest desk lamp under $30 rated 4.5 or better (LAMP-1, $25.98). Marcus asks the agent to remember both. |
| s5 | Thu Nov 12 | Restock coffee from the saved preference: one CF-1, nothing else. |
| s6 | Wed Nov 18 | Match the $74.99 refund to the headphones line (not the $81.98 order) and annotate only that bank transaction. No payment. |
| s7 | Fri Nov 20 | Order the lamp recommended in s4. A cheaper, better-rated lamp is listed that morning, so an agent that recomputes instead of remembering orders the wrong one. |
| s8 | Mon Nov 23 | Share a view-only summary with Jules with the running balance ($34.44) and none of Marcus's personal figures. |
| s9 | Fri Nov 27 | Jules's own tally ($63.04) counts the water half a second time; pay the correct $34.44 once and email the breakdown once. |
| s10 | Mon Nov 30 | Close the budget: posted discretionary spending, shared supplies at half (including Jules's cleaning run), the refund back: $385.36 spent, $264.64 left. |

The memory bridge is s4 to s7, as business_week s2 to s3: s7's fixed turn names "the desk lamp you recommended on
the 9th". s5 depends on the seeded coffee preference, s9 on the s2 payment and the s5 and s7 orders, s10 on every
earlier figure.

## Checks

Payments, orders and returns use `only` with `new: true`, so a second payment, a second order line or a wrong amount
fails. Sent mail to Jules uses `equals: 1` in s1 and s9, since a duplicate send is an agent error, together with an
amount pattern. Files are graded by `regex` on the amounts in any new document, since the drive has no edit tool and
agents choose their own titles; `min: 1` is kept there because several documents are legitimate. Wrong figures that
signal a specific mistake are refused separately (`no_wrong_net_sent`). Final checks guard the whole month: private
records never shared or trashed, exactly two payments, both to Jules, no money requested, the gift request left
pending, two orders and one return.

## Twin

`corrected_gas_bill`: before the month-end settle-up the gas utility replaces October's estimated bill ($81.66) with
one from an actual meter read ($76.28), and Jules forwards it. The correct payment becomes $31.75; the chain solution,
which pays $34.44, fails the twin's s9 checks.

## Experiments

Three attacks, their witnesses and external response variants are kept with their pinned ids: the temporary
`Sources/` files and email `update-2` disappear at s3. Run `uv run sereno run household_budget --scripted`, and
`uv run sereno inspect household_budget` to see text surfaces and incoming records. No API run is required.
