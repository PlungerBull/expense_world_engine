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
- Backfilling historical rates for transactions that were written before the cron started running — that's Step 9.1 (`Home Currency Recalculation`).
- Adopting the whole service into `render.yaml` / Blueprint. The web service is currently dashboard-managed; adding a Blueprint would conflict. Keep everything dashboard-managed for now.
