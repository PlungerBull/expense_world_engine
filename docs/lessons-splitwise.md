# API Lessons — Splitwise

> Splitwise is the most sophisticated reference for multi-party financial data modeling. Unlike YNAB (one user, one budget) or TickTick (tasks), Splitwise is built entirely around money flowing between people. Most of these lessons apply to our deferred `transaction_shares` phase but are documented now so the design is considered from the start.

---

## 1. Separate "Who Paid" from "Who Owes"

The most important lesson from Splitwise: a multi-party transaction has two independent dimensions. `paid_share` is who actually spent the money. `owed_share` is who is responsible for what portion. These are stored separately on every `ExpenseUser` record. A $100 dinner where Alex paid everything but it splits three ways:
- Alex: `paid_share: 100, owed_share: 33.33`
- Each partner: `paid_share: 0, owed_share: 33.33`

`net_balance = paid_share - owed_share`. This three-field model is the correct foundation for any multi-party expense. Never collapse it into a single amount field.

**Our decision (deferred):** When `transaction_shares` is built, each share row carries `paid_share_cents` and `owed_share_cents` independently. Net balance is always derived, never stored.

---

## 2. The Expense-Splits Join Table

Splitwise implies a classic junction pattern: an `expenses` table storing the total and the payer, and an `expense_splits` table with one row per participant per expense. Each split row holds that participant's `paid_share`, `owed_share`, and `user_id`. Independently queryable: "show me everything user X owes" is a single indexed query.

**Our decision (deferred):** `transaction_shares` will follow this exact pattern when implemented.

---

## 3. Pre-Computed Balances Table

Splitwise maintains a dedicated `balances` table storing the net amount one user owes another — a pre-computed summary updated atomically on every expense create, modify, or settle. Rather than scanning all splits on every request, the balances table answers "how much does user A owe user B?" in a single indexed lookup. It is a denormalised cache — the source of truth is always the splits, but the cache makes reads fast.

**Our decision (deferred):** Follow this pattern when implementing cross-user sharing. The `current_balance_cents` field on `expense_bank_accounts` is the single-user equivalent of this principle — already in place.

---

## 4. Three Split Types as an Enum

Splitwise supports three split modes — `EQUAL`, `EXACT`, and `PERCENT` — stored as an enum on the expense. All splits within one expense use the same method. Each type has different validation rules: `EQUAL` divides automatically, `EXACT` amounts must sum to the total, `PERCENT` shares must sum to 100.

**Our decision (deferred):** Store as smallint when implemented. 1 = equal, 2 = exact, 3 = percent.

---

## 5. Group ID 0 — The Universal Inbox Sentinel

Splitwise uses `group_id: 0` as a sentinel value for expenses that don't belong to any group. Rather than making `group_id` nullable and handling null throughout the codebase, every expense always has a group — the "no group" case gets a well-known ID. This makes all queries uniform.

**Our decision:** A useful pattern to keep in mind. Our equivalent is the `is_person = false` / `is_person = true` distinction on `expense_bank_accounts` — all accounts exist in the same table with a flag rather than splitting into two tables with nullable joins.

---

## 6. `original_debts` vs `simplified_debts` — Two Views, One Source

A Splitwise group response includes both `original_debts` (every raw bilateral debt as entered) and `simplified_debts` (the minimum-transaction restructuring that brings everyone to zero). These are two different computed views of the same underlying split data, returned together in one API response. Simplification never modifies source data — it only produces a different read view.

**Our decision:** Computed views belong in the API response layer. The database stores raw truth. The engine computes derived views on read. This principle applies beyond just sharing — dashboard summaries, P&L views, and budget vs. actual calculations follow the same rule.

---

## 7. Settlements Are Just Transactions

Splitwise records debt settlements as a special type of expense with `payment: true`. There is no separate payments or settlements table. A payment is an expense where one person's `paid_share` equals the total and the other's `owed_share` equals the total — a net balance change that cancels the original debt. One table, one set of queries, one audit trail.

**Our decision:** Settlements between people are standard transactions with a flag. No separate schema. This is already how our transfer model works — a debt settlement is a paired transaction on the person's virtual account, same as any other transfer.

---

## 8. The Zero-Sum Invariant

The sum of all net balances in a Splitwise group always equals zero. If someone is owed money, someone else owes it. This is a hard invariant enforced at the application layer on every write: every new expense must produce split rows where the sum of `paid_share` equals the sum of `owed_share` equals the total amount.

**Our decision:** The engine validates this invariant before committing any multi-party transaction. This applies now to our transfer model: any paired transfer must net to zero across both rows. The engine enforces this — it is not left to the client to get right.
