# Currency Model — Decision, 2026-08-01

**Status: decided, not yet implemented.** This document is the design record. The
code change it describes is scheduled ahead of WP1.3/1.4/1.5, which it deletes
rather than fixes.

---

## The decision, in one sentence

**Every account keeps its money in its own currency; the PEN value is calculated
when it is asked for, from `exchange_rates`, using the rate on the transaction's
date.**

Nothing converted is ever stored.

---

## Why this document exists

The engine had four surfaces that show a PEN value for non-PEN money, and each
used a different mechanism:

| Surface | Stores the rate? | Stores the home amount? | Strategy |
|---|---|---|---|
| Account balances | no | no — no `current_balance_home_cents` column exists | derived at read time |
| Reconciliations | no | no — `round(begin * rate)` at serialization (`schemas/reconciliations.py:107`) | derived at read time |
| Inbox | **yes** (`sql/003:62`, default `1.0`) | no — computed at serialization (`schemas/inbox.py:70`) | half derived |
| Transactions | **yes** (`sql/003:82`) | **yes** (`sql/003:76`) | fully stored |

`amount_home_cents` exists on exactly one table in the entire schema. Two of the
four surfaces already worked the way this decision mandates. **This is not a new
architecture — it is finishing a conversion that was already half done and never
completed.**

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
- **Cross-currency transfer.** Here a real rate exists. But the pair already
  stores `primary.amount_cents = 100000` (USD) and `sibling.amount_cents = 345000`
  (PEN). **The rate is `345000 ÷ 100000`.** The column stores a value derivable
  from two values already present.

**Therefore `expense_transactions.exchange_rate` never held information that was
not either recoverable or a reporting choice.** Dropping it loses nothing.

---

## Schema change

Migration `sql/019`.

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
| `amount_home_cents` on **reports and account balances** | computed instead of stored |

### Where PEN appears — amended 2026-08-02

> The first version of this document said *"the response contract does not change —
> every response with an amount includes a home-currency version."* **That was
> wrong**, and the owner corrected it while CR2 was being planned.

The rule is not "every amount." It is **figures the user compares or sums across
currencies**:

| Surface | PEN? |
|---|---|
| Monthly report — per category / per hashtag, single month **and** multi-month range | ✅ |
| Month totals — inflow / outflow / net | ✅ |
| Dashboard — archived lifetime panels | ✅ |
| Account balances (`current_balance_home_cents`) | ✅ — the only place you see all your money at once |
| **Individual transactions** | ❌ removed |
| **Inbox items** | ❌ removed |

A PEN account shows soles; a USD account shows dollars; their transactions show the
account's currency and nothing else. A second number on every ledger row is noise —
the rows in any one account are all the same currency anyway.

**Evidence this is right, not merely cheaper:** the CLI has **zero** references to
transaction-level `amount_home_cents`. It reads `current_balance_home_cents` and
`spent_home_cents` / `net_home_cents` only. The per-transaction value was computed
on every write, stored, serialised and shipped — and rendered by nobody. The CLI's
own docstring (`reports_cmd.py:80-81`) already calls home currency *"the only column
comparable across a multi-currency ledger."*

The field is **absent, not `null`** — a documented exception to null-over-omission,
same as `exchange_rate` and the retired `recalculation` field. A permanently-`null`
key on every transaction forever is dead weight.

`CLAUDE.md`'s home-currency convention was amended to match: the engine remains the
only thing that converts — that part never changes — but the obligation applies to
cross-currency figures, not to every amount.

**Known inconsistency, deliberately left:** reconciliations still expose
`beginning_balance_home_cents` / `ending_balance_home_cents` even though a
reconciliation belongs to one account and is therefore single-currency. The
chaining retirement reworks them wholesale; it settles this.

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

### Same-currency transfer

S/500, Savings → Checking.

```
leg A:  amount_cents=50000   (PEN account, debit)
leg B:  amount_cents=50000   (PEN account, credit)
```

Both convert at 1.0 → `−500 + 500 = 0`. Cancels exactly, as before.

### Cross-currency transfer

Send $1,000, receive S/3,450. Market rate that day 3.58.

```
before:  leg A: amount_cents=100000  exchange_rate=3.45  amount_home_cents=345000  ← FORCED
         leg B: amount_cents=345000  exchange_rate=1.0   amount_home_cents=345000

after:   leg A: amount_cents=100000   (USD account)
         leg B: amount_cents=345000   (PEN account)
```

No new column — the two legs already store their own native amounts. The rate is
recoverable as `3450 ÷ 1000 = 3.45` for display.

At read time:

```
USD leg:  100000 × 3.58  =  −S/ 3,580
PEN leg:  345000 × 1.00  =  +S/ 3,450
                            ───────────
                     net  =  −S/   130      ← the spread the bank charged
```

**Before, that S/130 was hidden** by forcing leg A's home value to 345000 instead
of 358000. It is a real cost really paid, and it now appears.

### USD → USD transfer (audit finding WP1.3)

```
before:  500 INTERNAL_ERROR — neither leg matches PEN, so the dominant-side
         rule reaches `raise RuntimeError` (transfers.py:166)

after:   leg A: amount_cents=50000
         leg B: amount_cents=50000
         report: both × 3.58 → −1790 + 1790 = 0
```

No home-currency match is needed because nothing stored requires one. **The bug
becomes unrepresentable.**

---

## `@Transfer` semantics

**`@Transfer` nets to zero only when both legs of a pair land in it.** Each leg is
categorised by its own account, independently (`transfers.py:99-108`):

```python
SystemCategoryKey.DEBT if primary_is_person else SystemCategoryKey.TRANSFER   # primary
SystemCategoryKey.DEBT if transfer_is_person else SystemCategoryKey.TRANSFER  # sibling
```

| Transfer | Primary leg → | Sibling leg → | `@Transfer` shows | New? |
|---|---|---|---|---|
| real ↔ real, same currency | @Transfer | @Transfer | **0** | no |
| real ↔ real, cross currency | @Transfer | @Transfer | **the FX spread** | ✅ **new** |
| real ↔ person | @Transfer | @Debt | **the full amount** | no — ships today |
| person ↔ person | @Debt | @Debt | untouched (`@Debt` gets 0 or the spread) | no |

So `@Transfer ≠ 0` means exactly one of two things:

1. **an FX spread** — both legs in, valued at different home amounts (new), or
2. **a loan or repayment with a person** — one leg in, nothing to cancel against.

Case 2 is pre-existing and documented: *"`spent_cents` can be negative for
income-dominant categories or for lending-out months on `@Transfer`/`@Debt`"*
(`roadmap.md:212`).

Both legs of a pair always share `primary_date` (`transfers.py:199,229`), so a
transfer can never straddle a month boundary and produce a false non-zero.

This preserves the standing rule that transfers stay visible in dashboards and
reports and are never excluded from totals.

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
rendered without complaint. That fallback is removed. It appears **12 times across
three sites**: `helpers/monthly_report.py:119-122` and `:198-201`, plus
`routers/dashboard.py:114-117` (`_SIGNED_HOME_CENTS_SQL`, used by both
archived-lifetime aggregators).

**A per-row `null` is not sufficient on its own.** `SUM` silently skips `NULL`s,
and `SUM(CASE WHEN x > 0 THEN x ELSE 0 END)` silently scores a `NULL` row as zero,
so an unflagged aggregate understates exactly like the fallback it replaced. Every
home-value `SUM` must be paired with a count of unconvertible rows, and a non-zero
count makes the aggregate `null` rather than a partial total. CR1 exports
`UNCONVERTIBLE_FLAG_EXPR` for this; CR2 §2 wires it.

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
  cannot reproduce it. The divergence is deliberate, and it disappears once CR3
  removes rate resolution from writes entirely.
- **`display_timezone` is unvalidated user input** (`sql/002:22`, settable via
  `PUT /auth/settings`). It must reach SQL as a bind parameter, never interpolated.
  `compute_month_bounds` silently falls back to UTC on an invalid zone name while
  `AT TIME ZONE` would raise — validating it on write is the root fix and belongs
  in the fail-closed sweep.

---

## Deferred: `@FX`

**Not shipping now. Free to add later.**

Splitting the FX spread into its own `@FX` category would let `@Transfer` mean
only "lending flow" and read 0 for every currency exchange:

```
shipping:   @Transfer  −S/ 130
deferred:   @Transfer     S/   0
            @FX        −S/ 130
```

This is what double-entry systems do — GnuCash routes cross-currency transfers
through per-currency *currency trading accounts* so the transaction stays
balanced in each currency separately, and the residual lands as an ordinary
income/expense line.

**Why it is deferred, not rejected.** `@FX` cannot be a normal category:

- **It cannot hold real transactions.** An FX row needs an `account_id` (the
  column is `NOT NULL`), but the spread lives *between* two accounts. And its
  value derives from that day's market rate, so storing it would re-introduce a
  stored derived value — precisely what this decision removes.
- **So it must be synthetic** — computed at report time by pairing legs on
  `transfer_transaction_id` and splitting the result:
  `@Transfer ← −3580 + 3580 = 0`, `@FX ← 3450 − 3580 = −130`.
- **Synthetic breaks an invariant.** A category's total is currently the sum of
  its transactions. `@FX` would have zero transactions and a non-zero total, and
  the rule that `hashtag_breakdown` rows sum to the parent total *by construction*
  would need a special case.

**Deferring costs nothing.** The stored facts are byte-identical either way — two
legs, two native amounts, no rate. Adding `@FX` later is a report-layer change
only: no migration, no data change, no write-contract change.

**Revisit when:** `@Transfer` carrying two meanings at once (money moved to a
person / cost of exchanging currency) becomes annoying in daily use. That is the
real argument for it, and real usage should decide.

---

## Decided: system categories are engine-assigned only

**Owner decision, 2026-08-01.** `@Transfer`, `@Debt` and `@Opening` may be
attached to a transaction **only** by the engine flow that owns them. Clients may
never set them by hand.

Without this, the `@Transfer` semantics above are unenforceable — the two
non-zero cases stop being the *only* two, and the category is no longer a
trustworthy FX indicator.

### Three holes, all currently open

| # | Hole | Where |
|---|---|---|
| 1 | `POST /transactions` accepts a system `category_id` | `validate_active_category` (`helpers/validation.py:94-114`) checks `deleted_at` and `is_archived` but **not `is_system`**; no other guard found |
| 2 | `PUT /transactions/{id}` can move an ordinary transaction *into* a system category | same missing check |
| 3 | `PUT /transactions/{id}` can move a transfer leg *out of* `@Transfer` | the transfer edit guard (`helpers/transactions.py:497-506`) blocks `amount_cents`, `account_id`, `date`, `exchange_rate`, `amount_home_cents` — **`category_id` is not in the set** |

Hole 3 is the mirror image of 1 and 2 and breaks the invariant just as
effectively: re-categorising one leg of a pair leaves the other stranded in
`@Transfer` with nothing to cancel against — indistinguishable from a loan.

### Fix

- Reject `is_system = true` category targets at the **public boundary** (request
  validation), returning `422`. The internal paths must keep working:
  `create_transfer_pair` assigns `@Transfer`/`@Debt`, and
  `create_opening_balance` delegates to `create_transaction` with `@Opening`.
  Same public-boundary-plus-internal-path shape as WP7.4.
- Add `category_id` to the transfer edit guard's blocked set.

Coordinate with **WP7.4**, which rejects reserved system-category *names* at the
same boundary. Together they make `@Transfer` mean exactly what this document
says it means.

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

## Client impact

**Breaking for the CLI.** `exchange_rate` becomes output-only; the write contract
no longer accepts it.

| Call site | Change |
|---|---|
| `expense/commands/log_cmd.py:34,94` | remove `--exchange-rate` |
| `expense/commands/inbox_cmd.py:226,278` | remove `--exchange-rate` (add + update) |
| `expense/commands/transactions_cmd.py:305` | remove `--exchange-rate` |
| `expense/commands/accounts_cmd.py:248` | remove `--exchange-rate` (opening balance) |
| `expense/import_/parse.py:206-213` | **delete the `usd-no-rate` skip.** A USD row lands on the USD account; no rate needed. |
| `expense/import_/apply.py:198-216` | stop sending `exchange_rate` in the payload |

⚠️ Append a full entry to [client-breaking-changes.md](client-breaking-changes.md)
when the code lands, not before.

**On the rejected escape hatch.** Firefly III lets a user override a converted
amount, and does it by storing a **second amount** ("this cost me S/152"), never a
rate. That shape is strictly better — an amount is readable off a statement; a
rate has to be computed to enter and re-multiplied to use. It is also unnecessary
here: under account-governs-currency there is no second amount to record for an
ordinary transaction, and the only place two real amounts exist — a cross-currency
transfer — already stores both. If per-transaction foreign amounts are ever
wanted, the correct model is an amount pair, as a new feature, not a rate column.

---

## Migration cost

**`expense_transactions` and `expense_reconciliations` both hold 0 rows (verified
2026-08-01).** There is no data migration. This is the cheapest this change will
ever be, and it shares that window with the reconciliation-chaining retirement
([TODO.md](../TODO.md)).

Work: one migration dropping three columns; ~60 lines deleted from
`helpers/transfers.py` (the whole dominant-side block, `:119-177`); the re-rate
guards deleted from `helpers/transactions.py:523,529` and `helpers/inbox.py:200`;
the 8 `COALESCE(t.amount_home_cents, …)` arms in `helpers/monthly_report.py`
replaced with a lateral join to `exchange_rates`; `routers/dashboard.py` likewise;
CLI changes above.

The lateral join is simple because only two currencies exist: PEN resolves to
1.0, USD to one row per date.

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
