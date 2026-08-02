# CR2 — Read paths compute home values

**Prerequisites:** CR1 merged. Read
[`../currency-model-decision.md`](../currency-model-decision.md) first.
**Blocks:** CR3. **Blocked by:** CR1.

---

## Goal

Switch every read path from *reading* `amount_home_cents` / `exchange_rate` to
*computing* the home value with CR1's `home_currency` helpers. The columns still
exist after this package — they are simply ignored on read. CR3 deletes them.

This is the expand half of an expand/contract migration. **This is the package
where behaviour changes**, so read the "Expected behaviour changes" section before
touching a test.

---

## Why

The stored columns are being deleted. Reads must stop depending on them first, so
the drop in CR3 is a no-op for readers rather than a breakage.

---

## Files

| File | What changes |
|---|---|
| `app/helpers/monthly_report.py` | 8 `COALESCE(t.amount_home_cents, …)` arms at `:119-122` and `:198-201` → `SIGNED_HOME_CENTS_EXPR` + join. Add unconverted-row counting. |
| `app/routers/dashboard.py` | `_SIGNED_HOME_CENTS_SQL` at `:112-120` → import from `home_currency`. Both archived-lifetime aggregators (`:133-208`) use it, each needing a **`LEFT JOIN`** to accounts and a new `display_timezone` parameter — see step 1. |
| `app/schemas/transactions.py` | `transaction_from_row:105` reads `row["amount_home_cents"]` — must come from the query, not the column |
| `app/schemas/inbox.py` | `:70,79` multiply by the stored rate — same treatment |
| `app/helpers/sync.py`, `app/routers/sync.py` | transaction + inbox payloads carry computed home values |
| `app/helpers/formatting.py` | verify null-guards; fix the WP10.2 inbox transfer-leg flip while here |

---

## Steps

### 1. Aggregates (`monthly_report.py`, `dashboard.py`)

Neither file joins accounts today, so `HOME_RATE_JOIN` has no `a` to reference.
**The join you add is not the same in both files — read this before copying it.**

- **`monthly_report.py`** starts `FROM expense_transactions t`, so an inner join is
  correct:
  `JOIN expense_bank_accounts a ON a.id = t.account_id AND a.user_id = t.user_id`
- ⚠️ **`dashboard.py` must use `LEFT JOIN`.** Both archived aggregators start from
  the category/hashtag table and `LEFT JOIN expense_transactions`
  (`:141-144`, `:176-197`) precisely so empty rows survive — the invariant is
  documented at `:129-131`: *"Categories with no transactions ever appear with zero
  totals — the LEFT JOIN preserves them."* An **inner** join to
  `expense_bank_accounts` on `t.account_id` re-filters those rows away and every
  archived category or hashtag with no transactions silently vanishes from the
  `include_archived` panels. Use
  `LEFT JOIN expense_bank_accounts a ON a.id = t.account_id AND a.user_id = t.user_id`.

**Both aggregators also need the user's `display_timezone` threaded in.**
`_load_archived_categories(conn, user_id)` (`:123`) and
`_load_archived_hashtags(conn, user_id)` (`:164`) take no settings today; add the
parameter and bind it for `HOME_RATE_JOIN`. `get_dashboard` already loads settings
at `:235` before calling them at `:254-255`, so pass what is already there — do not
add a second settings query.

Then replace the CASE matrices with the imported expressions. Delete the local
`_SIGNED_CENTS_SQL` / `_SIGNED_HOME_CENTS_SQL` constants in `dashboard.py` and the
inline copies in `monthly_report.py` — one definition, in `home_currency`.

⚠️ `compute_month_flow` in `monthly_report.py` is **shared by both
`/dashboard` and `/reports/monthly`** — that sharing is deliberate (byte-identical
shapes by construction). Do not fork it.

### 2. Missing-rate policy — `null` and flag

Per the decision doc: a row whose date has no resolvable rate contributes nothing,
and **the category must not report a partial sum.**

- Use CR1's `UNCONVERTIBLE_FLAG_EXPR` — do **not** hand-roll the
  `COUNT(*) FILTER (WHERE <home expr> IS NULL)` predicate at each of the four call
  sites (both `monthly_report` queries, both `dashboard` aggregators). One
  definition, imported, same as the CASE matrices.
- If a category has any unconverted row, its `spent_home_cents` is `null` — not a
  sum of the convertible subset. **The same applies to the dashboard's
  `lifetime_spent_home_cents`** (`dashboard.py:139`, `:181`), which is the same
  quantity under a different name and is easy to miss because the archived panels
  are the least-touched surface here.
- ⚠️ **The unconvertible count is the only authority. Never infer convertibility
  from the sum.** Both dashboard aggregators wrap their totals in
  `COALESCE(SUM(...), 0)`, so a group where *every* row is unconvertible arrives as
  a confident `0`, not a `NULL`. Keep that wrapper — it is what makes an empty
  archived category report `0` deterministically — but drive the null-out from
  `SUM(UNCONVERTIBLE_FLAG_EXPR) > 0` in Python, never from the aggregate being
  `NULL`. A `0` total sitting next to `unconverted_count > 0` is a false total, and
  it is the exact failure this package exists to remove.
- **The inflow/outflow totals need this too**, not just the category breakdown.
  `monthly_report.py:217` and `:219` wrap the home value in
  `SUM(CASE WHEN signed_home_cents > 0 THEN … ELSE 0 END)` — `NULL > 0` is `NULL`,
  so an unconvertible row silently scores as **zero** rather than propagating, and
  an all-unconvertible month returns `0` rather than failing loudly. Pair those two
  home SUMs with the flag. Leave `:216` and `:218` alone — they are the native
  figures, which per the rule below are unaffected.
- ⚠️ **Project the flag inside the CTE.** Both queries aggregate in an outer
  `SELECT … FROM signed_txns` (`:107-151`, `:188-220`) where the `a` and `r`
  aliases are out of scope. Select `UNCONVERTIBLE_FLAG_EXPR` as a column within the
  CTE and sum it by name outside; interpolating it into the outer `SUM` is a hard
  SQL error.
- ⚠️ **Fix the Python consumers in the same step.** `monthly_report.py:166`
  (`int(row["spent_home_cents"])`) and `:175`
  (`sum(r["spent_home_cents"] for r in rows)`) both `TypeError` on `None` — which
  is exactly the state this policy now produces. They must handle the null category
  total, or CR2's own missing-rate test goes red.
- Surface an `unconverted_count` on dashboard and monthly-report responses.
- Native-currency figures (`spent_cents`) are unaffected — they never needed a rate.

**Never `COALESCE` an unconvertible home value to the native amount.** That is the
bug being removed: it silently reports `$1,000` as `S/1,000`.

The `hashtag_breakdown` invariant — breakdown rows sum to the parent category total
**by construction** — must survive. If a category total is `null`, its breakdown
rows follow the same rule.

### 3. Row serialization

`transaction_from_row` reads `row["amount_home_cents"]`. Once the column is gone
that raises. Supply it from the query instead: every query that feeds
`transaction_from_row` selects `HOME_CENTS_EXPR AS amount_home_cents`, so the
serializer is unchanged.

**Write responses.** `INSERT ... RETURNING *` cannot carry a `LATERAL` join. Have
write paths **re-read the row through the standard read query** inside the same
transaction. One extra `SELECT` per write, and it guarantees one conversion
mechanism rather than a second Python-side copy. Given single-user volumes the
cost is irrelevant; the consistency is not.

Same treatment for `inbox_from_row`.

### 4. Sync

`/sync` payloads embed transactions and inbox items. Confirm no consumer reads the
doomed columns and that computed home values flow through. Sync account rows
intentionally `null` their `current_balance_home_cents` — leave that as is.

---

## Expected behaviour changes — do NOT "fix" these back

These are the point of the rework. A test asserting the old behaviour is now
asserting a bug.

**1. Cross-currency transfers stop netting to zero.**

`$1,000 → S/3,450`, market rate that day 3.58:

```
USD leg:  100000 × 3.58  =  −S/ 3,580
PEN leg:  345000 × 1.00  =  +S/ 3,450
                            ───────────
              @Transfer  =  −S/   130     ← the spread the bank charged
```

Previously `transfers.py` forced `sibling_home = primary_home`, hiding this. The
S/130 is real money really paid. Update any test asserting a forced zero.

**2. `@Transfer` may be non-zero.** Exactly two legitimate causes — an FX spread,
or a loan/repayment with a person (one leg goes to `@Debt`, so nothing cancels).
A third cause is a bug.

**3. Same-currency transfers still net to exactly 0.** Both legs convert at the
same rate. If this breaks, something is wrong.

**4. Stored vs computed may disagree** for pre-existing rows whose stored value
was wrong (rate-1.0 rows from finding 1.4, forced transfer legs from 1.3). The
computed value is the correct one. Both tables hold 0 rows as of 2026-08-01, so in
practice only test fixtures are affected.

**5. Near-midnight transactions may price on a different day than before.** Reads
now resolve the rate date in the user's `display_timezone` (decision doc, "Which
calendar day a transaction is priced on"), while the stored value was written from
the *client's* offset date. For a transaction within a few hours of midnight the
two can differ by one calendar day, and therefore by one rate row. This is the
intended convention — it keeps a transaction's rate date and its report month in
agreement — not a bug to patch back. The divergence disappears in CR3, when writes
stop resolving rates at all.

---

## Tests

Update existing files that assert stored home values: `test_audit_response_shape.py`
(17 refs), `test_phase_fixes.py` (13), `conftest.py` (5),
`test_exchange_rates_history.py` (4), `test_sync.py`, `test_rate_cache.py`,
`test_opening_balance.py`, `test_archive_endpoints.py`.

Add:

- **Cross-currency transfer nets to the spread, not zero** — the `$1,000 →
  S/3,450` @ 3.58 case above, asserting `@Transfer = −S/130`
- **Same-currency transfer still nets to exactly 0**
- **Real ↔ person transfer** — one leg `@Transfer`, one `@Debt`, unchanged by this
  package. This is the pre-existing non-zero case; pin it so it isn't confused
  with the FX case later.
- **Missing rate** — a transaction dated before the earliest rate row makes its
  category report `spent_home_cents: null` plus an `unconverted_count`, and
  **never** a native-amount substitute
- **`/dashboard` and `/reports/monthly` agree** for the same month — they share
  `compute_month_flow`; drift is a real bug class

---

## Done when

- [ ] No `COALESCE(t.amount_home_cents, …)` remains anywhere — `grep` returns nothing.
      ⚠️ This grep does **not** cover the aggregate-level `COALESCE(SUM(…), 0)` at
      `dashboard.py:139,181`; check those by hand and confirm the null-out is driven
      by the unconvertible count, not by the sum
- [ ] `lifetime_spent_home_cents` nulls out on `unconverted_count > 0`, same as
      `spent_home_cents` — a `0` total beside a non-zero count is a bug
- [ ] No read path reads `amount_home_cents` or `exchange_rate` from a row
- [ ] `dashboard.py`'s local CASE constants deleted; both files import from
      `home_currency`
- [ ] `dashboard.py`'s account join is a **`LEFT JOIN`** — archived categories and
      hashtags with zero transactions still appear with zero totals
      (`dashboard.py:129-131`); an inner join silently drops them
- [ ] `unconverted_count` on dashboard + monthly-report responses; categories with
      an unconvertible row report `spent_home_cents: null`
- [ ] `hashtag_breakdown` still sums to its parent category total
- [ ] Write responses re-read through the standard read query (one mechanism)
- [ ] `pytest` green
- [ ] The columns still exist — **no migration in this package**

---

## Do not

- Drop any column, or touch `sql/` — CR3
- Remove `exchange_rate` from request/response schemas — CR3
- Delete `lookup_exchange_rate` or the rate-resolution code in write paths — CR3
- Touch the field guards or `extra="forbid"` — CR4
- Add an `@FX` category — deferred, D-d in [README.md](README.md)
