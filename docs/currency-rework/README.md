# Currency Rework — Work Packages

**Created 2026-08-01. Transient.** These are execution documents. Delete or
archive the directory once CR5 lands. The permanent record of *what* was decided
and *why* is [`../currency-model-decision.md`](../currency-model-decision.md),
which is indexed in `CLAUDE.md` and stays.

---

## For an agent picking up one package

You do not need any conversation history. Read, in this order:

1. **`CLAUDE.md`** at the repo root — conventions, and the "The engine comes
   first" section which governs every judgement call in this rework.
2. **[`../currency-model-decision.md`](../currency-model-decision.md)** — the
   model. What is stored, what is computed, what `@Transfer ≠ 0` means. ~20 min
   read, non-optional.
3. **Your package file** — `CR1`…`CR6` below.

Then work only inside your package. Each one has a **Done when** checklist; when
it passes, stop. Do not opportunistically fix things belonging to a later package
— the ordering exists so each package leaves the repo green.

---

## The one-paragraph version

The engine stores `amount_home_cents` and `exchange_rate` on every transaction —
values *derived* from the rate table that then have to be maintained by hand as
their inputs change. They aren't maintained, which is what audit findings 1.3, 1.4
and 1.5 are. We are deleting those columns. Every account keeps its money in its
own currency, and the PEN value is computed at read time from `exchange_rates`
using the rate on the transaction's date. Two of the engine's four home-value
surfaces (account balances, reconciliations) already work this way — this finishes
a conversion that was half done.

**Revised 2026-08-02 (D-e, D-g, D-h, D-i).** The rework got narrower than that
paragraph implies. PEN now appears on **one** surface — the monthly report's
category and hashtag rows plus its month totals. Individual records, account
balances and reconciliations report native currency only, so the two surfaces named
above stop converting rather than being finished. Seven conversion mechanisms
collapse to one: `helpers/home_currency.py`.

---

## Packages

| # | Package | Scope | Ends green? |
|---|---|---|---|
| **CR1** | [Conversion helper](CR1-conversion-helper.md) | new `app/helpers/home_currency.py` + parity test. Nothing wired. | 🟢 zero behaviour change |
| **CR2** | [Read paths](CR2-read-paths.md) | four deletion steps (D-g, D-e, D-i, D-h) then one construction step — the monthly report computes instead of reading the column. **Behaviour changes here.** | 🟢 |
| **CR3** | [Write paths + migration](CR3-write-paths-and-migration.md) | writes stop resolving rates; `sql/019` drops the columns | 🟢 |
| **CR4** | [Fail-closed sweep](CR4-fail-closed.md) | allow-lists, `extra="forbid"`, system-category lock | 🟢 |
| **CR5** | [Docs](CR5-docs.md) | spec, schema-reference, audit plan, client-breaking-changes | 🟢 |
| **CR6** | [CLI handoff](CR6-cli-handoff.md) | **separate repo** — `expense_world_CLI` | 🟢 |

### Dependency graph

```
CR1 ──> CR2 ──> CR3 ──> CR4 ──> CR5
                  │
                  └──> CR6  (other repo, any time after CR3)

CR4 §5c (system-category lock) touches only helpers/validation.py
        └── may be pulled forward and run standalone at any point
```

**Strictly serial for the engine.** Each package consumes the previous one's
output. Do not parallelise CR1–CR5.

**Every package must end with `pytest` green** (no flags, against
`expense_world_test`). **178 tests** pass as of 2026-08-02 (CR1 added 13); expect a
lower count after CR2's and CR3's deletions. If a package leaves the suite red, it
is not done.

---

## Phase 0 — prerequisites (do once, before CR1)

1. ✅ **Principle recorded** in `CLAUDE.md` → "The engine comes first".
2. ✅ **WP1.1 work committed** (`edf0d3a`) — `sql/018`, the deleted
   `recalculate_home_currency.py`, and the new `currency-model-decision.md` /
   `scaling-boundaries.md` / `client-breaking-changes.md`.
3. ⬜ **Test database exists.** `deploy/local/create-test-db.sh`. Re-run with
   `--force` after CR3's migration.

---

## Decisions already made — do not relitigate

| # | Decision | Rationale |
|---|---|---|
| **D-a** | `exchange_rate` is dropped from **responses**, not just from writes | A rate belongs to a (currency, date) pair, not to a transaction. `GET /exchange-rates` already serves it. Keeping it on the wire preserves the exact mental model that caused 1.3/1.4/1.5. |
| **D-b** | A write whose date has no resolvable rate **succeeds and warns** | Recording reality must not depend on a rate lookup. The warning names the missing pair and date so the operator knows to run the backfill. |
| **D-c** | Currency rework precedes the reconciliation-chaining retirement | Three open 🔴s vs one 🟠 whose findings are already superseded on paper. |
| **D-d** | **`@FX` category is deferred**, not rejected | Specified in the decision doc §"Deferred: `@FX`". Report-layer only to add later — no migration, no data change. **Do not build it in this rework.** |
| **D-e** | **Individual transactions and inbox items carry no PEN value.** `amount_home_cents` is removed from their responses, not recomputed. *(2026-08-02, mid-CR2)* | A PEN account shows soles, a USD account shows dollars; rows within one account are all the same currency, so a second number per row is noise. Consolidation belongs where figures are combined — the report. The field was computed, stored, shipped and rendered by nobody. **This deleted CR2's largest step** (re-reading every row after every write, ~12 call sites) and the dual-implementation risk with it. |
| **D-f** | **`unconverted_count` is reported at both levels** — per category / hashtag row, and once per report | The per-row count is already computed to decide whether to null the row, so it is free; the report-level one is what makes a user notice, since a blank cell is easy to skim past. ⚠️ The roll-up must be `COUNT(DISTINCT t.id)` — a transaction appears in both its category row and its hashtag row. |
| **D-g** | **`archived_categories` and `archived_hashtags` are deleted from `/dashboard`.** `archived_accounts` stays. *(2026-08-02, mid-CR2)* | No rationale was ever recorded for them (commit `296083a` is titled only "archive categories/hashtags"). `compute_month_flow`'s category query has no `is_archived` filter, so archived categories already appear in every monthly report, and `/reports/monthly` accepts a 24-month range — the lifetime figure is a roll-up of something already obtainable. Both aggregators also omitted the `@Opening` exclusion the monthly report has, so every lifetime total was inflated and nobody noticed. An archived *account* still holds real money; an archived *category* holds only history. |
| **D-h** | **Cross-account aggregates report PEN only.** `spent_cents` (category and hashtag rows) and `inflow_cents` / `outflow_cents` / `net_cents` (month totals) are removed. *(2026-08-02, mid-CR2)* | `monthly_report.py:148,174,216,218` groups by category with no currency partition, so a category spanning both accounts sums `$15 + S/25 = 4000` — a number in no currency. This violates the hard constraint at `../currency-model-decision.md:488`: *"never emit a total that sums across currencies without converting."* **Categories and hashtags are assumed to span currencies rather than checked**, so there is no native aggregate worth preserving — not as a nullable field, not as a per-currency map. |
| **D-i** | **Per-account figures are native only.** `current_balance_home_cents` is removed from accounts and `/sync`; `beginning_/ending_balance_home_cents` from reconciliations. **No cross-account total replaces them** — the engine reports no net worth. *(2026-08-02, mid-CR2)* | A balance and a reconciliation are scoped to one account, therefore one currency, so the native figure is already complete. The absence of a total is deliberate: accounts are read in their own currency. **This deletes three of the seven conversion mechanisms** — `resolve_home_rates`, `get_home_balance`, `batch_get_rates` all lose every caller — and closes five review findings by deletion: the UTC `date_end` rate (F10), the UTC `today` rate (F11), three of four duplicate `main_currency` queries (F12), the double `get_home_balance` per mutation that made the activity log non-reproducible (F13), and an account query missing its `user_id` filter (F17). |

---

## Invariants that must survive

Check these before declaring any package done:

- **Sign convention.** Requests signed, storage positive, responses positive.
  Untouched by this rework — if a diff changes sign handling, it is wrong.
- **Transfers stay in reports.** Never exclude `transaction_type = 3` from totals.
  Same-currency transfers must still net to exactly 0.
- **`@Transfer ≠ 0` means exactly two things:** an FX spread (both legs in, valued
  at different home amounts) or a loan/repayment with a person (one leg in,
  nothing to cancel against). Any third cause is a bug. See the decision doc.
- **Null over omission.** Optional fields return `null`, never absent. Two
  deliberate exceptions, both *removed* from the schema rather than nulled:
  `exchange_rate` (CR3) and `amount_home_cents` on transactions / inbox items (CR2).
- **Currency appears by level, not by endpoint.** The rule, from D-e / D-h / D-i:

  | Level | Native | PEN |
  |---|---|---|
  | Individual records — transactions, inbox | **only** | none (D-e) |
  | Per-account figures — balances, reconciliations | **only** | none (D-i) |
  | Cross-account aggregates — category, hashtag, month totals | none (D-h) | **only** |

  A record is in one currency. An account is in one currency. Only an aggregate
  spans currencies. So conversion belongs at exactly one level and nowhere else,
  and it is computed at read time, never stored.
- **Visible rows sum exactly to the month total.** `scaling-boundaries.md:28`,
  filed as never-trade-away business logic. ⚠️ It is worded against `net_cents`,
  which D-h deletes — CR5 restates it as `net_home_cents`; the invariant itself is
  unchanged. It is also what forces archived categories to **stay** in the monthly
  report: the category list and the totals come from two independent queries, so
  filtering archived out of the list would strand their transactions inside the
  total with no row explaining them.
- **Balance atomicity, idempotency, activity log, soft delete** — untouched.

---

## What is NOT in this rework

- `@FX` category (D-d)
- Reconciliation-chaining retirement — next up, see [`../../TODO.md`](../../TODO.md)
- The full `extra="forbid"` sweep — WP6.1 of the audit plan; CR4 does only the
  transaction/inbox schemas
- Remaining WP1.7 rate hygiene: provider-rate validation in the FX jobs, the
  negative-lookup cache TTL, archived-account currencies in the fetch target list,
  `Decimal`/`ROUND_HALF_UP`. These now apply to the rate table and read-time math,
  and should follow immediately after CR5.
