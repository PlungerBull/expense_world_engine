# Currency Model — Decision, 2026-08-01

**Status: shipped 2026-08-05 as `sql/021` (WP2 of the deletion program; program docs in git history).** This document is
the design record and it survives the rework. It deleted audit findings 1.2, 1.4
and 1.5 rather than fixing them; 1.3 was repaired by WP1 first and is now
unrepresentable as well.

Three things this document said that turned out to need correcting, all fixed
inline below: the migration is `sql/021`, not `sql/019` (019 and 020 were consumed
by the transfer collapse); account balances keep their home value, which the
"Where currency appears" table denied; and reconciliations lost theirs, which the
same table had flagged as a "known inconsistency, deliberately left".

**Amended 2026-08-10: the transfer feature was removed entirely** (owner decision;
`sql/030`, entry in `client-breaking-changes.md`). The currency model itself is
untouched — native storage, read-time conversion, null-and-flag all stand — but
the transfer-specific sections below (`@Transfer` semantics, the transfer row
examples, the `@FX` deferral) are deleted or reduced to tombstones. A move
between accounts is now two ordinary rows; its legs convert independently like
any other rows, and monthly totals include them.

---

## The decision, in one sentence

**Every account keeps its money in its own currency; the PEN value is calculated
when it is asked for, from `exchange_rates`, using the rate on the transaction's
date.**

Nothing converted is ever stored.

---

## Where currency appears

**Amended 2026-08-02.** The original version of
this document assumed every response carrying an amount would carry a PEN version
too — the convention inherited from the stored-column model. It doesn't.

| Level | Native | PEN |
|---|---|---|
| Individual records — transactions, inbox items | **only** | none |
| Reconciliations — scoped to one account | **only** | none |
| Account balances | yes | **yes** — `current_balance_home_cents` |
| Cross-account aggregates — category, hashtag, month totals | none | **only** |

**The reasoning is one sentence: conversion belongs wherever currencies are
combined, and nowhere else.** A transaction belongs to one account, so it is in one
currency — a second number on the row is noise. A reconciliation is scoped to one
account, so the same holds. An aggregate spans accounts, and categories and
hashtags are **assumed** to span currencies rather than checked.

**Account balances are the one deliberate exception, and they earn it.** A single
balance is single-currency like a reconciliation — but the account *list* is the
only surface that shows all your money at once, and reading `S/8,500` beside
`$1,200` with no common unit is the thing that makes the list unusable. So the
balance keeps `current_balance_home_cents`, computed at **today's** rate — where
"today" resolves in the user's `display_timezone`
(`exchange_rate.rate_lookup_date`, owner decision 2026-08-06; previously these
lookups used the UTC date and could disagree with the reports near midnight) —
and the Python half of the conversion rule (`helpers/exchange_rate.get_rate`,
`batch_get_rates`) stays alive to serve it. `CLAUDE.md`'s home-currency table is
authoritative on this; an earlier revision of this document said per-account
figures were native-only, and that was never implemented.

Two consequences worth stating outright, because both look like omissions:

- **There is no net-worth total.** Nothing sums balances across accounts, even
  though each one carries a PEN figure. The conversion is there so the rows are
  comparable, not so they can be added.
- **A category whose date has no rate reports nothing at all** —
  `spent_home_cents: null` with no native figure beside it, because the native
  aggregate was removed as meaningless, plus an `unconverted_count` saying how
  many rows are behind the null. Fail-closed, by design.

### What re-adding a per-record or per-account PEN value would cost

Recorded so a future reader inherits the price instead of rediscovering it:

- `INSERT ... RETURNING *` cannot join to `expense_bank_accounts` or
  `exchange_rates`, so **every write path would need a follow-up read** inside its
  transaction — a dozen-plus sites across `helpers/transactions.py` and
  `helpers/inbox.py`.
- Those write paths would each need the user's `display_timezone`, which they do not
  load today.
- `helpers/home_currency.py` hardcodes its table aliases, so any second join
  context would need alias-parameterised builders.
- Account and reconciliation values would need `get_home_balance` and
  `resolve_home_rates` rebuilt, along with the rate-date and duplicate-settings
  problems that deleting them solved.

If a future view genuinely needs a comparable column across currencies, the cheap
answer is a new aggregate endpoint, not a field on every row.

---

## Why this document exists

The engine had four surfaces that show a PEN value for non-PEN money, and each
used a different mechanism:

| Surface | Stores the rate? | Stores the home amount? | Strategy |
|---|---|---|---|
| Account balances | no | no — no `current_balance_home_cents` column exists | derived at read time |
| Reconciliations | no | no — `round(begin * rate)` at serialization (`schemas/reconciliations.py:107`) | derived at read time |
| Inbox | **yes** (`sql/003:62`, default `1.0`) | no — computed at serialization (`schemas/inbox.py:86`) | half derived |
| Transactions | **yes** (`sql/003:82`) | **yes** (`sql/003:76`) | fully stored |

`amount_home_cents` exists on exactly one table in the entire schema. Two of the
four surfaces already worked the way this decision mandates. **This is not a new
architecture — it is finishing a conversion that was already half done and never
completed.**

⚠️ **Amended 2026-08-02.** The four-surface framing above is the *diagnosis*, and it
holds. The *remedy* went further than "make all four convert at read time": under
D-g/D-h/D-i, three of the four stop converting altogether. Inbox and transactions
lose their PEN value, account balances and reconciliations lose theirs, and only the
report converts. So the count that matters afterwards is **one surface, one
mechanism** (`helpers/home_currency.py`) — not four surfaces agreeing.

Every 🔴 finding in WP1 is the same failure: *a derived value was stored, then
not kept in sync with what derives it.*

- change the account → nobody recomputes it (**1.5**)
- set the account on an inbox item → nobody recomputes it (**1.4**)
- change the home currency → a 222-line helper had to walk the ledger (**1.1**,
  deleted 2026-08-01; home currency locked to PEN by `sql/018`)

---

## The rate on a transaction was never a fact

A transaction belongs to exactly one account, and **the account governs the
currency** — there is no transaction-currency column, and there never was. Given
that, ask when a stored rate encodes a fact about the world rather than a
reporting choice:

- **Ordinary USD expense on a USD account.** You spent $40. That is the whole
  fact. Its PEN value is a question asked later, answered by whatever rate policy
  is in force. **Not a fact.**
- **A PEN card used at a foreign merchant.** The bank already converted; the
  transaction is S/148 on a PEN account. The foreign price is not modelled at all.
  **Not a fact — there is no field for it.**
- **Two rows of a cross-currency move** *(argued when the since-removed transfer
  feature paired them; the argument survives the pairing)*. Here a real rate
  exists. But the two rows already store `100000` (USD side) and `345000` (PEN
  side). **The rate is `345000 ÷ 100000`.** The column stored a value derivable
  from two values already present.

**Therefore `expense_transactions.exchange_rate` never held information that was
not either recoverable or a reporting choice.** Dropping it loses nothing.

---

## Schema change

Migration **`sql/021`**. (This document originally said `sql/019`; that number and
`sql/020` went to the transfer collapse in the meantime.)

### Dropped

| Table | Column |
|---|---|
| `expense_transactions` | `amount_home_cents` |
| `expense_transactions` | `exchange_rate` |
| `expense_transaction_inbox` | `exchange_rate` |

### Kept, unchanged

| Thing | Why |
|---|---|
| `expense_transactions.amount_cents` | the real amount, in the account's currency |
| `expense_bank_accounts.currency_code` | the only place currency lives |
| **the `exchange_rates` table** | the rate history reads convert from — now load-bearing |
| `app/jobs/fetch_exchange_rates.py`, `app/jobs/backfill_exchange_rates.py` | same reason |
| `spent_home_cents` and the month totals on **the monthly report** | computed instead of stored — and the only surviving PEN figures |

### Where PEN appears

> **Revised twice on 2026-08-02.** The first version of this document said *"the
> response contract does not change — every response with an amount includes a
> home-currency version."* That was wrong (D-e). A narrower version then kept PEN on
> archived lifetime panels and account balances; that was also wrong (D-g, D-h, D-i).
> The settled rule is in **"Where currency appears"** near the top of this document.

The short form: **the monthly report and its month totals. Nothing else.** Individual
records, account balances and reconciliations report native currency only, and the
native aggregates that used to sit beside the PEN ones were removed because
`GROUP BY category_id` has no currency partition — `$15 + S/25 = 4000` is a number in
no currency, which the "Research basis" section below records as a hard constraint.

A PEN account shows soles; a USD account shows dollars; their transactions and
balances show the account's currency and nothing else. Consolidation happens where
figures are combined, which is the report.

The field is **absent, not `null`** — a documented exception to null-over-omission,
same as `exchange_rate` and the retired `recalculation` field. A permanently-`null`
key on every transaction forever is dead weight.

`CLAUDE.md`'s home-currency convention was amended to match: the engine remains the
only thing that converts — that part never changes — but the obligation applies to
cross-currency figures, not to every amount.

~~**Known inconsistency, deliberately left:** reconciliations still expose
`beginning_balance_home_cents` / `ending_balance_home_cents` even though a
reconciliation belongs to one account and is therefore single-currency.~~
**Settled 2026-08-05.** Both fields are gone, and `resolve_home_rates` with them —
which also closed audit finding 2.3, a live cross-tenant read (it selected accounts
with no `user_id` predicate, and engine-side scoping is the only tenant guard there
is while RLS stays inert). It did not wait for the chaining retirement.

---

## What rows look like

### Ordinary transaction, home currency

Lunch, S/45, PEN account.

```
before:  amount_cents=4500   exchange_rate=1.0   amount_home_cents=4500
after:   amount_cents=4500
```

### Ordinary transaction, foreign currency

Netflix, $15, USD account, 2026-08-01 (rate that day 3.58).

```
before:  amount_cents=1500   exchange_rate=3.58   amount_home_cents=5370
after:   amount_cents=1500
```

Report output is identical: **S/53.70**. The difference is where 3.58 comes from
— frozen into the row at write time before, looked up from `exchange_rates` for
2026-08-01 after.

Consequence: a wrong rate is no longer permanent. Fix the rate table once and
every historical report corrects itself. Under the stored model a bad rate — for
example one written by the old silent `1.0` fallback — was wrong forever.

*(Three further worked examples — same-currency transfer, cross-currency
transfer with its FX-spread arithmetic, and the USD→USD case of audit finding
WP1.3 — were deleted 2026-08-10 with the transfer feature, along with the
`@Transfer` semantics section that assigned meaning to a non-zero `@Transfer`
month. Git history holds them. A move between accounts is now two ordinary
rows; each converts independently by its own date's rate, and any FX spread
between them simply appears in whatever user categories the two rows carry.)*

---

## Missing-rate policy

Under the stored model this could not arise: the write failed with `422
RATE_UNAVAILABLE` before a row existed. Under read-time conversion **the write
succeeds and the report must decide.**

**Decision: null and flag.** A row whose date has no resolvable rate contributes
nothing to home-currency totals; the affected category reports
`spent_home_cents: null` and the response carries a count of unconverted rows.
Native-currency figures are unaffected.

**What this replaces.** The current report SQL does:

```sql
COALESCE(t.amount_home_cents, t.amount_cents)
```

which falls back to **treating USD cents as PEN cents** — a 3.58× understatement
rendered without complaint. That fallback is gone, and it was resolved by
construction rather than by editing four call sites: once the column is dropped,
the SQL cannot run. It lived in `helpers/monthly_report.py` and
`routers/dashboard.py`, as the `_SIGNED_HOME_CENTS_SQL` both modules built.

**A per-row `null` is not sufficient on its own.** `SUM` silently skips `NULL`s,
and `SUM(CASE WHEN x > 0 THEN x ELSE 0 END)` silently scores a `NULL` row as zero,
so an unflagged aggregate understates exactly like the fallback it replaced. Every
home-value `SUM` must be paired with a count of unconvertible rows, and a non-zero
count makes the aggregate `null` rather than a partial total.
`app/helpers/home_currency.py` exports `UNCONVERTIBLE_FLAG_EXPR` for this, and
`helpers/monthly_report.compute_month_flow` is the one caller that uses it — on the
breakdown rows, on the category totals rolled up from them, and on the month
totals. The flag must be projected inside the CTE and summed by name outside it:
the `a` and `r` aliases do not exist in the outer `SELECT`.

**Why the write should no longer fail.** Recording what happened must never be
blocked by a rate lookup. Two consequences worth having:

- cross-currency writes stop failing when the FX job is stale
- transactions dated before the provider floor (2024-03-02) become recordable

Scope is narrow in practice — the backfill covers 2024-03-02 → today and the
daily job runs — but the case is newly reachable, and silence is the wrong
default.

---

## Which calendar day a transaction is priced on

`expense_transactions.date` is `timestamptz`, so "the transaction's date" is not
self-evident — a bare `::date` cast resolves in whatever timezone the database
session happens to carry, which is machine-dependent and must never ship.

**Decision: the rate date is the transaction's date in the user's
`display_timezone`** — the same zone `compute_month_bounds` uses to bucket months.
This keeps pricing and reporting coherent: a transaction at
`2026-03-31T23:00-05:00` is counted in March *and* priced at the March 31 rate.
Under UTC it would be counted in March but priced at April 1.

Two consequences to know:

- **This diverges from the write path**, which resolves rates from the *client's*
  offset date (`body.date` is an `AwareDatetime`; Pydantic preserves the offset).
  That offset is not recoverable from a stored `timestamptz`, so read-time SQL
  cannot reproduce it. ~~The divergence is deliberate~~ — **and it is gone as of
  `sql/021`: no write resolves a rate, so there is only one rate date and it is
  this one.**
- **`display_timezone` must reach SQL as a bind parameter, never interpolated.**
  It is validated on write since the fail-closed sweep
  (`helpers/validation.validate_timezone`, both write paths), and reads tolerate
  pre-validation junk rows via `validation.resolve_timezone` — the single
  read-side fallback (added 2026-08-06) covering both Python `ZoneInfo`
  construction and the `AT TIME ZONE` bind, which previously would raise and
  500 every report read on a bad stored zone.

---

## ~~Deferred: `@FX`~~ — mooted 2026-08-10

*(This section argued against splitting the FX spread of a cross-currency
transfer out of `@Transfer` into a synthetic `@FX` category — deferred by the
owner on 2026-08-05. The transfer removal of 2026-08-10 deleted both categories
the argument was about; there is no paired spread to split. Git history holds
the full analysis, including why a synthetic category breaks the
category-total-equals-sum-of-transactions invariant — read it before ever
proposing a report-computed category.)*

---

## Decided: system categories are engine-assigned only

**Owner decision, 2026-08-01; scope reduced to `@Opening` on 2026-08-10** (the
transfer removal deleted `@Transfer` and `@Debt`). A system category may be
attached to a transaction **only** by the engine flow that owns it. Clients may
never set one by hand.

Without this, `@Opening`'s exclusion from flow reports is unenforceable — a user
filing an ordinary expense under `@Opening` would silently drop it from every
monthly report while it still moves the balance.

### The remaining holes (open-bugs 6.7)

| # | Hole | Where |
|---|---|---|
| 1 | `POST /transactions` accepts a system `category_id` | `validate_active_category` (`helpers/validation.py`) checks `deleted_at` and `is_archived` but **not `is_system`**; no other guard found |
| 2 | `PUT /transactions/{id}` can move an ordinary transaction *into* a system category | same missing check |

*(A third hole — moving a transfer leg out of `@Transfer` — was closed 2026-08-07
as bug 6.5 via the transfer edit guard, then became unrepresentable when the
transfer feature was removed 2026-08-10.)*

### Fix (for the two still open)

- Reject `is_system = true` category targets at the **public boundary** (request
  validation), returning `422`. The internal path must keep working:
  `create_opening_balance` delegates to `create_transaction` with `@Opening`.
  Same public-boundary-plus-internal-path shape as bug 7.4's reserved-name
  check (closed 2026-08-07), which rejects the reserved system-category *name* at
  the same boundary.

---

## What this closes in WP1

| Finding | Fate |
|---|---|
| **1.2** — dominant-side rule duplicated, survivor is the buggy copy | deleted with the rule |
| **1.3** 🔴 — branch order violates §547; USD→USD transfer 500s | **unrepresentable** — no dominant-side rule exists |
| **1.4** 🔴 — inbox promotes at rate 1.0 | **unrepresentable** — no stored rate, no `1.0` default |
| **1.5** 🔴 — account move keeps the old currency's conversion | **unrepresentable** — nothing stored to fall out of sync |
| **1.7** — rate hygiene | 2 of 5 items vanish (request validation, `gt=0`). Job rate validation and `Decimal`/`ROUND_HALF_UP` still apply to the rate table and read-time math. |

Three 🔴 findings and one 🟡 close by deletion rather than repair.

---

## Client impact *(closed — the entry landed)*

**Breaking for the CLI.** `exchange_rate` became output-gone and the write contract
stopped accepting it. The full wire-change record is the 2026-08-05 read-time
currency entry in [client-breaking-changes.md](client-breaking-changes.md) — that
entry, not this section, is the authority on what the CLI must absorb. (This
section was written as the pre-landing worklist, enumerating the `--exchange-rate`
options and import-path payloads to remove; the call-site table is in git history.)

**On the rejected escape hatch.** Firefly III lets a user override a converted
amount, and does it by storing a **second amount** ("this cost me S/152"), never a
rate. That shape is strictly better — an amount is readable off a statement; a
rate has to be computed to enter and re-multiplied to use. It is also unnecessary
here: under account-governs-currency there is no second amount to record for an
ordinary transaction. If per-transaction foreign amounts are ever wanted, the
correct model is an amount pair, as a new feature, not a rate column.

---

## Migration cost

**`expense_transactions` and `expense_reconciliations` both held 0 rows (verified
2026-08-01, re-verified before `sql/021` ran).** There was no data migration.

What it actually cost, for the record: one migration dropping three columns; the
whole dominant-side block deleted from `helpers/transfers.py`, taking its
`user_settings` round-trip with it; the re-rate blocks deleted from
`helpers/transactions.py` and `helpers/inbox.py`; `lookup_exchange_rate` and
`errors.rate_unavailable` deleted as unreachable; `resolve_home_rates` deleted
with the reconciliation home fields; `helpers/monthly_report.py` rebuilt around
`helpers/home_currency.py`'s lateral join; the dashboard's two archived-lifetime
aggregators deleted outright.

The lateral join is simple because only two currencies exist: PEN resolves to
1.0, USD to one row per date.

Two things the estimate missed, both worth knowing before a similar change:

- **The write path stops being able to fail.** Deleting the rate lookup deleted a
  whole error code, and with it a test that asserted the write must 422. That test
  had to be deleted deliberately, not adapted — its replacement asserts the
  opposite outcome for the same request.
- **`SUM` does not propagate the null.** Making the per-row value nullable is the
  easy half. `SUM(CASE WHEN x > 0 THEN x ELSE 0 END)` scores a null as **zero**, so
  every aggregate needed a paired `unconverted_count` and a Python-side null-out.
  See "Missing-rate policy" above — it is the part most likely to be quietly
  dropped by a future edit.

---

## Research basis

The "store native, keep historical rates, convert at report time" rule is what
the field converged on independently:

- **hledger / ledger-cli** — valuation is a report-time flag (`-V/--value`), not
  stored data. Their guidance: computed values *"are intended to be reported in
  parenthesis or otherwise distinguished to make it clear this is a view and not
  core data."*
- **Firefly III** — *"Amounts will always be in the currency of the associated
  object."* Converted values live in separate `pc_*` fields that are `null` unless
  the user opts in. Firefly also **proves the cost of the stored model**: it ships
  `php artisan correction:recalculate-pc-amounts` because *"if you (significantly)
  change the exchange rates, you may want to recalculate"* and *"if you change your
  primary currency, all amounts will have to be recalculated."* That is the
  222-line helper deleted in WP1.1, promoted to a permanent user-facing chore.
- **Lunch Money** — transactions stay in their original currency; a historical rate
  per transaction date rolls up into the primary currency.
- **Cross-app comparison verdict** — the best support requires *"preserving
  original currency amounts, maintaining historical exchange rates, and providing
  unified reporting in a home currency."*
- **YNAB** — the counter-model. Officially recommends **a separate budget per
  currency** with a `Currency Transfer` category in each. Legitimate, but the
  reported failure mode is this project's exact profile: *"If you're an expat
  earning in one currency and spending in another with bank accounts in multiple
  countries, YNAB starts to fight you."*
- **Actual Budget** — the warning. Currency-agnostic, and the consequence is
  stated plainly: *"A report that spans both is just adding raw numbers of
  different currencies together, which isn't meaningful."* **Hard constraint taken
  from this: never emit a total that sums across currencies without converting.**
- **GnuCash / Peter Selinger** — converting a cross-currency transfer at a single
  spot rate violates double-entry; the imbalance *is* the FX gain/loss and must be
  recorded, not ignored. Names what the current `sibling_home = primary_home`
  forcing does.
- **Spreadsheet practice** — two columns, conversion by `GoogleFinance()` formula.
  A spreadsheet formula *is* read-time conversion. Standard advice: *"The
  conversion to your home currency happens at the end, when you're reviewing your
  budget, not when you're standing at the checkout."*

Sources:
[hledger currency conversion](https://hledger.org/currency-conversion.html) ·
[Firefly III exchange rates](https://docs.firefly-iii.org/explanation/financial-concepts/exchange-rates/) ·
[Firefly III API](https://docs.firefly-iii.org/references/firefly-iii/api/) ·
[Lunch Money multicurrency](https://support.lunchmoney.app/settings/multicurrency) ·
[YNAB multiple currencies](https://support.ynab.com/en_us/using-multiple-currencies-in-ynab-a-guide-SyBF6PHno) ·
[Actual Budget multi-currency](https://actualbudget.org/docs/budgeting/multi-currency/) ·
[Selinger, multi-currency accounting in GnuCash](https://www.mathstat.dal.ca/~selinger/accounting/gnucash.html) ·
[Budgeting apps compared](https://borderlessbudget.com/compare/budgeting-apps-with-multi-currency) ·
[How to budget in two currencies](https://borderlessbudget.com/blog/budget-two-currencies)

---

## Product states considered

| State | Description | Verdict |
|---|---|---|
| **1** | No cross-currency ever. Two parallel ledgers, reports hard-partitioned by currency, `exchange_rates` and both FX jobs deleted. | **Rejected.** This is YNAB's official model and it is legitimate, but it costs the single-number month — "S/3,200 and $450" instead of one figure — and its documented failure mode is this project's exact profile. |
| **2** | Native storage, conversion in reporting only. | ✅ **Adopted.** Already how balances and reconciliations work. |
| **3** | Everything mixed and stored. | **Rejected.** The current *intent*, never coherently achieved. Requires restoring the recalc helper, unlocking `main_currency`, and owning the sync burden permanently — Firefly III demonstrates it never goes away. |
