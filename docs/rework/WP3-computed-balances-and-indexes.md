# WP3 — Compute the account balance, and add the indexes that requires

**Read [`README.md`](README.md) first.** Blocks WP4. Independent of WP1 and WP2.

> ## Do not split this package
>
> The balance deletion and the index migration **must land together**. Shipping the
> deletion alone turns every balance read into a sequential scan of the ledger table.
> Shipping the indexes alone is harmless but pointless. One migration, both halves.

---

## The problem

`expense_bank_accounts.current_balance_cents` is a stored running total, kept in step with
the ledger by a `CLAUDE.md` convention ("Balance updates are atomic") that every write path
has to honour, via `app/helpers/balance.py` (125 lines) and 36 call sites.

It is a derived value with two sources of truth. One missed update, one crash between the
two writes, one manual SQL correction, and the balance disagrees with the rows it claims
to summarise — permanently, silently, and with no way to tell which one is right.

This is the same defect the engine is already removing in WP2. A stored balance is a
stored `amount_home_cents` with a different name.

**Scale is not a counter-argument.** Plain-text ledger tools (beancount, hledger,
ledger-cli) and Actual Budget compute balances from transactions and store nothing. Banks
materialise balances because they sum millions of rows per request. A personal ledger
reaches perhaps 10k–100k rows in a lifetime; with an index, summing one account's rows is
an index scan of a few thousand entries and does not grow with the rest of the table.

## The discovery that makes this package bigger than it looks

`expense_transactions` has **two indexes**: its primary key, and `(user_id, updated_at)` —
and that second one exists to serve `/sync`, which WP4 deletes.

There is **no index on `account_id`, `date`, `category_id`, `reconciliation_id` or
`transfer_transaction_id`** — the five columns every real query filters or joins on. The
same is true across the domain tables: their only non-unique index is the sync one.

Verify it yourself:

```sql
SELECT tablename, indexdef FROM pg_indexes
WHERE schemaname = 'public' AND indexdef NOT LIKE '%UNIQUE%'
ORDER BY tablename;
```

**Why nobody noticed: the stored balance was masking the missing index.** Nothing ever
needed to query transactions by account, because the account's balance was a column you
could read directly. Delete the cache and the query appears — against a table that has
never been indexed for it.

This is not an argument for keeping the cache. It is the discovery that the
denormalisation was buying an index the schema should have had anyway, and charging a
drift risk for it.

## What is decided

- Drop `current_balance_cents`. Delete `app/helpers/balance.py` and its call sites.
- The balance is the signed sum of the account's non-deleted transactions, including its
  `@Opening` entry.
- **`current_balance_cents` stays on the wire, unchanged.** Only its source changes, from
  a column read to a computed sum. This is the largest internal change in the whole program
  and it should be invisible to every client.
- The index migration ships in the same change.
- `CLAUDE.md`'s "Balance updates are atomic" convention is **deleted and replaced**, not
  quietly dropped. It is marked ⏳ and points here.

## What you must work out

- **The exact index set.** The list below is the audit's recommendation, not a
  prescription. Derive the real set from the queries you find, and **confirm each one with
  `EXPLAIN`** rather than trusting this table.

  | Index | Serves |
  |---|---|
  | `expense_transactions (user_id, account_id) WHERE deleted_at IS NULL` | The computed balance. Non-negotiable — this is what replaces the cached column. |
  | `expense_transactions (user_id, date)` | Month bucketing, dashboard, every date-ranged list |
  | `expense_transactions (user_id, category_id)` | Report grouping |
  | `expense_transactions (transfer_transaction_id) WHERE transfer_transaction_id IS NOT NULL` | Leg pairing — and after WP1 this column *is* the transfer discriminator, so it is on the hot path |
  | `expense_transactions (reconciliation_id) WHERE reconciliation_id IS NOT NULL` | Listing a reconciliation's assigned transactions |
  | `expense_transaction_hashtags (hashtag_id)` | The report's hashtag breakdown joins the junction in this direction |

- **How to compute it without an N+1.** `GET /accounts` and `/dashboard` both list every
  account. One `SUM` per account is the classic N+1; it must be a single
  `GROUP BY account_id` joined onto the account list. An account with no transactions must
  produce `0`, not a missing row or `null` — check your join direction.
- **How the sign is applied.** After WP1 this is `transaction_type`; before WP1 it is the
  four-branch matrix. Either way, **reuse the existing signed-amount expression** rather
  than writing a third copy — duplicated sign matrices are already a known defect in this
  codebase.
- **What reconciliation does with the balance now.** It exists to check the ledger against
  a statement; today it compares against a cache that can itself be wrong. Confirm nothing
  in `app/helpers/reconciliations.py` depends on the stored column.
- **Whether the balance belongs in the same query as the account row or a separate one.**
  Either is defensible; the N+1 is the thing to avoid.
- **What the activity log does now.** `CLAUDE.md` records a deliberate exception exempting
  balance writes from activity logging. With no balance write, that exception has nothing
  to describe — confirm and remove it.

## Where to look

```bash
grep -rn "current_balance_cents\|apply_balance\|reverse_balance" app/ tests/   # ~36 sites
```

| File | Role |
|---|---|
| `app/helpers/balance.py` | 125 lines. Should not survive. |
| `app/helpers/accounts.py` | Account reads; where the computed balance has to appear |
| `app/routers/dashboard.py` | Lists every account — the N+1 risk |
| `app/helpers/transactions.py` | Create / update / delete / restore, each currently mutating the balance |
| `app/helpers/reconciliations.py` | Consumer of balances |
| `app/schemas/accounts.py` | The wire field that must not change |

## Invariants that must survive

- **Balance = opening entry + all non-deleted movements on that account.** Soft-deleted
  transactions are excluded; restoring one puts its effect back.
- Archived accounts still hold real money and still report a balance.
- Balances remain in the account's **native currency**. Home-currency balances are WP2's
  concern.
- Batch writes stay all-or-nothing.
- The wire shape of `current_balance_cents` does not change.

## Opening balances become load-bearing — test them

With a stored balance, an `@Opening` transaction was one input among many, and a wrong one
could be masked by the cached figure. **With a computed balance the opening entry is the
seed of the sum**: if it is wrong or missing, the account's balance is wrong by exactly
that amount, forever, on every screen.

This is an improvement — it makes an invariant that was always true finally honest — but it
means the `@Opening` path needs a test asserting that the computed balance equals opening
plus movements. It is now the foundation rather than a contributor.

Note also: `@Opening` is excluded from monthly reports but **included** in balances. Make
sure your sum does not inherit the report's exclusion.

## Definition of done

- [ ] `grep -rn "current_balance_cents" app/` finds only the response field, never a column
      read or write. Column dropped by migration.
- [ ] `app/helpers/balance.py` deleted.
- [ ] **The indexes ship in the same migration as the deletion.**
- [ ] `EXPLAIN` on the account-list query shows an index scan, not a sequential scan, and
      the query count does not grow with the number of accounts.
- [ ] A test asserts balance = opening + movements, including after a soft delete and a
      restore.
- [ ] A test asserts an account with zero transactions reports `0`.
- [ ] `pytest` green.
- [ ] **`CLAUDE.md`'s "Balance updates are atomic" convention replaced** with "balances are
      computed at read time, never stored" — the same sentence the currency model uses.
      Remove the ⏳ block. Remove the activity-log exception for balance writes if it is
      now vacuous.
- [ ] No `docs/client-breaking-changes.md` entry is needed for the balance itself — verify
      that is true before concluding it, and note the internal change somewhere.

## Out of scope

- `/sync` and the `(user_id, updated_at)` indexes — that is WP4, which **must land after
  this package**. Leave those indexes alone; WP4 removes them once yours exist.
- Currency conversion of balances (WP2).
- Re-caching the balance for performance. If it is ever needed, adding a cache back is
  safe precisely because you can recompute to verify it. Do not pre-optimise.
