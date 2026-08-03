# CR2 — Read paths

**Prerequisites:** CR1 merged. Read
[`../currency-model-decision.md`](../currency-model-decision.md) first.
**Blocks:** CR3. **Blocked by:** CR1.

---

> ## ⚠️ Scope rewritten — 2026-08-02, owner decisions D-e / D-g / D-h / D-i
>
> **The engine ends with PEN on one surface: the monthly report.** Everything else
> reports native currency only.
>
> | Level | Native | PEN |
> |---|---|---|
> | Individual records — transactions, inbox | **only** | none (D-e) |
> | Per-account figures — balances, reconciliations | **only** | none (D-i) |
> | Cross-account aggregates — category, hashtag, month totals | none (D-h) | **only** |
>
> A record is in one currency. An account is in one currency. Only an aggregate
> spans currencies — and categories/hashtags are **assumed** to span them rather
> than checked. So conversion belongs at exactly one level and nowhere else.
>
> **If you read an earlier version of this file**, three things it told you to do
> are now deletions instead: re-reading every row after every write (~14 sites),
> converting the dashboard's archived lifetime panels, and preserving
> `current_balance_home_cents`. All gone.

---

## Goal

Reduce the engine to one home-currency surface, then make that surface compute
instead of reading a stored column.

The columns still exist after this package — they are simply no longer read. CR3
drops them.

**This is the package where behaviour changes**, so read "Expected behaviour
changes" before touching a test.

---

## Why

The stored columns are being deleted, so reads must stop depending on them first.

But most reads that depended on them turned out to be surfaces that should not
convert at all: a per-record PEN value that was computed, stored, serialised and
shipped for nobody; per-account balances that are single-currency by construction;
and native aggregates that summed dollars into soles. Deleting those is cheaper and
more correct than converting them — and it takes **seven conversion mechanisms down
to one**.

---

## Steps

Five steps, four of them deletions. Commit each separately; each ends `pytest` green.

### 1. Delete the archived aggregates (D-g)

| File | Delete |
|---|---|
| `app/routers/dashboard.py` | `_SIGNED_CENTS_SQL` / `_SIGNED_HOME_CENTS_SQL` (`:102-120`), `_load_archived_categories` (`:123-161`), `_load_archived_hashtags` (`:164-208`), the `archived_categories` / `archived_hashtags` branches (`:246-255`) and their response fields |
| `app/schemas/dashboard.py` | `DashboardArchivedAggregate` (`:51-64`), the two `DashboardResponse` fields (`:81-87`) |
| `tests/test_archive_endpoints.py` | the `lifetime_spent_home_cents == -300` assertion (`:439`) |

**`archived_accounts` stays.** An archived *account* still holds real money; an
archived *category* holds only history.

This also removes, by deletion rather than repair, the missing `@Opening` exclusion
in both aggregators (`../audit-2026-08-01-remediation-plan.md:299`) — they summed
opening balances into every lifetime total, and nobody noticed.

### 2. Remove per-record PEN values (D-e)

| File | Change |
|---|---|
| `app/schemas/transactions.py` | drop `amount_home_cents` — the field (`:54`) and the `row[...]` read (`:105`) |
| `app/schemas/inbox.py` | drop `amount_home_cents` (`:44`, `:70`) and `transfer_amount_home_cents` (`:53`, `:79`); the `round(amount_cents * rate)` computations disappear with them |
| `app/helpers/formatting.py` | remove the `amount_home_cents` negation branches (`:21-22`, `:56-57`); `amount_cents` negation stays exactly as is |
| `app/routers/sync.py` | transaction + inbox payloads lose the field via the schema change — no separate edit |

The field is **removed, not nulled** — a deliberate exception to null-over-omission,
the same treatment `exchange_rate` gets in CR3 and `recalculation` got in WP1.1. A
permanently-`null` key on every transaction forever is dead weight.

**Keep `exchange_rate`** on both response models, still read from the column. CR3
removes it.

**No write path changes.** Every `INSERT ... RETURNING *` and `UPDATE ... RETURNING *`
is untouched — there is no computed field left for a write response to carry.

**While in `formatting.py`, close the WP10.2 flip.** `apply_debit_as_negative_inbox`
negates `amount_cents` but never `transfer_amount_cents` (`:45-57`), so the sibling
leg is emitted unflipped beside a flipped primary
(`../audit-2026-08-01-remediation-plan.md:297`).

### 3. Remove per-account PEN values (D-i)

The largest deletion. Every caller of three helpers disappears, so the helpers go too.

| File | Change |
|---|---|
| `app/schemas/accounts.py` | drop `current_balance_home_cents` (`:48`, `:66`) |
| `app/schemas/dashboard.py` | drop `current_balance_home_cents` from `DashboardAccount` (`:16`) |
| `app/schemas/reconciliations.py` | drop `beginning_balance_home_cents` / `ending_balance_home_cents` (`:64`, `:68`, `:107`, `:111`) |
| `app/routers/dashboard.py` | `_load_accounts`' rate block (`:60-89`) and its `main_currency` parameter |
| `app/routers/accounts.py` | the `main_currency` fetch + `batch_get_rates` block (`:74-96`); the `get_home_balance` call (`:145`) |
| `app/routers/sync.py` | the `resolve_home_rates` block and its explanatory comment (`:71-82`) |
| `app/helpers/accounts.py` | **delete `get_home_balance`** (`:26-53`) and its 11 call sites — activity-log snapshots become native-only |
| `app/helpers/reconciliations.py` | **delete `resolve_home_rates`** (`:31-91`) and its 5 call sites |
| `app/helpers/exchange_rate.py` | **delete `batch_get_rates`** (`:140-168`) — no callers remain |

**Keep `get_rate`.** `GET /exchange-rates` (`routers/exchange_rates.py:28`) and
CR1's parity test still use it, and `lookup_exchange_rate` calls it until CR3.

⚠️ **No replacement total.** The engine reports no net worth. Accounts are read in
their own currency and nothing sums them. Deliberate — see D-i.

**What this closes, all by deletion rather than repair:**

| Was | Now |
|---|---|
| `resolve_home_rates` resolved `date_end` in **UTC**, not `display_timezone` — the only historical-date converter, contradicting CR1's own convention | gone with the function |
| Account balances resolved at UTC `today` — for a Lima user after 19:00 local, that asks for *tomorrow's* rate | gone with `batch_get_rates` |
| **Three of four** `SELECT main_currency FROM user_settings` sites, which disagreed on whether missing settings mean `null` or `422` | only `get_user_report_settings` remains (the fourth, `transfers.py:145`, dies in CR3) |
| `get_home_balance` called **twice per account mutation** purely to fill an activity-log snapshot, at *today's* rate — so the audit log was a function of when it ran, not of the data | gone with the function |
| `resolve_home_rates`' account-currency query had **no `user_id` filter** | gone with the function |

### 4. Remove native aggregates (D-h)

`app/helpers/monthly_report.py` — delete `spent_cents` from the breakdown query and
the Python roll-up (`:148`, `:166`, `:174`, `:180`), and `inflow_cents` /
`outflow_cents` / `net_cents` from the totals query and dict (`:216`, `:218`,
`:227`, `:229`, `:233`, `:235`, `:237`). Same fields off `app/schemas/dashboard.py`
(`:31`, `:38`, `:44`, `:46`, `:48`).

**Why, since these never needed a rate:** they never needed one *per row*, but
`GROUP BY category_id` has no currency partition, so a category spanning both
accounts sums `$15 + S/25 = 4000` — a number in no currency. That violates the hard
constraint at `../currency-model-decision.md:488`: *"never emit a total that sums
across currencies without converting."*

`SIGNED_CENTS_EXPR` may end up unused by this file — check before removing the
import; CR1 exports it either way.

⚠️ **Two invariants are worded against deleted fields.** Both survive; only the
wording moves, and that is CR5's job:
- `../engine-spec.md:815` — breakdown rows sum to the parent's `spent_cents` →
  **`spent_home_cents`**, and a `null` parent has `null` rows.
- `../scaling-boundaries.md:28` — *"visible rows sum exactly to `net_cents`"* →
  **`net_home_cents`**.

### 5. The monthly report computes

Two queries remain: the breakdown CTE (`:105-157`) and the totals CTE (`:186-225`).
Neither joins accounts today, so CR1's fragments have no `a` to reference.

**Thread the timezone.** `compute_month_flow` gains a `display_timezone` parameter —
it has none today. Both callers, `routers/dashboard.py:244` and
`routers/reports.py:47`, already hold it from `get_user_report_settings`. **Do not
add a second settings loader.**

⚠️ `compute_month_flow` is **shared by `/dashboard` and `/reports/monthly`** — that
sharing is deliberate (byte-identical shapes by construction). Do not fork it.

**Assert the home currency.** At both call sites, assert
`settings["main_currency"] == HOME_CURRENCY`. `helpers/home_currency.py:126-129`
states this as a caller obligation and **no caller anywhere performs it**. It costs
nothing and makes a lifted `sql/018` CHECK fail loudly instead of silently pricing a
non-PEN ledger in PEN.

**Splice the fragments.** Inside each CTE, after `FROM expense_transactions t`:

```sql
LEFT JOIN expense_bank_accounts a ON a.id = t.account_id AND a.user_id = t.user_id
<home_rate_join("$4")>
```

⚠️ **`LEFT JOIN`, not `JOIN`.** `helpers/home_currency.py:25-31` writes the alias
contract that way. An inner join happens to be safe here — `account_id` is
`NOT NULL` with an FK — but one join shape everywhere is worth more than what an
inner join buys, which is nothing. Both queries bind `$1..$3`, so the timezone is
`$4`.

Then replace the inline CASE matrices with `SIGNED_HOME_CENTS_EXPR`. **Do not
hand-roll** the sign matrix or a `COUNT(*) FILTER` predicate — CR1 exports both, and
three duplicated copies of that matrix is what audit WP9.1 was about.

⚠️ **Project the flag inside the CTE.** Both queries aggregate in an outer
`SELECT ... FROM signed_txns` where the `a` and `r` aliases are out of scope.
Select `UNCONVERTIBLE_FLAG_EXPR AS is_unconvertible` within the CTE and sum it by
name outside; interpolating it into the outer `SUM` is a hard SQL error.

### 6. Missing-rate policy — `null` and count

A row whose date has no resolvable rate contributes nothing, and **the group must
not report a partial sum.**

- ⚠️ **The count is the only authority. Never infer convertibility from the sum.**
  `SUM` skips NULLs, and `SUM(CASE WHEN x > 0 THEN x ELSE 0 END)` scores a NULL row
  as **zero** — so an all-unconvertible month returns a confident `0`, not `NULL`.
  Drive the null-out from `SUM(is_unconvertible) > 0` in Python, never from the
  aggregate being `NULL`.
- **The totals need this as much as the breakdown.** `:217` and `:219` are exactly
  the silent-zero shape above.
- ⚠️ **Fix the Python consumers in the same step.** `monthly_report.py:166`
  (`int(row["spent_home_cents"])`), `:175` (`sum(...)`) and **`:238`**
  (`net_home_cents = inflow_home_cents - outflow_home_cents`) all `TypeError` on
  `None` — exactly the state this policy produces.

**Reporting the count — D-f: BOTH levels.**

| Level | Field | Purpose |
|---|---|---|
| Per row — each category, each hashtag-combination | `unconverted_count` | *which* figure is unknown, and how badly |
| Per report — top level of `/dashboard`, each month of `/reports/monthly` | `unconverted_count` | makes it **noticeable**; a blank cell is easy to skim past |

The per-row number is already computed to decide whether to null that row, so
exposing it costs one field.

⚠️ **The top-level count must be `COUNT(DISTINCT t.id)`, not a sum of the per-row
counts.** A transaction appears in *both* its category row and its
hashtag-combination row, so summing double-counts: a month with 2 bad transactions
would report 4.

**Never `COALESCE` an unconvertible home value to the native amount.** That is the
bug being removed: it reports `$1,000` as `S/1,000`.

The `hashtag_breakdown` invariant — breakdown rows sum to the parent **by
construction** — must survive against `spent_home_cents`. A `null` parent has `null`
rows.

---

## Schema changes — `app/schemas/dashboard.py`

Absent from the original package's file list, and nothing else works without it.

| Field | Change |
|---|---|
| `spent_home_cents` (`:32`, `:39`) | `int` → `Optional[int]` |
| `inflow_home_cents` / `outflow_home_cents` / `net_home_cents` (`:45,47,49`) | `int` → `Optional[int]` |
| `spent_cents`, `inflow_cents`, `outflow_cents`, `net_cents` (`:31,38,44,46,48`) | **removed** (D-h) |
| `current_balance_home_cents` (`:16`) | **removed** (D-i) |
| `DashboardArchivedAggregate` (`:51-64`) + its two response fields | **removed** (D-g) |
| `unconverted_count` | **new** on `DashboardHashtagBreakdown`, `DashboardCategory`, `DashboardTotals` |

`app/schemas/reports.py` reuses `DashboardCategory` / `DashboardTotals` wholesale, so
it is fixed for free.

---

## Archived categories stay in the monthly report

`compute_month_flow`'s category query (`:94-103`) filters on `user_id`,
`deleted_at IS NULL` and `system_key IS DISTINCT FROM 'opening_balance'` — **not on
`is_archived`.** That is deliberate. **Add a comment saying so**, because an
uncommented omission is precisely how the `@Opening` gap in the archived aggregators
survived unnoticed.

The reason is mechanical, not philosophical. The category list and the month totals
come from **two independent queries**. Filter archived out of the list and their
transactions still count in the total, leaving money on no visible row — which
breaks *"visible rows sum exactly to the month total"*
(`../scaling-boundaries.md:28`, filed as never-trade-away business logic). Excluding
them from the totals as well would make archiving a category today rewrite what past
months report.

---

## Expected behaviour changes — do NOT "fix" these back

A test asserting the old behaviour is now asserting a bug.

**1. Cross-currency transfers stop netting to zero.** `$1,000 → S/3,450`, market
rate that day 3.58:

```
USD leg:  100000 × 3.58  =  −S/ 3,580
PEN leg:  345000 × 1.00  =  +S/ 3,450
                            ───────────
              @Transfer  =  −S/   130     ← the spread the bank charged
```

`transfers.py` previously forced `sibling_home = primary_home`, hiding it. The S/130
is real money really paid.

**2. `@Transfer` may be non-zero.** Exactly two legitimate causes — an FX spread, or
a loan/repayment with a person (one leg goes to `@Debt`, so nothing cancels). A
third cause is a bug.

**3. Same-currency transfers still net to exactly 0.** Both legs convert at the same
rate. If this breaks, something is wrong.

**4. A category with an unconvertible row now shows nothing at all** —
`spent_home_cents: null` with no native figure beside it, because D-h removed the
native one. Fail-closed working as designed, but it is a blank where a number was.

**5. Near-midnight transactions may price on a different day.** Reads resolve the
rate date in the user's `display_timezone`, while the stored value came from the
*client's* offset date. Intended — it keeps a transaction's rate date and its report
month in agreement. The divergence disappears in CR3.

**6. `amount_home_cents` disappears from transaction and inbox responses.** Absent,
not `null`. Tests asserting a per-transaction value should be **deleted**, not
updated — the quantity no longer exists at that level.

**7. Balances and reconciliations lose their PEN figures**, and nothing replaces
them. There is no net-worth number anywhere in the engine.

---

## Tests

Update existing files that assert stored or per-account home values:
`test_audit_response_shape.py` (17 refs), `test_phase_fixes.py` (13), `conftest.py`
(5), `test_sync.py`, `test_archive_endpoints.py`, `test_opening_balance.py`.

⚠️ **Seeding rules** (`tests/test_home_currency_parity.py:1-33`): seed rates strictly
inside **2001-01-01…2020-12-31** and never `DELETE FROM exchange_rates` wholesale —
the table has no `user_id` and is shared across xdist workers. The suite floor is
1997-01-14 (`test_exchange_rates_history.py`), and `test_phase_fixes.py` depends on
nothing existing before 2000-01-01.

Add:

- **`amount_home_cents` absent** from transaction, inbox and `/sync` payloads —
  absent, not `null`
- **`current_balance_home_cents` absent** from account and `/sync` payloads; the
  reconciliation home fields absent too
- **Cross-currency transfer nets to the spread, not zero** — the case above,
  asserting `@Transfer = −S/130` in the report
- **Same-currency transfer still nets to exactly 0**
- **Real ↔ person transfer** — one leg `@Transfer`, one `@Debt`. The pre-existing
  non-zero case; pin it so it is not confused with the FX case later
- **Missing rate** — a transaction dated before the earliest rate row makes its
  category report `spent_home_cents: null` plus a per-row `unconverted_count`, and
  **never** a native substitute
- **All-unconvertible month** — totals are `null`, not `0` (the `NULL > 0` trap)
- **Top-level count is de-duplicated** — one unconvertible transaction carrying two
  hashtags reports `unconverted_count: 1` at report level, not 2 or 3. This passes by
  accident with single-hashtag fixtures, so the fixture must carry **two or more**
- **Mixed-currency category returns no native figure** — the D-h regression guard
- **`/dashboard` and `/reports/monthly` agree** for the same month — they share
  `compute_month_flow`; drift is a real bug class

---

## Done when

- [ ] `grep -rn "lifetime_spent" app/ tests/` → nothing (D-g)
- [ ] `grep -rn "amount_home_cents" app/schemas/ app/helpers/formatting.py` → nothing (D-e)
- [ ] `grep -rn "current_balance_home_cents\|balance_home_cents\|resolve_home_rates\|get_home_balance\|batch_get_rates" app/` → nothing (D-i)
- [ ] `grep -rn "spent_cents\|inflow_cents\|outflow_cents\|net_cents" app/` → nothing (D-h)
- [ ] `grep -rn "get_rate(" app/` → only `routers/exchange_rates.py` and `helpers/exchange_rate.py` internals
- [ ] No `COALESCE(t.amount_home_cents, …)` remains anywhere
- [ ] `compute_month_flow` takes `display_timezone`; **both** callers assert
      `main_currency == HOME_CURRENCY`
- [ ] The accounts join is a **`LEFT JOIN`**; the flag is projected **inside** the CTE
- [ ] `unconverted_count` present at **both** levels; the report-level one is
      `COUNT(DISTINCT t.id)`, verified with a fixture whose unconvertible transaction
      carries **two or more hashtags**
- [ ] A category with an unconvertible row reports `spent_home_cents: null` — never a
      partial sum, never a native substitute
- [ ] `hashtag_breakdown` rows still sum to their parent's `spent_home_cents`
- [ ] The `is_archived` omission in `compute_month_flow` carries a comment
- [ ] No write path re-reads a row for conversion — that step was removed
- [ ] `pytest` green
- [ ] The columns still exist — **no migration in this package**

---

## Do not

- Drop any column, or touch `sql/` — CR3
- Remove `exchange_rate` from request/response schemas — CR3
- Delete `lookup_exchange_rate` or the rate-resolution code in write paths — CR3
- **Delete `get_rate`** — still serves `GET /exchange-rates` and CR1's parity test
- Touch the field guards or `extra="forbid"` — CR4
- Update `engine-spec.md`, `scaling-boundaries.md`, `api-design-principles.md` or
  `client-breaking-changes.md` — CR5
- Add an `@FX` category — deferred, D-d in [README.md](README.md)
- **Re-add a PEN value at the record or account level.** If a future view needs one,
  that is a new decision, not a CR2 judgement call — and
  `../currency-model-decision.md` records what re-adding would cost.
