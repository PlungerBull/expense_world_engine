# TODO

Operational / deployment tasks that are not part of normal code review. Each entry describes what needs to happen, why, and when it becomes blocking.

## Wire up the Render Cron Job for daily exchange-rate fetching

**What:** Create a new Render Cron Job service that runs `python -m app.jobs.fetch_exchange_rates` daily.

**Why:** Without it, no rows are ever written to the `exchange_rates` table. `lookup_exchange_rate` falls back to `1.0` on every lookup, so `amount_home_cents` gets written equal to `amount_cents` on every new transaction — which makes `/v1/dashboard` and `/v1/reports/monthly` show wrong home-currency totals for any account that isn't in the user's main currency.

**When it becomes blocking:** before Step 9 Part A can be considered production-ready. The code is already deployed; the cron is what turns it on.

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

**Why:** The daily cron only inserts `/latest` going forward. Any historical transaction whose `amount_home_cents` was computed with the `1.0` fallback (or with a mismatched date) will stay wrong until the historical rates exist in the table and are used to recalculate.

**Owner:** User (PlungerBull) will handle this directly against the database — not via engine code.

**When:** at the very end of engine work, after all of Step 9 (Parts A + B) and Step 9.1 ship, and immediately before moving on to the CLI repo. This is the last cleanup task for the engine.

**Reference:** Frankfurter supports per-date queries — `https://api.frankfurter.app/YYYY-MM-DD?from=USD&to=PEN` returns the closing rate for that specific date. Any backfill script or manual run should use this and insert rows canonically as `(base_currency='USD', target_currency=<X>, rate_date=<date>, rate=<rate>)`, matching the daily cron's format.
