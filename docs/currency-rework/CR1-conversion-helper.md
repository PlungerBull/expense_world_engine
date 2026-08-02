# CR1 — Conversion helper

**Prerequisites:** Phase 0 in [README.md](README.md). Read
[`../currency-model-decision.md`](../currency-model-decision.md) first.
**Blocks:** CR2, CR3, CR4, CR5. **Blocked by:** nothing.

---

## Goal

Create `app/helpers/home_currency.py` — the single place the native→home
conversion rule is expressed for **set-based reads** (reports, dashboards, list
endpoints). Nothing is wired to it in this package. The repo ends with **zero
behaviour change** and a green suite.

This exists so CR2 has one mechanism to adopt everywhere, instead of each call
site growing its own conversion.

---

## Why

The engine currently reads a *stored* `amount_home_cents` column. That column is
being deleted (CR3), so every read path needs a way to compute the value instead.
Doing that ad-hoc per call site is how the codebase ended up with four different
home-value mechanisms in the first place — see the decision doc's opening table.

---

## Background you need

**Rates are stored canonically as USD-based rows:**
`(base_currency='USD', target_currency=X, rate = units of X per 1 USD)`.

**Lookup carries forward.** `helpers/exchange_rate.py:67-77` resolves with
`rate_date <= $1 ORDER BY rate_date DESC LIMIT 1` — the most recent rate *on or
before* the date. Weekends and holidays need no special handling. The hard
requirement is one row on or before the earliest transaction date.

**Only two currencies exist.** `sql/015` locks the set to `{USD, PEN}`; `sql/018`
locks `user_settings.main_currency` to `PEN`. So home is always PEN, and the only
non-trivial conversion is USD→PEN, which is a direct `(USD, PEN)` row lookup. Do
**not** build cross-rate support — `get_rate` deliberately returns `None` for it
(`exchange_rate.py:98-103`).

---

## What to build

### `app/helpers/home_currency.py`

Three exports. Keep them as composable SQL string constants/builders so callers
interpolate them into existing queries rather than restructuring around them.

**1. `HOME_RATE_JOIN`** — a `LEFT JOIN LATERAL` that resolves one rate per
transaction row:

```sql
LEFT JOIN LATERAL (
    SELECT er.rate
    FROM exchange_rates er
    WHERE er.base_currency  = 'USD'
      AND er.target_currency = <home>
      AND er.rate_date <= t.date::date
    ORDER BY er.rate_date DESC
    LIMIT 1
) r ON a.currency_code <> <home>
```

Requires the caller's query to have `expense_transactions t` joined to
`expense_bank_accounts a`. Several current queries do not join the accounts table
— note in the docstring that CR2 must add that join.

**2. `HOME_CENTS_EXPR`** — the unsigned home value:

```sql
CASE
    WHEN a.currency_code = <home> THEN t.amount_cents
    WHEN r.rate IS NOT NULL       THEN round(t.amount_cents * r.rate)
    ELSE NULL
END
```

⚠️ **`NULL` is the missing-rate signal.** Never `COALESCE` it to
`t.amount_cents` — that is the existing bug being removed (it treats USD cents as
PEN cents, a 3.58× understatement rendered silently). See the decision doc,
"Missing-rate policy".

**3. `SIGNED_HOME_CENTS_EXPR`** — the signed form, applying the sign matrix:
income and transfer-credits positive, expenses and transfer-debits negative.

This replaces three duplicated copies of the CASE matrix
(`routers/dashboard.py:112-120`, `helpers/monthly_report.py:119-122` and
`:198-201`). Closes audit finding **WP9.1**, whose stated risk is that
`/dashboard` and `/reports/monthly` drift and disagree about the same month.

Also export the **native** signed expression (`SIGNED_CENTS_EXPR`) so both halves
of the matrix live in one module — `dashboard.py:102-110` and
`monthly_report.py:111-118` are the same duplication.

**Interpolate `app.constants` enum members, not bare `1/2/3`** (audit WP9.9). Use
`int(TransactionType.EXPENSE)` etc. so a renumbering can't silently desync the SQL.

---

## Accepted duplication — read this

`get_rate` in `helpers/exchange_rate.py` **stays**. It is still needed for:

- account balances, which convert at *today's* rate, not a transaction date
  (`helpers/accounts.py:26-53`)
- reconciliations (`schemas/reconciliations.py:107`)
- the `GET /exchange-rates` endpoint itself

So `get_rate` and `HOME_RATE_JOIN` express the same carry-forward rule in two
languages. **This is a real DRY violation and it is accepted deliberately** —
aggregates must run in SQL, and pulling every row into Python to convert would be
worse.

**It is also exactly how audit finding WP1.2 happened**: the dominant-side rule was
implemented twice, the copies disagreed, and the surviving copy was the buggy one.

**Mitigation is mandatory in this package:** a parity test (below). Do not skip it.

---

## Tests

New file, e.g. `tests/test_home_currency_parity.py`.

**Parity test.** For a matrix of dates and currencies, assert the SQL fragment and
`get_rate` produce the same result:

| Case | Expect |
|---|---|
| PEN → PEN, any date | rate 1.0, no table access |
| USD → PEN, date with an exact rate row | that row's rate |
| USD → PEN, date with **no** row but an earlier one exists | the earlier row (carry-forward) |
| USD → PEN, weekend / holiday date | carry-forward, no special case |
| USD → PEN, date **before the earliest row** (< 2024-03-02) | `NULL` from SQL, `None` from `get_rate` |
| A known real gap: **2025-12-10** (absent from the provider dataset) | carry-forward from 2025-12-09 |

Seed `exchange_rates` directly in the test — do not depend on the live table.

⚠️ `exchange_rates` has **no `user_id` column**, so its rows are global and cannot
be cleaned up per-user by the standard fixtures. This is precisely why the suite
was moved to a dedicated `expense_world_test` database (roadmap 11.8). Clean up
seeded rate rows explicitly in your fixture teardown.

**Rounding.** Assert the expression's rounding against the intended convention.
Note that SQL `round()` on `numeric` is half-away-from-zero while Python's
`round()` is banker's rounding — they disagree on exact `.5` cases. Audit finding
**1.7** wants `ROUND_HALF_UP` throughout; pick that, and make the parity test
assert it explicitly with a `.5` case so the discrepancy is pinned rather than
discovered later. Where Python-side rounding is involved, use `Decimal` with an
explicit `ROUND_HALF_UP` quantiser rather than the builtin.

---

## Done when

- [ ] `app/helpers/home_currency.py` exists exporting `HOME_RATE_JOIN`,
      `HOME_CENTS_EXPR`, `SIGNED_HOME_CENTS_EXPR`, `SIGNED_CENTS_EXPR`
- [ ] Every expression uses `app.constants` enum members, no bare integers
- [ ] Module docstring states: `NULL` means unconvertible and must never be
      coalesced; the caller's query must join `expense_bank_accounts a`; and the
      `get_rate` duplication + why the parity test exists
- [ ] Parity test covers all six matrix rows above, including the `.5` rounding case
- [ ] **Nothing else imports the new module yet** — this package wires nothing
- [ ] `pytest` green, and the test *count* is higher than before (only additions)
- [ ] `git diff` touches only the new module and the new test file

---

## Do not

- Wire this into `dashboard.py`, `monthly_report.py` or any schema — that is CR2
- Delete or modify `get_rate` / `lookup_exchange_rate` — CR3 handles the latter
- Drop any column — that is CR3
- Build cross-rate (non-USD ↔ non-USD) support — deliberately unsupported
- Add an `@FX` category — deferred, see D-d in [README.md](README.md)
