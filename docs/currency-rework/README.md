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

---

## Packages

| # | Package | Scope | Ends green? |
|---|---|---|---|
| **CR1** | [Conversion helper](CR1-conversion-helper.md) | new `app/helpers/home_currency.py` + parity test. Nothing wired. | 🟢 zero behaviour change |
| **CR2** | [Read paths](CR2-read-paths.md) | reads compute instead of reading the column. **Behaviour changes here.** | 🟢 |
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
`expense_world_test`). 165 tests pass as of 2026-08-01; expect a lower count after
CR3's deletions. If a package leaves the suite red, it is not done.

---

## Phase 0 — prerequisites (do once, before CR1)

1. ✅ **Principle recorded** in `CLAUDE.md` → "The engine comes first".
2. ⬜ **Commit the staged WP1.1 work.** The tree currently holds uncommitted
   changes: `sql/018`, `helpers/auth.py`, `schemas/auth.py`, the deleted
   `recalculate_home_currency.py` + its tests, doc updates, and the new
   `currency-model-decision.md` / `scaling-boundaries.md` /
   `client-breaking-changes.md`. Commit before starting CR1 so each package is a
   clean diff.
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
- **Null over omission.** Optional fields return `null`, never absent. The one
  deliberate exception is `exchange_rate`, which is *removed* from the schema
  entirely, not nulled.
- **`amount_home_cents` stays in every response** — computed, not stored. The
  response contract does not change; only writes do.
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
