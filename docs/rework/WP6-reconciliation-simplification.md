# WP6 — Delete reconciliation chaining and shrink the largest helper

**Read [`README.md`](README.md) first.** Independent of every other package — run it whenever.

---

## What reconciliation is for

Name a period, record the beginning and ending balance from your statement, assign
transactions to it, and check that they add up. Complete it when they do; revert if you
were wrong.

That is the whole feature. Stated as the owner put it: *every reconciliation has a fixed
beginning and ending balance and we reconcile against them, nothing else.*

## What it currently costs

`app/helpers/reconciliations.py` is **1,066 lines across 13 functions — the largest helper
in the codebase**, for a feature with zero rows and a one-paragraph description.

The reason is mostly machinery this package removes, plus machinery other packages remove:

| Weight | Fate |
|---|---|
| The chaining cascade — `_cascade_chained_recalc` + 5 call sites, `_previous_chained_neighbor`, `_shift_sort_orders_at_or_above`, `_serialize_with_neighbor` (~90 lines) | **Deleted here** |
| `beginning_balance_source` (1 = manual, 2 = chained) | **Deleted here** |
| `resolve_home_rates` and the `*_home_cents` response fields | Deleted by WP2 |
| Sort-order management and bulk reorder | **Open question — see below** |
| CRUD, the draft→completed state machine, field locking, transaction assignment | **Keep** |

A reasonable end state is roughly 300–400 lines.

## Why chaining has to go

Chaining lets one reconciliation take its beginning balance from the previous one's ending
balance, cascading downstream when an upstream figure changes.

**The cascade has no status predicate.** Editing an upstream *draft* silently rewrites the
beginning balance of a *completed* reconciliation — doing through the back door exactly
what the completion field-lock refuses at the front. A completed reconciliation is a record
of what you checked and agreed; something that can rewrite it after the fact makes the
record worthless.

This is a `CLAUDE.md` "fix at the root" case. The fix is not adding a status check to the
cascade; it is recognising that a beginning balance is a fact you read off a statement, not
a value the engine should derive.

After this, every reconciliation's beginning balance is entered by the user, full stop.

## What is decided

- Delete `_cascade_chained_recalc` and its five call sites, `_previous_chained_neighbor`,
  `_serialize_with_neighbor`, and `expense_reconciliations.beginning_balance_source`.
- Beginning balance is always user-entered. There is no derived mode.
- The draft → completed → reverted state machine stays, including field locking on
  completion.
- Transaction assignment stays as it is: explicit, by `reconciliation_id`.

## The open question you must answer

**Does `sort_order` survive?**

Its stated purpose was twofold: per-account display ordering **and** the chain order. With
chaining gone, only the first remains — and you would be asking the user to hand-order a
list that has a natural chronological order.

Deleting it would also remove:

- `PUT /accounts/{id}/reconciliations/order` (a route)
- `_shift_sort_orders_at_or_above` and the renumber-in-one-transaction logic
- the `422` that rejects `sort_order` in the plain `PUT`

And it would give `date_start` / `date_end` an actual job. Today **both are pure labels** —
stored, echoed, editable, and *never used to select transactions*; assignment is by
explicit ID, not by date range. (`date_end` currently supplies the as-of date for home-rate
resolution, but WP2 removes that use.)

Investigate and decide. Either answer is acceptable if you record the reasoning:

- **Delete it** and order by date. Simpler, fewer moving parts, dates become meaningful.
  Needs a rule for reconciliations with null dates.
- **Keep it.** Defensible if you conclude the user genuinely wants arbitrary ordering, or
  if nullable dates make date-ordering unreliable.

If you keep it, note that `CLAUDE.md`'s collection-ordering convention applies in full:
per-scope `sort_order integer NOT NULL DEFAULT 0`, listed ASC, new rows append at `max+1`,
soft-deleted rows keep their slot and reclaim it on restore, cross-scope values are never
compared, and bulk reorder accepts any subset.

## A second question worth raising, not necessarily answering

Should `date_start` / `date_end` **select** the transactions rather than merely label the
period? Today assignment is entirely manual. Date-driven assignment would be a genuinely
different feature — likely better, definitely larger. **Do not build it in this package.**
Flag it as a product question if your investigation suggests it.

## What you must work out

- **What the field lock actually covers on completion**, and whether removing chaining
  leaves any path that can still mutate a completed record.
- **Whether reverting is symmetric** with completing — what it restores and what it leaves.
- **What happens to assigned transactions** when a reconciliation is soft-deleted and
  restored. `expense_transactions.reconciliation_id` is a real FK; confirm the lifecycle.
- **Whether the difference figure is computed or stored.** If it is stored anywhere, it is
  the same defect as WP3's balance and should be computed.
- **Whether `resolve_home_rates` still exists** by the time you run. If WP2 has not landed,
  note its missing `user_id` filter — selecting accounts by ID without scoping to the user
  is a tenancy defect under `CLAUDE.md`, not a tidiness one. Fix it or hand it to WP2, but
  do not leave it.

## Where to look

```bash
grep -rn "beginning_balance_source\|_cascade_chained_recalc\|_previous_chained_neighbor" app/ tests/
grep -rn "sort_order" app/helpers/reconciliations.py app/routers/
wc -l app/helpers/reconciliations.py     # 1066 as of 2026-08-04
```

| File | Role |
|---|---|
| `app/helpers/reconciliations.py` | Everything above |
| `app/routers/reconciliations.py` | 8 routes |
| `app/routers/accounts.py` | The bulk-reorder route, `PUT /accounts/{id}/reconciliations/order` |
| `app/schemas/reconciliations.py` | Wire shapes, including `beginning_balance_source` |
| `docs/engine-spec.md` | Chaining is specified in several sections — note them for WP7 |
| `tests/test_reconciliation_rules.py`, `tests/test_reconciliation_ordering.py` | The behaviour you are changing |

## Invariants that must survive

- **A completed reconciliation cannot be silently mutated.** That is the point of the whole
  package. After this, nothing anywhere may rewrite a completed record's balances.
- Every mutation still writes an activity-log row.
- Soft delete and restore still work, and a restored reconciliation is coherent.
- Reconciliation figures stay in the account's native currency.
- Batch/transactional atomicity is unchanged: a reorder, if it survives, renumbers inside
  one transaction.

## Definition of done

- [ ] `grep -rn "beginning_balance_source\|cascade" app/` returns nothing relevant; column
      dropped by migration.
- [ ] **A test proves editing one reconciliation cannot change another's balances**, in any
      status. This is the regression the package exists to prevent.
- [ ] The `sort_order` question is answered in writing — in your summary and, if it
      survives, in a comment explaining why.
- [ ] `app/helpers/reconciliations.py` is substantially smaller; report the before/after
      line count.
- [ ] `pytest` green, with chaining tests deleted rather than skipped.
- [ ] Entry appended to `docs/client-breaking-changes.md` if any wire field or route is
      removed.
- [ ] Note for WP7 which `engine-spec.md` sections you invalidated.

## Out of scope

- Home-currency values on reconciliations (WP2).
- The computed balance itself (WP3) — though this feature becomes more trustworthy once it
  compares against the ledger rather than a cache.
- Date-driven transaction assignment. Flag it; don't build it.
- Deleting the reconciliation feature. It stays. This is simplification, not removal.
