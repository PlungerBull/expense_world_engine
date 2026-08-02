# TODO

Operational / deployment tasks, plus accepted design changes awaiting scheduling — work that is not part of normal code review. Each entry describes what needs to happen, why, and when it becomes blocking.

> **Removed 2026-08-01: "Wire up the Render Cron Job for daily exchange-rate fetching."** Never an open task on the local profile — the job runs as a daily launchd agent ([deploy/local/README.md](deploy/local/README.md), roadmap Step 11.5). The Render recipe it carried now lives where it is actually needed, in [deploy/cloud/README.md](deploy/cloud/README.md) step 5 (cloud reactivation). Full original entry in git history.

> **Removed 2026-08-01: "Home-currency recalculation: switch from per-row UPDATEs to bulk SQL."** Closed, not deferred — the target narrowed from 1000+ public users to the owner alone that day, which retires the entire premise. Its stated trigger was Render's HTTP timeout, and Render left the path at Step 11; the follow-on concern (a long synchronous recalc pinning a pool connection) only bites with concurrent users. At one user the recalc is fast, rare, and correct. Full original entry in git history.
>
> **Superseded later the same day:** the recalculation itself is now **gone**. The home currency is locked to PEN (`sql/018`), `PUT /auth/settings` rejects `main_currency` with `422`, and `app/helpers/recalculate_home_currency.py` was deleted along with its tests — it carried a silent `1.0` rate fallback (audit finding WP1.1). The sentence above claiming "nothing was deleted" no longer holds. Row 1 of [docs/scaling-boundaries.md](docs/scaling-boundaries.md) is retired with it. The dominant-side zero-sum logic *does* survive, in [app/helpers/transfers.py](app/helpers/transfers.py).

**One open item** (next up) and **one parked item**. Everything below them is a closed record, kept for the dated history.

---

## Retire reconciliation chaining — replace with explicit values + a continuity check — 🔵 OPEN, decided 2026-08-01

**Owner decision D3** of [docs/audit-2026-08-01-remediation-plan.md](audit-2026-08-01-remediation-plan.md). Tracked here rather than as an audit work package because it is a design change, not a defect fix — it *deletes* the code three audit findings were about.

**What:** Remove the chained beginning-balance system. Every reconciliation stores `beginning_balance_cents` as a plain value. On `POST`, the engine prefills it from the previous row's `ending_balance_cents` as a **one-time suggestion** — no live link, no recomputation afterwards. Replace the cascade with a read-time continuity indicator on every reconciliation response:

- `previous_ending_balance_cents` — `null` when the row is first in `sort_order`
- `continuity_gap_cents` — `beginning_balance_cents − previous_ending_balance_cents`; `null` when there is no previous row

`0` means the chain is continuous; any non-zero value is where the chain breaks, and the number *is* the size of the discrepancy.

**Why:** today the cascade (`_cascade_chained_recalc`, `app/helpers/reconciliations.py:190`) walks downstream on every ending-balance change and rewrites `beginning_balance_cents` — with **no status predicate**. So editing an upstream draft silently rewrites the locked beginning balance of a `COMPLETED` reconciliation, doing through the back door exactly what §646's field lock refuses at the front door. A reconciliation you signed off against a real bank statement should not move because you corrected a typo three rows earlier. A discrepancy is information to surface, not to auto-repair.

**What it deletes:**

| Removed | Why |
|---|---|
| `_cascade_chained_recalc` (~90 lines) + its 5 call sites (`:467`, `:625`, `:848`, `:897`, reorder) | no chain to propagate |
| Audit finding **WP5.1** — cascade early-stop unsound on reorder/restore | no cascade |
| Audit finding **WP5.2** — cascade rewrites `COMPLETED` rows (this decision) | no cascade |
| Audit finding **WP5.4** — reorder response omits cascade-affected rows | no cascade-affected rows |
| `beginning_balance_source` column + enum + §650 ambiguity guard | every row is now an explicit value |
| CLI chained/manual picker (`expense/tui/screens/reconciliations.py:311-330`) and `--source` flag (`expense/commands/reconcile_cmd.py:47`) | nothing left to pick |

Bulk reorder becomes a pure `sort_order` write with no balance math, and the §646 completed-lock becomes real and unconditional.

**Plumbing that already exists:** `_serialize_with_neighbor` (`reconciliations.py:133`) already fetches each row's neighbors, so both new fields are computed in a query that already runs. No new storage.

**What you give up:** auto-correction. Fixing an upstream ending balance no longer updates downstream rows — `continuity_gap_cents` flags the break and each row is corrected deliberately. That is the intended trade: the auto-correction was corrupting signed-off batches.

**When: now, before data accumulates.** `expense_reconciliations` currently holds **0 rows** and `expense_transactions` holds **0 rows** (verified 2026-08-01), so the migration that would normally have to freeze every existing chained row to its computed value is a no-op today. This is the cheapest this change will ever be.

**Scope beyond code:** spec rewrite — 15 occurrences of "chained", sections §588-607, §620, §648, §650, §671-681, mostly deletion; `schema-reference.md` column removal; migration to drop `beginning_balance_source`; CLI simplification in the two files above.

---

## Split transactions (`parent_transaction_id`) — 🅿️ PARKED, reviewed 2026-08-01

**Owner decision D8** of [docs/audit-2026-08-01-remediation-plan.md](audit-2026-08-01-remediation-plan.md). Parked here so the reserved field has a tracked home and is not rediscovered as a mystery by the next audit.

**Status:** the column exists (`sql/003_expense_tables.sql:85`, self-referencing FK on `expense_transactions`), the field is on the wire in every transaction response (`app/schemas/transactions.py:63,114`), and it is **always `null`** — no endpoint accepts or writes it. This is an unbuilt feature, not a defect and not a vestige.

**Why it stays:** unlike the People API (D7), the docs already tell the truth — spec §414 documents it as *"reserved, always `null` in v1"*, targets Phase 5, and instructs clients not to build logic on it. Nothing to correct. Retiring it would cost a migration plus a rewrite of `schema-reference.md §Split Transactions` to reclaim one nullable column and one null JSON key.

**What shipping it would mean** (recorded now so the design isn't re-derived later): one parent row holds the full amount and is a **display container that does not move the balance**; child rows hold the portions and are the only rows that touch `current_balance_cents` (`schema-reference.md:402`). Splits must be created atomically in a single API call. Both halves interact with soft-delete, the activity log and the balance rules, so this is a real work package, not a field to start populating.

**When it becomes blocking:** never on its own. Revisit when you actually want to split a receipt across categories — or, if you decide splits will never ship, close this entry and retire the column and field together.

---

## Backfill historical exchange rates (manual, user-owned) — ✅ DONE 2026-07-31

> **Shipped as a job, not a one-off:** `app/jobs/backfill_exchange_rates.py`.
> `python -m app.jobs.backfill_exchange_rates --from 2024-03-02` inserted 881
> daily USD→PEN rows spanning 2024-03-02 → 2026-07-31. Idempotent and resumable
> — it skips dates already present before making any HTTP call, so re-run it
> freely to widen the range. Spot-checked against the provider at full 8dp
> precision; no day-over-day move above 5% in the whole series.
>
> **Two findings worth keeping:**
> 1. **You need far less history than it looks.** `get_rate` resolves with
>    `rate_date <= $1 ORDER BY rate_date DESC LIMIT 1` — it carries the last
>    earlier rate forward. So the hard requirement is *one row on or before your
>    earliest transaction date*; denser coverage buys accuracy, not availability.
>    Weekends and holidays need no handling at all.
> 2. **The provider floor is 2024-03-02.** Every earlier date 404s, including
>    the legacy `@1/<date>/` path (verified 2026-07-31). The job refuses earlier
>    `--from` values rather than leaving a silent hole. Pre-March-2024 history
>    needs a different source. One genuine gap exists inside the range —
>    2025-12-10, absent from the provider's dataset — which carry-back covers.

**What:** Populate `exchange_rates` with per-date rows going back to the earliest transaction date in the system, so historical transactions can be re-converted with accurate point-in-time rates.

**Why:** The daily cron only inserts `/latest` going forward. Any historical transaction written while the old silent `1.0` fallback was in place (pre-fix; see commit that introduced `RATE_UNAVAILABLE`) will have an incorrect `amount_home_cents` and `exchange_rate` until the historical rates exist in the table and `PUT /auth/settings` (or a manual recalc) is re-run. Post-fix writes can no longer create this corruption, but any rows seeded before the fix need remediation.

**Owner:** User (PlungerBull) will handle this directly against the database — not via engine code.

**When:** ~~at the very end of engine work~~ **Updated 2026-07-30:** folded into local-deployment Step 11.5 ([docs/roadmap.md](docs/roadmap.md)) — runs right after the data migration and daily-fetch verification, followed by a home-currency recalc, so PEN/USD history converts at true point-in-time rates. Easier now: the target database is local Postgres, no pooler in the way.

**Reference (provider corrected 2026-07-30):** Frankfurter **cannot** serve this — it carries ECB reference rates only, and the ECB list has no PEN (discovered when the daily job first ran for real; the 15 pre-existing `rate=3.75` rows turned out to be hand-inserted placeholders, ~10% off market, and were deleted). The provider is now **fawazahmed0/currency-api** (keyless, CDN-hosted), which the daily job uses via `app.jobs.fetch_exchange_rates`. It supports dated queries for backfill — `https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@YYYY-MM-DD/v1/currencies/usd.min.json` returns all USD rates for that date (lowercase codes) — and `_fetch_currency_api(version="YYYY-MM-DD")` in the job module already wraps this. Insert rows canonically as `(base_currency='USD', target_currency=<X>, rate_date=<date>, rate=<rate>)`, matching the daily job's format. **Run the backfill before importing historical spreadsheet data** — cross-currency writes for dates without rates fail with `422 RATE_UNAVAILABLE` by design.

