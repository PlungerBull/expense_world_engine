# WP1 — Collapse `transaction_type` into a direction

**Read [`README.md`](README.md) first.** Blocks WP2. Depends on nothing.

> ## This is the only package with a deadline
>
> This is not a deletion — it is a change to how every ledger row is encoded. Today it is
> pure code: the ledger holds **zero rows**, so there is nothing to rewrite and no
> dual-shape window. After the first real transaction lands it means rewriting every row
> *and* every report *and* writing a backfill that has to infer direction from a column
> you are removing. **Do this before the engine is used.**

---

## The problem

`transaction_type` is carrying two facts that have nothing to do with each other:

1. **Which way money moved on this account** — in or out. Values `1` (expense) and `2`
   (income) encode this.
2. **Who the counterparty was** — value `3` (transfer) says the other side is an account
   you also own.

Because `3` occupies a slot in what is otherwise a direction column, direction for
transfers had to be exiled into a *second* column, `transfer_direction` (1 = debit,
2 = credit), which is only meaningful when the first column holds one specific value.

Two columns encoding one concept, only one valid at a time. Everything downstream pays
for it:

- **Report logic needs four branches to express two outcomes.** Every signed-amount
  expression in the codebase reads
  `WHEN type = 2 OR (type = 3 AND dir = 2) THEN +x WHEN type = 1 OR (type = 3 AND dir = 1) THEN -x`.
- **A directionless transfer leg is representable.** `transfer_direction` is nullable with
  nothing tying it to `type = 3`. Such a row moves an account balance and is invisible in
  every report, because it matches none of the four branches and falls to `ELSE 0`.
- **The inbox needs a parallel triple** of transfer columns to mirror the encoding.
- **Deleting one leg is ambiguous**, because "leg" is a property of a column value rather
  than of the pairing relationship.

The decisive observation: **every `type = 3` row carries a direction, and direction
already means in/out.** So `type = 3` tells you nothing about direction that
`transfer_direction` doesn't already say. Its only remaining information is "the
counterparty is an account you own" — which `transfer_transaction_id IS NOT NULL` already
says, using a column that already exists.

## What is decided

Do not relitigate these. They are the point of the package.

- `transaction_type` becomes **1 = outflow, 2 = inflow**. Present on every row, never
  null, no third value. Add a `CHECK` — `expense_transactions.transaction_type` currently
  has none at all (that is open bug 6.3, which this closes).
- **`transfer_direction` is deleted** from `expense_transactions` *and*
  `expense_transaction_inbox`. It is redundant: direction now lives in one column on every
  row.
- **A transfer is identified by `transfer_transaction_id IS NOT NULL`.** It becomes the
  discriminator, so it moves onto the hot path — WP3 adds its index.
- `infer_transfer_direction` in `app/schemas/transactions.py` is deleted.
  `infer_transaction_type` becomes the **single** place in the engine where a sign is read.
- A transfer remains two paired rows: an outflow on account A, an inflow on account B,
  each stored positive, each in its own account's currency.

## What you must work out

The above is the destination. How to get there is yours to determine — explore before
deciding.

- **How the pair is created.** Find `create_transfer_pair` (in `app/helpers/transactions.py`
  as of 2026-08-04) and work out what its inputs mean once direction comes from the sign of
  the request amount. The existing same-sign guard exists to catch "two outflows" — decide
  what enforces that invariant now.
- **How promotion builds a pair.** The inbox stores a primary amount plus a transfer
  triple. Migration `sql/019` is worth reading in full before you touch this: it documents
  exactly which bug arose last time the inbox's encoding diverged from the ledger's. Its
  `inbox_transfer_fields_coherent` CHECK currently requires
  `transfer_direction IN (1,2) AND transaction_type = 3` and must be rewritten to require
  the transfer pair plus `transaction_type IN (1,2)`. **The constraint must stay
  fail-closed** — a half-transfer row must remain unrepresentable in the database, not
  merely guarded in Python.
- **Category assignment.** Transfers auto-assign `@Transfer`, or `@Debt` when one leg is a
  person account. Confirm that logic still keys off something that survives.
- **Deleting or restoring one leg.** Today's behaviour needs to be established before you
  change it. If the correct policy is ambiguous, implement the fail-closed option (refuse,
  or act on both legs atomically) and flag the choice in your summary rather than picking
  silently.
- **Whether `TransferDirection` in `app/constants.py` still has any caller** after the
  sweep.

## Where to look

Verify all of this — the counts were taken on 2026-08-04 and will drift.

```bash
grep -rn "transfer_direction" app/ tests/          # ~63 hits in app/ alone
grep -rn "transaction_type = 3\|transaction_type == 3\|TransactionType.TRANSFER" app/
```

| File | Why it matters |
|---|---|
| `app/schemas/transactions.py` | `infer_transaction_type`, `infer_transfer_direction`, and the wire shape that exposes both fields |
| `app/helpers/transactions.py` | Pair creation, update, delete, restore |
| `app/helpers/inbox.py` | Promotion — the path `sql/019` was written to fix |
| `app/helpers/monthly_report.py`, `app/routers/dashboard.py` | The four-branch sign CASE, duplicated |
| `app/helpers/home_currency.py` | `_signed()` — the same matrix again, and **WP2 depends on this being collapsed** |
| `app/constants.py` | `TransactionType`, `TransferDirection` |
| `sql/019_inbox_transfer_direction.sql` | The CHECK you are rewriting, and the history of why it exists |

## Invariants that must survive

- `amount_cents` is **always stored positive**. Sign lives in a typed column, never in a
  value. This package strengthens that rule; it must not weaken it anywhere.
- Requests stay signed — the caller sends a negative amount for an outflow and the engine
  infers the type. **Callers never set `transaction_type` manually.**
- Responses stay positive, with `transaction_type` carrying direction and
  `?debit_as_negative=true` remaining a caller-side display preference.
- **The two legs of a transfer still cancel.** In native currency for same-currency
  transfers; in home currency the sum is non-zero only for FX spread or a person leg —
  see `docs/currency-model-decision.md` on what `@Transfer ≠ 0` means.
- **Transfers stay visible in reports and dashboards.** Never exclude them from totals.
  Under the new encoding the legs cancel naturally in net, which is the point.
- Balance updates remain atomic (that convention is live until WP3).

## Definition of done

- [ ] `grep -rn "transfer_direction" app/` returns nothing.
- [ ] No code path can produce a ledger row whose direction is unknown — demonstrate this,
      ideally with a `CHECK`, not a comment.
- [ ] `sql/019`'s coherence CHECK rewritten and still fail-closed; a half-transfer inbox row
      is still rejected by the database.
- [ ] `expense_transactions.transaction_type` has a `CHECK` (closes open bug 6.3).
- [ ] **Open bug 1.3 (every USD→USD transfer returns 500) has a regression test that fails
      before your change and passes after.** It lives in the write path you are rewriting
      and is expected to fall out — prove it rather than assuming it, and if it does not,
      say so.
- [ ] A test asserts a transfer's two legs cancel, and a test asserts every row has a
      direction.
- [ ] `pytest` green. Tests asserting the three-value encoding are rewritten deliberately,
      not loosened.
- [ ] **`CLAUDE.md`'s sign convention rewritten** — remove the ⏳ note, drop
      `infer_transfer_direction`, and state that `transaction_type` is direction on every
      row and a transfer is identified by its pairing FK.
- [ ] Entry appended to `docs/client-breaking-changes.md`. This is a real break: the CLI
      must stop reading `transfer_direction` and stop expecting `transaction_type = 3`.
      It detects transfers via `transfer_transaction_id != null` instead.
- [ ] Delete bug 6.3 and (if proven) 1.3 from `docs/open-bugs.md`.

## Out of scope

- Anything currency-related — no touching `amount_home_cents`, `exchange_rate`, or the
  conversion. **That is WP2, and it depends on you.** Collapse the sign matrix in
  `home_currency.py`; do not wire that module into anything.
- `current_balance_cents` (WP3). Keep balance updates atomic as they are today.
- The `is_person` / `@Debt` product question. The person-leg branch must keep working
  exactly as it does now; you are re-encoding direction, not deciding whether people exist.
