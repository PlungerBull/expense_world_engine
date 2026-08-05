# WP2 — Convert currency at read time; delete every stored derived value

**Read [`README.md`](README.md) first. Requires WP1 to have landed.**

> **Why the dependency is real.** `app/helpers/home_currency.py` — the module you are
> wiring in — contains a `_signed()` sign matrix with four branches keyed on
> `transaction_type = 3` and `transfer_direction`. WP1 collapses that to two branches on
> `transaction_type` alone and makes its `ELSE 0` arm unreachable. Wire this module before
> WP1 lands and you will rewrite it immediately. **Check that `transfer_direction` is gone
> from `app/` before you start.**

---

## The rule, in one sentence

**Currency conversion is a lookup of the rate for that row's date, and nothing else.**

Nothing derived is stored. Change the rate table and every past report corrects itself.
Where no rate exists, the figure is `null` plus a count of unconverted rows — never a
native amount substituted for a home amount.

`docs/currency-model-decision.md` is the authority on this and survives the rework. Read
it before you start; it explains *why* a stored conversion never held a fact, and what
`@Transfer ≠ 0` means.

## The problem

Three columns store a conversion frozen at write time:

| Column | Why it's wrong |
|---|---|
| `expense_transactions.amount_home_cents` | A derived value with a second source of truth. Root cause of open bugs 1.4 and 1.5. |
| `expense_transactions.exchange_rate` | Never held a fact — for a cross-currency transfer it is literally `sibling.amount_cents ÷ primary.amount_cents`. |
| `expense_transaction_inbox.exchange_rate` | Defaults to `1.0`, which is how a $100 draft promotes as 100 PEN cents (open bug 1.4). |

And the read path compensates with a fallback that makes it worse:

```sql
COALESCE(t.amount_home_cents, t.amount_cents)
```

**This relabels rather than converts.** A USD row with no stored home value is read as
though its dollar cents were sol cents. It is live in **12 places** (8 in
`app/helpers/monthly_report.py`, 4 in `app/routers/dashboard.py`) as of 2026-08-04.

It is **latent, not currently firing** — `lookup_exchange_rate` raises `RATE_UNAVAILABLE`
(422) rather than falling back to `1.0`, so the write path refuses to create a row it
cannot rate, and the column is never null in practice. Do not let that make you
complacent: the hazard is the shape. A fallback whose job is to hide missing data behind a
plausible number is precisely what `CLAUDE.md`'s **fail closed** rule forbids.

**Deleting `amount_home_cents` resolves all 12 sites by construction** — the SQL cannot
run once the column is gone. That is the intended forcing function.

## What is decided

- Drop `amount_home_cents` and `exchange_rate` from `expense_transactions`; drop
  `exchange_rate` from `expense_transaction_inbox`.
- **The write path stops doing currency work entirely.** No rate lookup, no multiplication,
  no stored result. Recording what happened must never be blocked by a rate lookup — which
  also means cross-currency writes stop failing when the FX job is stale, and transactions
  dated before the provider floor (2024-03-02) become recordable.
- Conversion moves to the read path via `app/helpers/home_currency.py`, which already
  implements exactly the right thing: a `LEFT JOIN LATERAL` taking the newest rate on or
  before the row's date, a `CASE` returning the native amount for home-currency rows and
  `NULL` otherwise, and `UNCONVERTIBLE_FLAG_EXPR` so a missing rate is counted rather than
  hidden.
- **Home-currency values appear only on figures the user compares or sums across
  currencies** — never on individual records. `CLAUDE.md`'s "Home currency" section has the
  authoritative table. In short: monthly report per category and per hashtag, month totals,
  dashboard archived panels, account balances. **Individual transactions and inbox items
  carry no home value at all.**
- **A per-row `null` is not sufficient on its own.** `SUM` silently skips nulls, and
  `SUM(CASE WHEN x > 0 THEN x ELSE 0 END)` scores a null row as zero — an unflagged
  aggregate understates exactly like the fallback it replaced. Every home-value `SUM` must
  be paired with a count of unconvertible rows, and a non-zero count makes the aggregate
  `null` rather than a partial total.

## What you must work out

- **Whether `lookup_exchange_rate` survives at all.** If nothing on the write path needs a
  rate, find out what else calls it before deleting it.
- **What happens to `resolve_home_rates` in `app/helpers/reconciliations.py`.** It supplies
  home values for reconciliation balances using `date_end` as the as-of date, and those
  response fields are being removed. Note it also has a genuine defect: it selects accounts
  by ID **with no `user_id` filter**, which under `CLAUDE.md`'s tenancy rule is a security
  defect, not a tidiness one. It is reachable from `/reconciliations` and, until WP4,
  from `/sync`. Deleting it closes that; if any part survives, the filter must be added.
- **Which response fields disappear**, precisely. Candidates identified by the audit:
  `amount_home_cents` on transaction and inbox responses; `current_balance_home_cents` on
  accounts; the reconciliation `*_home_cents` fields; and the *native* cross-account
  aggregates (`spent_cents`, `inflow_cents` / `outflow_cents` / `net_cents`), which are
  meaningless as sums — a category spanning both currencies yields `$15 + S/25 = 4000`, a
  number in no currency. Delete them rather than nulling them. Verify each against the
  actual schemas before removing.
- **The dashboard's archived category/hashtag panels.** The audit recommends deleting them
  (their only remaining consumer is `is_archived`, which WP5 removes from those tables).
  Confirm the coupling and decide. `archived_accounts` **stays** — an archived account
  still holds real money, unlike an archived category which holds only history.
- **What `SIGNED_CENTS_EXPR` is still for.** It loses its last aggregate caller here but is
  the basis of `UNCONVERTIBLE_FLAG_EXPR`. Do not garbage-collect it without checking.
- **Read-time timezone handling.** `home_rate_join` takes the user's `display_timezone` as
  a **bind parameter**, and its placeholder index differs per query. `display_timezone` is
  unvalidated user input (settable via `PUT /auth/settings`) — it must never be
  interpolated into SQL. Reuse the value from `monthly_report.get_user_report_settings`
  rather than adding a second settings loader.

## Where to look

```bash
grep -rn "amount_home_cents\|exchange_rate" app/ tests/
grep -rn "COALESCE(t.amount_home_cents" app/          # 12 sites
grep -rn "home_currency" app/                          # currently imported by nothing
```

| File | Role |
|---|---|
| `app/helpers/home_currency.py` | The replacement. ~284 lines, roughly 80% commentary explaining the reasoning. Read the module docstring in full — it documents the aggregation contract and the caller requirements. |
| `app/helpers/monthly_report.py` | 8 `COALESCE` sites. `compute_month_flow` is the single source of truth shared with the dashboard. |
| `app/routers/dashboard.py` | 4 `COALESCE` sites. |
| `app/helpers/transactions.py` | Write-path rate lookup and `amount_home_cents` computation, in both the normal and promote paths. |
| `app/helpers/exchange_rate.py` | `lookup_exchange_rate`, `get_rate`, and the carry-forward read semantics. |
| `tests/test_home_currency_parity.py` | The only current importer of `home_currency.py`. |

## Invariants that must survive

- **The engine is the only thing that does currency conversion. Clients never compute it.**
  That part is absolute.
- Rates are stored canonically USD-based; `X→USD` is handled by inversion; non-USD↔non-USD
  returns nothing (unreachable under the two-currency lock in `sql/015`).
- **Carry-forward is the read semantic**: the most recent rate on or before the requested
  date. Coverage density buys accuracy, not availability.
- The fail-closed guard in `home_rate_join`'s second `ON` clause is **not redundant** — the
  subquery hardcodes `base_currency = 'USD'` and never references the account's currency,
  so without it a third currency would silently receive the USD→PEN rate. Its docstring
  says "do not simplify it away". Don't.
- `main_currency` stays as the chokepoint it is, even though `sql/018` locks it to `'PEN'`.
- Reports still exclude `@Opening` and still include transfers.

## Definition of done

- [ ] `grep -rn "amount_home_cents" app/` returns nothing; the three columns are dropped by
      migration.
- [ ] `grep -rn "COALESCE(t.amount_home_cents" app/` returns nothing.
- [ ] `home_currency.py` is imported by the read paths — it is no longer dead code.
- [ ] The write path performs no rate lookup. A cross-currency transaction can be created
      when the FX table has no row for its date.
- [ ] A test proves an unconvertible row produces `null` **plus** a non-zero
      `unconverted_count`, and never a partial total.
- [ ] A test proves `/dashboard` and `/reports/monthly` agree about the same month. They
      share `compute_month_flow`; if they can disagree, the duplication that caused it is
      still there.
- [ ] `pytest` green.
- [ ] Open bugs **1.4** and **1.5** deleted from `docs/open-bugs.md`.
- [ ] `docs/currency-model-decision.md` updated if anything you decided contradicts it.
- [ ] Entry appended to `docs/client-breaking-changes.md` listing every removed response
      field.

## Out of scope

- The `Decimal` / `ROUND_HALF_UP` rounding cleanup noted in `home_currency.py`. SQL and
  Python can differ by one cent on a converted amount, which is why the parity test
  compares rates rather than cents. Deliberately unscheduled.
- Adding a third currency. `sql/015`'s CHECK stays.
- `current_balance_cents` itself — that is WP3. You are removing the *home* value on
  account balances; WP3 removes the stored native balance. If both land, the account
  balance is computed and native-only.
