# TODO

Operational / deployment tasks that are not part of normal code review. Each entry describes what needs to happen, why, and when it becomes blocking.

## Wire up the Render Cron Job for daily exchange-rate fetching

**What:** Create a new Render Cron Job service that runs `python -m app.jobs.fetch_exchange_rates` daily.

**Why:** Without it, no rows are ever written to the `exchange_rates` table. Any write that needs a cross-currency conversion (`POST /transactions`, `PUT /transactions/{id}` with a date change, `POST /transactions/batch`, `POST /inbox`, `PUT /inbox/{id}` with a date change) now fails with `422 RATE_UNAVAILABLE`. Same-currency writes still succeed (identity rate short-circuit in `get_rate`), so a single-currency user is unaffected.

**When it becomes blocking:** the moment any user holds an account in a currency other than their `main_currency`. Until then, same-currency writes succeed and nothing is corrupted — but the first cross-currency write 422s until the cron populates a rate row.

**Steps (one-time, via Render dashboard):**

1. Render dashboard → **New +** → **Cron Job**
2. Connect the same GitHub repo as the web service
3. **Name:** `fetch-exchange-rates`
4. **Runtime:** Python
5. **Build Command:** `pip install -r requirements.txt`
6. **Command:** `python -m app.jobs.fetch_exchange_rates`
7. **Schedule:** `0 16 * * *` (daily 16:00 UTC — after ECB's ~16:00 CET publish in both DST variants)
8. **Environment:** link the same env group as the web service so it picks up `SUPABASE_DB_URL` and friends
9. Save, then click **Trigger Run** once to smoke-test. Check the logs for `inserted USD->PEN <date> = <rate>`.

**Verification after first run:**
```
GET /v1/exchange-rates?target=PEN&base=USD
```
Should return the rate just inserted, not 404.

**Not in scope here:**
- Adopting the whole service into `render.yaml` / Blueprint. The web service is currently dashboard-managed; adding a Blueprint would conflict. Keep everything dashboard-managed for now.

---

## Backfill historical exchange rates (manual, user-owned)

**What:** Populate `exchange_rates` with per-date rows going back to the earliest transaction date in the system, so historical transactions can be re-converted with accurate point-in-time rates.

**Why:** The daily cron only inserts `/latest` going forward. Any historical transaction written while the old silent `1.0` fallback was in place (pre-fix; see commit that introduced `RATE_UNAVAILABLE`) will have an incorrect `amount_home_cents` and `exchange_rate` until the historical rates exist in the table and `PUT /auth/settings` (or a manual recalc) is re-run. Post-fix writes can no longer create this corruption, but any rows seeded before the fix need remediation.

**Owner:** User (PlungerBull) will handle this directly against the database — not via engine code.

**When:** at the very end of engine work, after all of Step 9 (Parts A + B) and Step 9.1 ship, and immediately before moving on to the CLI repo. This is the last cleanup task for the engine.

**Reference:** Frankfurter supports per-date queries — `https://api.frankfurter.app/YYYY-MM-DD?from=USD&to=PEN` returns the closing rate for that specific date. Any backfill script or manual run should use this and insert rows canonically as `(base_currency='USD', target_currency=<X>, rate_date=<date>, rate=<rate>)`, matching the daily cron's format.

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
