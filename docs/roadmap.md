# Expense Tracker — Build Roadmap

> Build order: Engine → CLI → Web Dashboard → iOS. Nothing exists for any client until it is defined and working in the engine first.
> Full specs: `engine-spec.md` · `cli-spec.md` · `ios-spec.md`

---

## Step 0 — Accounts & Repos

Everything you need before writing a single line of code.

**Accounts to create (if not already):**
- GitHub account
- Supabase account (supabase.com)

**Create 4 private GitHub repos:**
- `expense_world_engine` — Python FastAPI backend
- `expense_world_cli` — Python Typer CLI
- `expense_world_web` — Next.js read-only dashboard on Vercel
- `expense_world_ios` — Swift / SwiftUI (can wait, but create the repo now)

**Local setup:**
- Clone `expense_world_engine` locally
- Create a Python virtual environment inside it
- Install FastAPI, Uvicorn, SQLAlchemy (or asyncpg), python-jose (JWT), Typer as starters

**Connect GitHub from day one.** Every step below ends with a commit and push. Small, frequent commits — one per logical unit of work.

---

## Step 1 — Supabase: Build the Schema

*Deliverable: a live Supabase database with all Phase 1 tables, RLS, and seed data.*

1. Create a new Supabase project. Note the Postgres connection string and JWT secret — you'll need both.
2. In the Supabase SQL editor, run the schema in this order:
   - Enable the `uuid-ossp` extension: `CREATE EXTENSION IF NOT EXISTS "uuid-ossp";`
   - Infrastructure tables: `users`, `user_settings`, `global_currencies`, `exchange_rates`, `sync_checkpoints`, `idempotency_keys`, `activity_log`
   - Expense tables: `expense_bank_accounts`, `expense_categories`, `expense_transaction_inbox`, `expense_transactions`, `expense_hashtags`, `expense_transaction_hashtags`, `expense_reconciliations`
3. Seed `global_currencies` with: USD, PEN (additional currencies deferred)
4. Enable Row-Level Security on every table and add the policy: `auth.uid() = user_id`
5. Write the trigger that auto-creates a `public.users` row whenever Supabase Auth creates a new `auth.users` row

**Verify:** All tables visible in Supabase table editor. RLS policies active. Trigger fires when a test auth user is created.

**Commit:** `feat: initial schema — all Phase 1 tables, RLS, seed data`

---

## Step 2 — Engine Skeleton

*Deliverable: a FastAPI app running locally, connected to Supabase, with a health check endpoint.*

1. Initialize the FastAPI project structure inside `expense_world_engine`
2. Add `.env` file (gitignored) with `SUPABASE_URL`, `SUPABASE_DB_URL`, `SUPABASE_JWT_SECRET`
3. Connect to Supabase via the Postgres connection string
4. Create one endpoint: `GET /health` → returns `{"status": "ok"}`

**Verify:** `GET /health` returns 200 locally.

**Commit:** `feat: engine skeleton — FastAPI + Supabase connection, health check`

---

## Step 3 — Auth Middleware + User Bootstrap

*Deliverable: JWT validation working. First real endpoints verified via Swagger.*

1. Build the JWT validation middleware:
   - Reads `Authorization: Bearer <token>` header
   - Verifies signature using `SUPABASE_JWT_SECRET`
   - Rejects expired or invalid tokens with `401`
   - Extracts `user_id` and injects it into the request context
2. Build `POST /auth/bootstrap` — creates `users` + `user_settings` rows if they don't exist (idempotent)
3. Build `GET /auth/me` — returns user profile + settings
4. Build `PUT /auth/settings` — partial update of `user_settings`
5. Wire up the OpenAPI/Swagger UI

**Verify:** Sign in via Supabase Auth dashboard → get a JWT → call `/auth/bootstrap` via Swagger → confirm rows appear in Supabase.

**Commit:** `feat: auth middleware, JWT validation, bootstrap + me + settings endpoints`

---

## Step 4 — Core Resources

*Deliverable: accounts, categories, and hashtags fully CRUD and verified.*

Build each resource group completely before starting the next. For each: list, create, get, update, soft-delete. Include validation, activity log writes, and correct error responses.

**4a — Bank Accounts**
- All CRUD endpoints
- `POST /accounts/{id}/archive`
- Validate `currency_code` exists in `global_currencies`
- Validate `currency_code` immutability on update

**Verify:** Create an account, update it, archive it, try to update currency (expect 422).
**Commit:** `feat: accounts CRUD — list, create, update, archive, soft-delete`

**4b — Categories**
- All CRUD endpoints
- System category auto-creation logic (`@Debt`, `@Transfer`) — internal engine function, not an endpoint
- Block rename/delete on `is_system = true` categories

**Verify:** Create a category, delete it, try to delete a system category (expect 403).
**Commit:** `feat: categories CRUD — list, create, update, soft-delete, system category protection`

**4c — Hashtags**
- All CRUD endpoints

**Commit:** `feat: hashtags CRUD`

---

## Step 5 — Inbox

*Deliverable: the inbox flow works end-to-end including promotion.*

1. All inbox CRUD endpoints
2. Auto-populate `exchange_rate` on create/update when `account_id` and `date` are both present
3. `POST /inbox/{id}/promote` — the most important endpoint in Phase 1:
   - Validates all required fields are present
   - Validates `date ≤ now()`
   - Creates `expense_transactions` row with `inbox_id` back-reference
   - Sets `status = 2` (promoted) on the inbox row
   - Soft-deletes the inbox row (`deleted_at = now()`)
   - Updates `current_balance_cents` on the account
   - Writes two `activity_log` entries (transaction created, inbox item deleted)
   - All of the above in a single database transaction — atomic

**Verify:** Create an incomplete inbox item, try to promote it (expect 422). Fill in all fields. Promote successfully. Confirm the inbox item is soft-deleted and the ledger transaction exists.

**Commit:** `feat: inbox CRUD + promote endpoint — atomic inbox-to-ledger flow`

---

## Step 6 — Transactions (Ledger)

*Deliverable: direct ledger creation, full editing with all business logic, balance updates.*

1. `GET /transactions` with all filters (`account_id`, `category_id`, `hashtag_id`, `date_from`, `date_to`, `cleared`, `approved`, `search`)
2. `POST /transactions` — direct to ledger (all required fields must be present)
3. `GET /transactions/{id}`
4. `PUT /transactions/{id}`:
   - Field locking when reconciliation is completed (reject `amount_cents`, `account_id`, `title`, `date` changes with 422)
   - Date change: re-fetch historical exchange rate, recalculate `amount_home_cents`
   - Balance update when `amount_cents` or `account_id` changes
5. `DELETE /transactions/{id}` — soft-delete, balance update, handle transfer sibling
6. `POST /transactions/batch` — atomic batch create

**Verify:** Create a transaction directly, edit its date (confirm `amount_home_cents` recalculates), delete it (confirm balance updates), try to edit a field that should be locked.

**Commit:** `feat: transactions CRUD — direct ledger entry, field locking, balance updates, batch create`

---

## Step 7 — Transfers

*Deliverable: paired transfer creation with zero-sum validation and auto-category assignment.*

1. Extend `POST /transactions` and `POST /inbox` to accept an optional `transfer` object
2. When `transfer` is present:
   - Create both transaction rows atomically
   - Link via `transfer_transaction_id` (each points to the other)
   - Auto-assign `@Transfer` to both real accounts, `@Debt` to any person account side
   - Auto-create `@Debt` or `@Transfer` system categories if they don't exist yet
   - Validate that the two transactions are directionally opposite (one negative, one positive)
   - Update `current_balance_cents` on both accounts
   - **Do not auto-create person accounts.** If `transfer.account_id` references a non-existent or archived account, return `422`. Person accounts are created explicitly via the People API (Phase 4).
3. Deletion of a transfer transaction deletes both rows atomically

**Verify:** Create a real-to-real transfer (both sides get @Transfer). Test real-to-person transfer behaviour end-to-end once Phase 4 ships the People API — until then, this path is exercisable only by seeding a person account directly in the DB (dev/test only). Try to create a transfer where both sides are the same sign (expect 422). Try to create a transfer to a non-existent `account_id` (expect 422 — no auto-creation).

**Commit:** `feat: transfer creation — paired transactions, zero-sum validation, auto-category`

---

## Phase 1 Complete ✓

At this point you have a fully working headless expense logger. Verify the entire Phase 1 surface via Swagger end-to-end before moving on.

**Deploy to production:**
1. Create a Render account (render.com) ✅
2. Deploy the engine to Render. Set env variables in the hosting dashboard. ✅
3. Verify `GET /health` returns 200 in production. ✅

**Production URL:** `https://expense-world-engine.onrender.com`

---

## Step 8 — Reconciliations (Phase 3)

All reconciliation endpoints. Complete/revert logic. Field locking enforcement in the transaction update endpoint.

**Commit:** `feat: reconciliations — CRUD, complete, revert, transaction field locking`

---

## Step 9 — Sync + Dashboard + Exchange Rates (Phase 2)

Split into **Part A** (read-side endpoints + exchange rates) and **Part B** (sync), with sync pulled out so it can be designed and executed in isolation.

### Step 9 Part A — Activity, Exchange Rates, Dashboard, Reports ✅ Shipped

1. **`GET /activity`** — paginated audit-log reads with `resource_type` and `resource_id` filters, sorted by `created_at DESC`. *(commit `d57b7f7`)*
2. **Exchange rate daily fetch job + `GET /exchange-rates`** — stdlib-only Python script (`app/jobs/fetch_exchange_rates.py`) that calls Frankfurter (`https://api.frankfurter.app/latest?from=USD&to=<targets>`) and upserts canonical USD-based rows into `exchange_rates`. The read endpoint uses the shared `get_rate` helper which handles directional math (inversion, cross-rates) at lookup time. Wiring the script to a daily Render Cron Job is operational and tracked in [TODO.md](../TODO.md). Historical backfill is also a user-owned task in [TODO.md](../TODO.md), scheduled for the very end of engine work. *(commit `d57b7f7`)*
3. **`GET /dashboard`** — current calendar month summary. Single call, everything needed for the main view. Response includes:
   - **`bank_accounts`** — all real accounts (`is_person = false`, not archived) with `current_balance_cents` + `current_balance_home_cents` (home converted at today's rate via `get_rate`).
   - **`people`** — all person accounts (`is_person = true`) with balances in both currencies. Same shape as `bank_accounts`, separated for client convenience.
   - **`categories`** — every non-deleted category with `spent_cents` (signed) and `spent_home_cents` (signed) for the current month. Also returns `hashtag_breakdown`: an array of `{ hashtag_combination: [hashtag_id, ...], spent_cents, spent_home_cents }` rows that sum cleanly to the parent category total. The combination is the *exact set* of hashtags on a transaction — `[#lunch, #work]` and `[#lunch]` are different rows. Transactions with no hashtags appear as a row with `hashtag_combination: []`.
   - **`totals`** — current month `inflow_cents`, `outflow_cents`, `net_cents` (all signed) in both currencies.
   - **Signed-flow semantics:** every transaction row contributes a signed amount derived from `transaction_type` + `transfer_direction`. Expenses and transfer debits are negative (outflow); income and transfer credits are positive (inflow). Categories sum signed amounts, so a real-to-real transfer naturally cancels to zero under `@Transfer`. `spent_cents` can be negative for income-dominant categories or for lending-out months on `@Transfer`/`@Debt`. *(commit `0ce92d4`)*
4. **`GET /reports/monthly`** — historical month data. Shares the exact same aggregation helper as `/dashboard` (`app/helpers/monthly_report.py`), so byte-identical shapes by construction. Query params:
   - `?year=&month=` — single month. Response is a bare object.
   - `?from_year=&from_month=&to_year=&to_month=` — multi-month range (inclusive, capped at 24 months). Response wraps per-month payloads in a `months` array, oldest first.
   - Mutually exclusive; partial/mixed/inverted/oversized inputs return `422` with the standard error shape. *(commit `a21d8c4`)*
5. **Cross-currency transfer zero-sum fix** *(discovered during Part A implementation, not originally in the plan)*. Before: `app/helpers/transfers.py` called `lookup_exchange_rate` independently for each leg of a cross-currency transfer, using the ECB market rate. For transfers where the user's actual execution rate differed from the market rate, the two legs' `amount_home_cents` values diverged and phantom home-currency balances leaked into dashboard totals on every cross-currency transfer. After: the dominant-side rule forces the non-dominant side's home value by direct assignment, guaranteeing zero-sum by construction. Documented in [api-design-principles.md §12](api-design-principles.md) and [schema-reference.md "Cross-currency transfers"](schema-reference.md). *(commit `f5f417c`)*

**Hashtag-combination grouping rule:** Aggregation is `GROUP BY (category_id, sorted_array_of_hashtag_ids)`. The hashtag set is sorted by `id` before grouping so `[#a, #b]` and `[#b, #a]` are the same group. The sum of all `hashtag_breakdown` rows under a category equals the category's `spent_cents` exactly — enforced by construction (the category total is computed from the breakdown rows, not a separate query).

**Verify Part A:**
- Trigger `python -m app.jobs.fetch_exchange_rates` manually, confirm a row appears in `exchange_rates`. Call `GET /v1/exchange-rates?base=USD&target=PEN`.
- Call `GET /v1/dashboard`. Confirm `bank_accounts`, `people`, `categories` (with `hashtag_breakdown`), and `totals` are all populated. Sum of `hashtag_breakdown` rows equals each category's `spent_cents`.
- Call `GET /v1/reports/monthly?from_year=2025&from_month=11&to_year=2026&to_month=4` and confirm 6 months returned in order.
- Create a cross-currency transfer (3750 PEN → 1000 USD with `main_currency = PEN`). Call `GET /v1/dashboard`. Confirm both legs have `amount_home_cents = 375000` (PEN cents) and that `totals.net_home_cents` is unchanged by the transfer.

### Step 9 Part B — Sync 🔨 In Progress

Design validated against Todoist Sync API v9, YNAB delta requests, Contentful CDA, Lunch Money, TickTick, and Things Cloud. See `docs/api-design-principles.md §3` for the full sync model and `docs/engine-spec.md §Sync` for the wire contract.

6. **`GET /v1/sync`** — delta sync with opaque-UUID `sync_token` and per-client checkpoints via `X-Client-Id` header. Wildcard `*` does full fetch; deltas use `WHERE updated_at > last_sync_at` against every synced table. All reads + the checkpoint write happen inside one Postgres `REPEATABLE READ` transaction for snapshot isolation. Response carries 8 top-level keys (`sync_token`, `accounts`, `categories`, `hashtags`, `inbox`, `transactions`, `reconciliations`, `settings`). Transactions embed `hashtag_ids: [uuid, ...]`; junction table stays internal. Soft-deleted rows flow as tombstones with `deleted_at` set. Schema migration `sql/009_user_settings_sync.sql` adds `version` + `deleted_at` to `user_settings` (closing a documented schema convention gap) and `(user_id, updated_at)` indexes to every synced table for query performance at 1000+ users. Cross-cutting: `DELETE /hashtags/{id}` now bumps `version` + `updated_at` on every transaction whose junction rows it soft-deletes (parent-bump rule, see [api-design-principles.md §3](api-design-principles.md)).

**Verify Part B:**
- `GET /v1/sync` with `sync_token=*` returns all active records plus a new token.
- Mutate a transaction, re-sync with the returned token, confirm only the mutated row comes back.
- Soft-delete a transaction, re-sync, confirm it appears as a tombstone (`deleted_at` set).

---

## Step 9.1 — Home Currency Recalculation ✅ Shipped

*Deliverable: changing `main_currency` in settings recalculates all home-currency amounts, idempotently and in batches, via a first-class job.*

**Implementation:** `app/helpers/recalculate_home_currency.py`, wired into `PUT /auth/settings` in `app/routers/auth.py`. Three passes: (1) regular transactions — `get_rate` lookup + recompute, (2) transfer pairs — dominant-side rule reapplication for zero-sum, (3) pending inbox items — `exchange_rate` refresh. Synchronous inside the settings request (Phase 1). Every updated row bumps `version + updated_at` for sync. Single `activity_log` entry includes recalc summary. 6 integration tests in `tests/test_home_currency_recalc.py`. *(commit `003c204`)*

Depends on: Step 6 (transactions exist), Step 9 Part A (historical exchange rates available, background job infrastructure in place).

### Why this is a real feature, not a setting toggle

Every production multi-currency system we looked at (QuickBooks Online, Xero, Firefly III, Lunch Money) treats the home/base currency as **effectively immutable** post-setup. QBO and Xero refuse to change it in place at all; Firefly III allows it but only via a `correction:recalculate-pc-amounts` command that "may take some time if you have a lot of transactions." The reason is that `amount_home_cents` is cached on every transaction at write time (per IAS 21.21 — spot rate at transaction date, immutable), so changing the home currency requires rewriting every row. This is a background job, not a setting toggle, and the product UX should signal that.

### Behavior

When `PUT /auth/settings` detects that `main_currency` has actually changed (old value != new value), trigger a recalculation modelled after Firefly III's `correction:recalculate-pc-amounts`:

1. **Idempotent, batched, restartable.** The job can be re-run safely; a partial run can be resumed. Implemented as an async background task (or a synchronous operation for small data volumes — see "execution model" below).
2. **`amount_home_cents` on all non-deleted `expense_transactions`** — per-row lookup of the historical rate for that transaction's `date` via the shared `get_rate` helper, honouring any `exchange_rate` override already stored on the row (user overrides must not be clobbered). If `account.currency_code == new main_currency`, set `amount_home_cents = amount_cents` directly. Cross-currency transfer pairs stay zero-sum by re-applying the dominant-side rule (see [api-design-principles.md §12](api-design-principles.md)).
3. **`current_balance_home_cents` on all non-deleted `expense_bank_accounts`** — recomputed at today's rate against the new home currency.
4. **`exchange_rate` on pending inbox items** (`status = 1`) — recomputed to reflect the new home currency so future promotions compute correctly.
5. **Single `activity_log` entry** — `resource_type = 'user_settings'`, `action = 2` (updated), recording `main_currency` changed from X to Y plus the job's summary (rows touched, duration, outcome). Individual transaction updates are **not** logged — bulk recalculation is a single audit event, not thousands.
6. **Never retroactively mutate rates or re-derive them from current `exchange_rates`** unless the `exchange_rate` on the row is null or the transaction's date was explicitly edited. User manual overrides survive.

### Execution model

**Phase 1 reality:** transaction volume is low (single user, hundreds to low thousands of rows). A synchronous recalculation inside the `PUT /auth/settings` request is acceptable as long as it fits within the Render request timeout. The response returns only after recalculation completes; the client sees normal synchronous semantics.

**Phase 2 / multi-tenant:** when customer transaction counts grow, migrate to an async job model. The `PUT /auth/settings` request enqueues the job, the response includes a `recalculation_job_id`, and a new `GET /auth/recalculation_jobs/{id}` endpoint lets the client poll for completion. Do NOT introduce this complexity until it's needed — ship the synchronous version first.

### Verify

- Set `main_currency = USD`. Create a few transactions on a PEN account. Switch `main_currency = PEN`. Confirm all `amount_home_cents` values are recomputed. Confirm `current_balance_home_cents` on accounts is recomputed at today's rate. Confirm pending inbox items' `exchange_rate` is updated. Confirm exactly one `activity_log` entry was written.
- Re-run the job (trigger another `PUT /auth/settings` that toggles back). Confirm it completes cleanly and produces identical results on repeated runs (idempotence).
- Create a cross-currency transfer in PEN main. Switch to USD main. Confirm the transfer still nets to zero in the new home currency.

**Commit:** `feat: home currency recalculation on main_currency change`

---

## Step 9.5 — Web Dashboard (Read-Only)

*Deliverable: a lightweight Next.js dashboard on Vercel that reads from the engine and shows you if you're on track.*

**Repo:** `expense_world_web` → deployed to Vercel free tier.

**Built after:** Step 6 (transactions endpoint) is working. You don't need reconciliations or transfers to visualise basic spending.

**Stack:**
```
expense_world_engine   → Render (Python FastAPI, always-on)
expense_world_db       → Supabase (Postgres)
expense_world_web      → Vercel (Next.js, read-only client)
expense_world_cli      → local machine (Python Typer)
expense_world_ios      → later, maybe never needed
```

**Engine calls:**
- `GET /dashboard` → bank accounts, people, categories (with hashtag breakdown), current month totals
- `GET /reports/monthly?from_year=...&to_year=...` → last 6 months for the trend table
- `GET /transactions` with filters (`account_id`, `category_id`, `hashtag_id`, date range, search) → the transactions browser

**Must-have views:**
1. **Bank accounts panel** — every real account with its current outstanding balance (native + home currency).
2. **People panel** — every person account with its outstanding balance. Same shape as bank accounts; visually separated.
3. **Categories — current month** — flat list of categories with this month's total spend.
4. **Categories → hashtag breakdown — current month** — each category expandable to show its `hashtag_breakdown` rows. Hashtag-combination rows sum cleanly to the parent category total.
5. **6-month trend table — categories** — rows are categories, columns are the last 6 months, cells are `spent_home_cents`. Powered by a single multi-month `/reports/monthly` call.
6. **6-month trend table — categories + hashtag combinations** — same shape as #5 but rows include the hashtag-combination breakdown beneath each category.
7. **Transaction browser** — paginated list of all transactions with filters for hashtag, category, and bank account (and date range / search as bonus).

**What it does not do:** no entry, no editing, no forms. Read-only. Anything beyond the seven views above waits for a later iteration.

**Commit:** `feat: read-only dashboard — balances, category totals, recent transactions`

---

## Step 10 — Engine Complete → Start CLI

**Engine is feature-complete.** All endpoints (Steps 0–9.1) are implemented, documented, and tested (26 sync + 6 recalc integration tests). Two operational tasks remain before full production readiness — see [TODO.md](../TODO.md): (1) wire up the Render Cron Job for daily exchange-rate fetching, (2) backfill historical exchange rates.

Next: write the `expense_world_cli` spec (fill in `cli-spec.md`) and start building CLI commands against the live engine.

---

## Later Phases (Engine)

| Phase | Scope |
|---|---|
| Phase 4 | People & person accounts — dedicated People API (`POST /people`, etc.) + CLI surface. **Person accounts are created only via this API, never auto-created by the transfer engine.** |
| Phase 5 | Batch CSV import, `import_id` deduplication, recurrence templates |
| Budgets | `expense_budgets` table, budget endpoints — deferred |
| Sharing | `transaction_shares`, cross-user flows — deferred |

---

## Web Dashboard — Expand Later

Once the CLI is stable and you've used the system for a while, the web dashboard can be expanded incrementally — add entry, add editing, add more views. By that point you'll know exactly what you actually want. Spec in `ios-spec.md` (serves as design reference for both web and iOS).

**Before any client UI ships:** Configure Supabase Auth providers (Apple sign-in, Google sign-in) in the Supabase dashboard. Not needed during engine development — only when real users log in via a UI.

## iOS (Maybe)

Begins after the web dashboard proves insufficient for mobile use. If the Next.js PWA on Vercel is good enough pinned to your home screen, iOS may never be needed. Spec in `ios-spec.md`.

---

*Last updated: April 2026*
