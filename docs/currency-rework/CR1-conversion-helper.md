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

**Lookup carries forward.** The resolution SQL lives in the private
`_fetch_rate_from_db` (`helpers/exchange_rate.py:40-103`), not in `get_rate` —
`get_rate` (`:106-137`) is a caching wrapper over it. The predicate is
`rate_date <= $2 ORDER BY rate_date DESC LIMIT 1` — the most recent rate *on or
before* the date — and it appears twice, at `:67-77` (USD→X) and `:83-93` (X→USD).
Weekends and holidays need no special handling. The hard requirement is one row on
or before the earliest transaction date.

**`get_rate` returns a tuple, and caches misses.** Its return type is
`Optional[tuple[float, date]]` (`exchange_rate.py:31`) — `(rate, actual_rate_date)`,
not a bare float. It caches on `(from, to, as_of)` with a 1-hour TTL and **caches
negative results too** (`:29`, `:124-136`). Both facts matter for the parity test
below.

**Only two currencies exist.** `sql/015` locks the set to `{USD, PEN}`; `sql/018`
locks `user_settings.main_currency` to `PEN`. So home is always PEN, and the only
non-trivial conversion is USD→PEN, which is a direct `(USD, PEN)` row lookup. Do
**not** build cross-rate support — `get_rate` deliberately returns `None` for it
(`exchange_rate.py:98-103`).

**Transaction dates are `timestamptz`, and the cast timezone is a decision.**
`expense_transactions.date` is `timestamptz NOT NULL` (`sql/003:79`), so a bare
`::date` resolves in the session `TimeZone` — which `app/db.py` never sets, so it
inherits the server default (currently `America/Lima`). That is machine-dependent
and must not ship.

**Decision: cast in the user's `display_timezone`**, the same zone
`compute_month_bounds` already uses to bucket months (`monthly_report.py:51-76`).
This keeps a transaction's rate date and its report month in agreement: a
transaction at `2026-03-31T23:00-05:00` is counted in March *and* priced at the
March 31 rate. Under UTC it would be counted in March but priced at April 1.

⚠️ Do not describe this as "matching what the write path does" — it does not, and
that claim was wrong in an earlier draft of this document. Writes resolve rates
from `body.date.date()` (`exchange_rate.py:207`, fed from `transactions.py:319`,
`:525`, `:1216`, `inbox.py:94`, `:205`), where `body.date` is an `AwareDatetime`
that Pydantic leaves at the *client's* offset. That offset is not recoverable from
a stored `timestamptz`, so read-time SQL cannot reproduce it. This is a deliberate
convention change; its user-visible consequence is CR2's expected-behaviour-change
**5** (near-midnight transactions may price on a different day than the stored
value did).

⚠️ **`display_timezone` is unvalidated user input.** It is `text NOT NULL DEFAULT
'UTC'` (`sql/002:22`) settable through `PUT /auth/settings` (`helpers/auth.py:162`)
with no IANA-name check. Two consequences for this fragment:

- **It must be a bind parameter, never interpolated** — interpolating it is SQL
  injection. This forces `HOME_RATE_JOIN` to be a **builder** taking the caller's
  placeholder — see "How `<home>` is resolved" below for how the index is chosen; it
  differs per caller, so do not hardcode one. `<home>` is still a safe literal
  because `sql/018` locks it; the timezone is not.
- **Python and SQL disagree on a bad value.** `compute_month_bounds:61-63` catches
  the bad zone and falls back to UTC; `AT TIME ZONE` raises and would 500. Note the
  divergence in the docstring and flag it for CR4's fail-closed sweep — validating
  `display_timezone` on write is the root fix and belongs there, not here.

---

## What to build

### `app/helpers/home_currency.py`

Five exports. Keep them as composable SQL string constants/builders so callers
interpolate them into existing queries rather than restructuring around them.

**How `<home>` is resolved.** It is a **SQL literal interpolated at module import
from a single named constant**, not a bind parameter — reusable fragments get
spliced into queries with differing positional `$N` numbering, so a `$N` baked into
them cannot work. Define it once (`HOME_CURRENCY = "PEN"`, in `app/constants.py` if
you add it there) and build every fragment from it. Interpolation is safe here
*only* because `sql/018:29-31` locks `user_settings.main_currency` to `PEN`: it is
a constant, not user input, and never reaches these strings from a request. Note in
the docstring that if that CHECK is lifted, these become builder functions taking
`home_currency`, and the `'USD'` guard below must be revisited at the same time.

**The timezone is the opposite case** — user-settable and therefore a bind
parameter, which is why `HOME_RATE_JOIN` is a builder. See the timezone note under
"Background you need" before writing it.

**1. `HOME_RATE_JOIN`** — a `LEFT JOIN LATERAL` that resolves one rate per
transaction row:

```sql
LEFT JOIN LATERAL (
    SELECT er.rate
    FROM exchange_rates er
    WHERE er.base_currency  = 'USD'
      AND er.target_currency = <home>
      AND er.rate_date <= (t.date AT TIME ZONE <tz_param>)::date
    ORDER BY er.rate_date DESC
    LIMIT 1
) r ON a.currency_code <> <home>
   AND a.currency_code = 'USD'
```

`<tz_param>` is the caller's **bind placeholder** for `display_timezone`, which is
why this export is a function — `home_rate_join("$4")` — rather than a constant.
Pass the caller's **next free positional index**, which differs per query: the
`monthly_report` queries already bind `$1..$3` (user_id, start_utc, end_utc) so the
timezone is `$4`, while the `dashboard` archived aggregators bind only `$1` so it is
`$2`. Do not hardcode an index in the module.

Callers already have the value: `get_user_report_settings`
(`monthly_report.py:36-47`) returns `main_currency` + `display_timezone` and is
already called by `dashboard.py:236` and `reports.py:122`. **Reuse it; do not add a
second settings loader.**

⚠️ **The second `ON` clause is not redundant — it is the fail-closed guard.** The
subquery hardcodes `base_currency = 'USD'` and never references `a.currency_code`,
so without it *any* non-home currency silently receives the USD→PEN rate. That is
correct only because `sql/015` locks the set to `{USD, PEN}` — a single `CHECK`
constraint. If a third currency is ever admitted, the guard makes it fall to the
`ELSE NULL` arm below (missing, flagged) instead of producing a confidently wrong
number. Do not "simplify" it away.

Requires the caller's query to have `expense_transactions t` joined to
`expense_bank_accounts a`. **The aliases `t` and `a` are part of the contract** —
export them as module constants and name them in the docstring, so CR2 and the
parity test embed the same scaffold rather than two. Several current queries do not
join the accounts table — note in the docstring that CR2 must add that join.

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

**It must wrap `HOME_CENTS_EXPR` textually** — reference the expression, do not
re-derive the multiplication with a sign inside it. This is a
single-definition rule, not a numeric one: Postgres `round(numeric)` is
half-away-from-zero, which is an **odd function**, so `round(-x * r)` and
`-round(x * r)` are equal for every input. Verified: 0 disagreements across 4001
exact-`.5` cases. Either order is numerically fine; wrapping is required so there
is one definition to change, matching the magnitude-then-round convention at
`transactions.py:314,320` (`abs()` then `round()`).

This replaces three duplicated copies of the CASE matrix
(`routers/dashboard.py:112-120`, `helpers/monthly_report.py:119-122` and
`:198-201`). Closes audit finding **WP9.1**, whose stated risk is that
`/dashboard` and `/reports/monthly` drift and disagree about the same month.

**4. `SIGNED_CENTS_EXPR`** — the **native** signed expression, so both halves of the
matrix live in one module. `dashboard.py:102-110` and `monthly_report.py:111-117`
are the same duplication.

**5. `UNCONVERTIBLE_FLAG_EXPR`** — `1` when a real transaction row has no home
value, else `0`:

```sql
CASE WHEN t.id IS NOT NULL AND (<HOME_CENTS_EXPR>) IS NULL THEN 1 ELSE 0 END
```

⚠️ **The `t.id IS NOT NULL` guard is required, not defensive.** `dashboard.py`'s
archived aggregators `LEFT JOIN` transactions so that categories and hashtags with
no transactions survive with zero totals (`:129-131`). On those rows `t.*` and `a.*`
are all `NULL`, so `HOME_CENTS_EXPR` falls to its `ELSE NULL` arm — indistinguishable
from a genuinely unconvertible row. Verified:

| row | home value | flag without guard |
|---|---|---|
| PEN transaction | 5000 | 0 |
| USD transaction, rate present | 17500 | 0 |
| USD transaction, **rate missing** | `NULL` | 1 ✓ |
| **no transaction at all** (empty category) | `NULL` | 1 ✗ |

Without the guard, every archived category with zero transactions reports
`spent_home_cents: null` instead of `0` — a false "unconvertible" that contradicts
the invariant the `LEFT JOIN` exists to preserve.

This is the **aggregate half of the missing-rate policy, and it is not optional.**
The decision doc (`currency-model-decision.md:219-221`) requires "null and flag" —
but a per-row `NULL` does not survive aggregation intact. It vanishes silently in
*both* shapes the codebase actually uses:

- `SUM(signed_home_cents)` (`monthly_report.py:149`) — `SUM` skips `NULL`s, so the
  total is quietly missing the unconvertible rows.
- `SUM(CASE WHEN signed_home_cents > 0 THEN signed_home_cents ELSE 0 END)`
  (`monthly_report.py:217` and `:219`, the home inflow/outflow totals) — `NULL > 0`
  is `NULL`, not true, so the row takes `ELSE 0` and is counted as **zero**. Note
  this shape cannot even fail loudly: a group where *every* row is unconvertible
  returns `0`, not `NULL`.

Either way the result is a silently understated total: the same failure class as
the `COALESCE` fallback this package removes, relocated from the row layer to the
aggregate layer.

⚠️ **Scope constraint — project the flag inside the CTE.** Both `monthly_report`
queries aggregate in an outer `SELECT ... FROM signed_txns` over a CTE
(`:107-151`, `:188-220`). The aliases `a` and `r` exist only *inside* that CTE, so
interpolating `UNCONVERTIBLE_FLAG_EXPR` into the outer `SUM` is a hard SQL error.
It must be selected as a column within the CTE (e.g. `… AS is_unconvertible`) and
summed by name outside. Say this in the docstring — it is the first thing an
implementer will get wrong.

CR2 §2 already specifies the policy (`COUNT(*) FILTER (WHERE <home expr> IS NULL)`,
`unconverted_count`, category-level `null`), but without an export it hand-rolls
that predicate at four call sites — the two `monthly_report` queries and both
`dashboard` archived-lifetime aggregators. That is exactly the per-call-site
divergence this package exists to prevent. Export it once.

**Docstring must state the aggregation contract:** every `SUM` of a home expression
is paired with `SUM(UNCONVERTIBLE_FLAG_EXPR)`, and any non-zero count makes the
aggregate `null` rather than a partial total. Note explicitly that the
inflow/outflow totals need this too, not just the category breakdown.

Also record what the flag does **not** cover: it measures convertibility, not
classifiability. The `ELSE 0` arm of the sign matrix silently drops a row with an
out-of-range `transaction_type` from both native and home totals without raising
the flag. Pre-existing, and near-unreachable now that the expressions interpolate
`app.constants`, but it is the one remaining silent-drop in these fragments.

**Interpolate `app.constants` enum members, not bare `1/2/3`** (audit WP9.9). Use
`int(TransactionType.EXPENSE)` etc. so a renumbering can't silently desync the SQL.
The `IntEnum` values in `app/constants.py:33-41` already match the literals exactly,
so this is a zero-behaviour change.

---

## Accepted duplication — read this

`get_rate` in `helpers/exchange_rate.py` **stays**. It is still needed for:

- account balances, which convert at *today's* rate, not a transaction date
  (`helpers/accounts.py:26-53`) — the only genuine today's-rate consumer
- the account list endpoint, which uses `batch_get_rates` to avoid the N+1 that
  helper would cause in a loop (`routers/accounts.py:84`)
- reconciliations (`helpers/reconciliations.py:78`) — note these convert at the
  reconciliation's `date_end`, **not** today (`:89`), so they are already a
  historical-date consumer. `schemas/reconciliations.py:107` is only the
  `round(begin * rate)` arithmetic against a rate passed in as an argument; it
  performs no lookup.
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
`get_rate` resolve the **same rate row**. Compare rates, not converted cents — see
"Rounding" below for why.

⚠️ **Define `as_of` deliberately, or this test measures the wrong thing.** SQL now
resolves its date as `(t.date AT TIME ZONE <tz>)::date`, so the Python call
`get_rate(conn, "USD", "PEN", as_of)` must derive `as_of` the *same* way — and
`row["date"].date()` does **not**, because asyncpg hands back UTC. Reproducing the
conversion in Python would mean re-implementing the thing under test.

Sidestep it instead: **seed every fixture transaction at midday in the test's
`display_timezone`**, far enough from midnight that no plausible zone shifts the
calendar day, and derive `as_of` as that same local date. Timezone handling then
cannot be what makes a row pass or fail, and the test measures rate resolution — its
actual subject.

Note the test user's `display_timezone` is **`'UTC'`**: `conftest.py:90-92` inserts
`user_settings` with only `user_id`, so the column takes its `sql/002:22` default.
Midday is therefore `12:00Z`.

**Then pin the timezone decision in a separate test — and read this before writing
it, because the obvious version cannot fail.** "A 23:00 transaction resolves the
local day's rate" is *vacuous* for a UTC user: `23:00Z` is 2010-06-15 under
`AT TIME ZONE 'UTC'`, under a hardcoded UTC cast, and under the bare `::date` this
section exists to eliminate. All three agree, so the test would pass against the
bug it is meant to catch.

The case only discriminates with a **non-UTC zone** and an instant **after UTC
midnight but before local midnight**:

```sql
('2010-06-16T02:00:00Z' AT TIME ZONE 'UTC')::date          → 2010-06-16
('2010-06-16T02:00:00Z' AT TIME ZONE 'America/Lima')::date → 2010-06-15   ← the assertion
```

So: set `user_settings.display_timezone` to a non-UTC zone for the duration, seed
different rates on the two adjacent days, and assert the **earlier** local day's
rate is chosen. Restore the zone in a `finally`. Mutating it is safe — the test user
is worker-local (`conftest.py:64,69`) and xdist runs a worker's tests sequentially.

A test that cannot fail is worse than no test: it advertises coverage the fragment
does not have.

| Case | Expect |
|---|---|
| PEN → PEN, any date | home value equals native; the rate join contributes nothing |
| USD → PEN, date with an exact rate row | that row's rate |
| USD → PEN, date with **no** row but an earlier one exists | the earlier row (carry-forward) |
| USD → PEN, weekend / holiday date | carry-forward, no special case |
| USD → PEN, date before the earliest row **in your seeded range** | SQL and `get_rate` agree — whatever they resolve, they resolve the same thing |
| A gap date: a seeded range with one day deliberately omitted | carry-forward from the preceding day |

⚠️ Row 1 is asserted differently from the rest. The fragment yields `r.rate = NULL`
for a PEN row and returns `t.amount_cents` via the first `CASE` arm — it never
produces a rate of `1.0` the way `get_rate` does. Assert the home *value* equals the
native amount, not a rate.

⚠️ Row 5 is deliberately **not** phrased as "expect `NULL`" — see the query-date
constraint below. Absolute `NULL` belongs in its own test, not in the parity matrix.

⚠️ **Clear the `get_rate` cache in the fixture.** It caches misses for an hour
(`exchange_rate.py:29`, `:124-136`). A test that queries a date, seeds a row, then
queries again gets the stale `None` and fails for the wrong reason. Also remember
`get_rate` returns `(rate, actual_rate_date)` — compare `result[0]`.

Seed `exchange_rates` directly in the test — do not depend on the live table. Rows
5–6 are therefore written against *your own seeded range*, not against production
facts. (The production floor is 2024-03-02 and the provider dataset has real gaps,
but neither holds in `expense_world_test`, whose only guaranteed row is conftest's
`CURRENT_DATE` seed. An assertion phrased against production dates would be
testing the fixture, not the fragment.)

⚠️ `exchange_rates` has **no `user_id` column**, so its rows are global and cannot
be cleaned up per-user by the standard fixtures. This is precisely why the suite
was moved to a dedicated `expense_world_test` database (roadmap 11.8). Three
consequences:

- `conftest.py:142-146` **already seeds a global `USD→PEN @ CURRENT_DATE = 3.4`**
  with `ON CONFLICT DO NOTHING`. Do not assume an empty table and do not delete
  that row.
- The suite runs under xdist (`pytest.ini:9` — `-n 4 --dist loadfile`) and global
  rows are shared across workers. Delete only your own dates in teardown — never
  `DELETE FROM exchange_rates`.
- ⚠️ **Seed strictly inside 2001-01-01 … 2020-12-31.** Other tests depend on the
  *absence* of rates outside that window, so a seed in the wrong place breaks them
  rather than you:
  - `test_phase_fixes.py:270-286` posts a transaction dated **2000-01-01** and
    asserts `422 RATE_UNAVAILABLE`, relying on no rate existing on or before that
    date. Seed anything ≤ 2000-01-01 and it starts returning 201 on another worker.
  - `test_exchange_rates_history.py:7,21` establishes a "far-past synthetic dates"
    convention at 1997-01-xx. **Do not follow it** — that convention is what walks
    an implementer into the trap above.
  - Stay away from `CURRENT_DATE` too; `conftest.py:142-146` owns it.
- **Clean up fixture transactions, not just rate rows.** `dashboard.py`'s archived
  aggregators are lifetime-unbounded (no date filter), so a stray transaction left
  behind can shift another test's totals. Low risk — they filter
  `is_archived = true` — but delete what you create.
- ⚠️ **Constrain the dates you *query*, not just the ones you seed.** Carry-forward
  matches *any* earlier row in a global table, so "no rate before date X" is not a
  property you control. `tests/test_exchange_rates_history.py:22-32` seeds global
  `USD→PEN` rows at **1997-01-14 (3.50)** and **1997-01-15 (3.51)** from a
  function-scoped fixture; `--dist loadfile` pins that *file* to one worker while
  yours runs concurrently on another. So any absolute `NULL` assertion at a date
  after 1997 will intermittently resolve 3.50 instead.

  Handle it in two parts:

  1. **The parity rows assert agreement, not absolute values.** That is the test's
     actual job — SQL and `get_rate` must resolve the *same* row, whatever it is.
     A concurrent insert moves both together and parity still holds. Do not phrase
     any parity row as "must be `NULL`".
  2. **One dedicated test for the `NULL` path** may assert absolutely, but only by
     *querying* a date earlier than every row any test seeds. `1990-01-01` is safe
     today — the earliest seeded row in the whole suite is
     `test_exchange_rates_history`'s 1997-01-14. State in a comment that this is a
     suite-wide floor, so a future test seeding earlier knows it breaks this one.
     (Querying below the floor is fine; *seeding* below it is what the window rule
     above forbids.)

**Rounding.** Assert the expression's rounding **in SQL only**: Postgres `round()`
on `numeric` is half-away-from-zero. Do *not* write a test trying to catch a
`round(-x*r)` vs `-round(x*r)` discrepancy — half-away-from-zero is an odd
function, so no input produces one.

The discrepancy that **is** real, and worth a comment even though fixing it is out
of scope: `_fetch_rate_from_db` returns `float(row["rate"])`
(`exchange_rate.py:80`), truncating the stored `numeric` to binary float, after
which Python's builtin `round()` applies banker's rounding. The SQL fragment keeps
full `numeric` precision and rounds half-away-from-zero. So SQL and Python can
differ by one cent on the *converted amount* even when they agree on the rate.

That is precisely why the parity test compares **rates, not converted cents.**
Do not "strengthen" it into a cents comparison — it would fail, and the fix is
forbidden here: audit finding **1.7** wants `Decimal` + `ROUND_HALF_UP`
throughout, but `README.md` defers that to after CR5 ("Remaining WP1.7 rate
hygiene") and this package's "Do not" forbids touching `get_rate`. Leave a
docstring note pointing at WP1.7 so the gap is recorded rather than rediscovered.

---

## Done when

- [ ] `app/helpers/home_currency.py` exists exporting `HOME_RATE_JOIN`,
      `HOME_CENTS_EXPR`, `SIGNED_HOME_CENTS_EXPR`, `SIGNED_CENTS_EXPR`,
      `UNCONVERTIBLE_FLAG_EXPR`, and the required table aliases
- [ ] `HOME_RATE_JOIN` is a **builder** taking the caller's `display_timezone`
      placeholder, carries the `a.currency_code = 'USD'` fail-closed guard, and
      casts as `(t.date AT TIME ZONE <tz_param>)::date`
- [ ] The timezone is bound, never interpolated; `<home>` is interpolated from the
      single `HOME_CURRENCY` constant
- [ ] `SIGNED_HOME_CENTS_EXPR` wraps `HOME_CENTS_EXPR` by reference rather than
      re-deriving the multiplication
- [ ] `UNCONVERTIBLE_FLAG_EXPR` carries the `t.id IS NOT NULL` guard, and is
      documented as CTE-projected rather than outer-SELECT interpolated
- [ ] Every expression uses `app.constants` enum members, no bare integers
- [ ] Module docstring states: `NULL` means unconvertible and must never be
      coalesced; the aggregation contract (every `SUM` paired with the
      unconvertible count, non-zero count ⇒ `null` not a partial total); the
      caller's query must join `expense_bank_accounts a` using the contracted
      aliases; rate dates resolve in the user's `display_timezone` and why that
      diverges from the write path; and the `get_rate` duplication + why the parity
      test exists
- [ ] Parity test covers all six matrix rows above. Rows 2–6 assert SQL/`get_rate`
      **agreement** rather than absolute values; row 1 (PEN→PEN) instead asserts
      the home value equals the native amount
- [ ] Fixture transactions are seeded at **midday** so timezone handling cannot
      decide a parity row
- [ ] The near-midnight test uses a **non-UTC** `display_timezone` and an instant
      after UTC midnight — verify it *fails* against a hardcoded-UTC cast before
      accepting it, since the UTC-user version is vacuous
- [ ] A separate test pins the `NULL` (unconvertible) path at a date below the
      suite-wide seed floor
- [ ] Parity test clears the `get_rate` cache and cleans up only its own seeded
      rate dates
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
