# TODO

Operational / deployment tasks that are not part of normal code review. Each entry describes what needs to happen, why, and when it becomes blocking.

> **Removed 2026-08-01: "Wire up the Render Cron Job for daily exchange-rate fetching."** Never an open task on the local profile — the job runs as a daily launchd agent ([deploy/local/README.md](deploy/local/README.md), roadmap Step 11.5). The Render recipe it carried now lives where it is actually needed, in [deploy/cloud/README.md](deploy/cloud/README.md) step 5 (cloud reactivation). Full original entry in git history.

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

---

## Home-currency recalculation: switch from per-row UPDATEs to bulk SQL

**What:** Replace the per-row `conn.execute(UPDATE ... WHERE id=$1)` loops in [app/helpers/recalculate_home_currency.py](app/helpers/recalculate_home_currency.py) with one `UPDATE ... FROM (VALUES ...)` per pass, so all rows for a pass are rewritten in a single round-trip.

**Why:** Today, all three recalc passes (regular transactions, transfer pairs, inbox items) iterate rows in Python and fire one UPDATE per row. At ~2-5 ms per Render ↔ Supabase round-trip, a user with ~10,000 transactions takes ~30 s — right at Render's HTTP timeout. Because the recalc runs synchronously inside `PUT /auth/settings`, a timeout leaves the user stranded: the transaction rolls back, balances stay in the old currency, and retries hit the same wall. Correctness is fine; this is purely throughput.

**Priority:** Low. Defer until one of these triggers fires:
- A real user reports a timeout when changing `main_currency`, OR
- The platform hits ~10k active users, OR
- A single user's transaction count (per `SELECT count(*) FROM expense_transactions WHERE user_id=X AND deleted_at IS NULL`) approaches 5k — at that point they're close to the ceiling.

**Cheapest fix (recommended first pass):** one bulk `UPDATE` per pass using `UPDATE ... FROM (VALUES ...)`. Python still does the rate math and builds the VALUES list; only the writes get batched. Drops ~10,000 round-trips to ~3. ~20-line change, no new infrastructure. Tests in [tests/test_home_currency_recalc.py](tests/test_home_currency_recalc.py) already cover the correctness cases and should keep passing.

**Bigger fix (only if bulk SQL isn't enough):** the async job path the spec already flags — return `{"recalculation_job_id": ..., "status": "running"}` immediately, run on a worker, expose `GET /auth/recalculation-jobs/{id}` for polling. Adds a job table and worker runtime. See `engine-spec.md` §Auth for the spec hook.

**Related (historical):** the silent `1.0` fallback in `lookup_exchange_rate` and the same-`rate_date` cross-rate JOIN (both in [app/helpers/exchange_rate.py](app/helpers/exchange_rate.py)) used to compound the risk — missing rate rows could silently produce wrong `amount_home_cents`. Both are fixed: lookups now raise `422 RATE_UNAVAILABLE`, and cross-rate is explicitly unsupported under the PEN/USD-only policy. The bulk recalc path uses `get_rate` directly and already treats `None` as "skip the row", so there's no remaining interaction between missing rates and recalc correctness.
