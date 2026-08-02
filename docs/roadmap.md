# Expense Tracker — Build Roadmap

> Build order: Engine → CLI → Web Dashboard → iOS. Nothing exists for any client until it is defined and working in the engine first.
> Full specs: `engine-spec.md` (this repo) · `cli-spec.md` (sibling repo, `expense_world_CLI/docs/`) · `ios-spec.md` (not yet written — the `expense_world_ios` repo does not exist as of 2026-07-30)

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
- Install FastAPI, Uvicorn, python-jose (JWT), and a Postgres driver as starters. *(Resolved: **asyncpg**, used directly with hand-written SQL — no ORM. SQLAlchemy was listed as an option here and pinned in `requirements.txt` for a while but was never imported; it was dropped 2026-07-30. Test deps live in `requirements-dev.txt`.)*

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

**Production URL (at this step's completion):** `https://expense-world-engine.onrender.com` — *mothballed 2026-07-30 by Step 11; the live engine is `http://127.0.0.1:8000` (deploy/local).*

---

## Step 8 — Reconciliations (Phase 3)

All reconciliation endpoints. Complete/revert logic. Field locking enforcement in the transaction update endpoint.

**Commit:** `feat: reconciliations — CRUD, complete, revert, transaction field locking`

---

## Step 9 — Sync + Dashboard + Exchange Rates (Phase 2)

Split into **Part A** (read-side endpoints + exchange rates) and **Part B** (sync), with sync pulled out so it can be designed and executed in isolation.

### Step 9 Part A — Activity, Exchange Rates, Dashboard, Reports ✅ Shipped

1. **`GET /activity`** — paginated audit-log reads with `resource_type` and `resource_id` filters, sorted by `created_at DESC`. *(commit `d57b7f7`)*
2. **Exchange rate daily fetch job + `GET /exchange-rates`** — stdlib-only Python script (`app/jobs/fetch_exchange_rates.py`) that fetches all USD rates and upserts canonical USD-based rows into `exchange_rates`. *(Provider was Frankfurter at ship time; swapped to fawazahmed0/currency-api 2026-07-30 — ECB data carries no PEN. See the job docstring + TODO.md.)* The read endpoint uses the shared `get_rate` helper which handles directional math (USD-involving conversions and inversion) at lookup time. Cross-rate (non-USD ↔ non-USD) lookups are intentionally unsupported under the Phase 1 PEN/USD-only policy (`sql/015`); `get_pair_rate` returns `None` for that case. Scheduling is operational, not code: the job runs as a daily launchd agent under the local profile ([deploy/local/README.md](../deploy/local/README.md)); the Render cron recipe for cloud reactivation lives in [deploy/cloud/README.md](../deploy/cloud/README.md) step 5. Historical backfill shipped 2026-07-31 as `app/jobs/backfill_exchange_rates.py` (see [TODO.md](../TODO.md)). *(commit `d57b7f7`)*
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

### Step 9 Part B — Sync ✅ Shipped

Design validated against Todoist Sync API v9, YNAB delta requests, Contentful CDA, Lunch Money, TickTick, and Things Cloud. See `docs/api-design-principles.md §3` for the full sync model and `docs/engine-spec.md §Sync` for the wire contract.

6. **`GET /v1/sync`** — delta sync with opaque-UUID `sync_token` and per-client checkpoints via `X-Client-Id` header. Wildcard `*` does full fetch; deltas use `WHERE updated_at > last_sync_at` against every synced table. All reads + the checkpoint write happen inside one Postgres `REPEATABLE READ` transaction for snapshot isolation. Response carries 8 top-level keys (`sync_token`, `accounts`, `categories`, `hashtags`, `inbox`, `transactions`, `reconciliations`, `settings`). Transactions embed `hashtag_ids: [uuid, ...]`; junction table stays internal. Soft-deleted rows flow as tombstones with `deleted_at` set. Schema migration `sql/009_user_settings_sync.sql` adds `version` + `deleted_at` to `user_settings` (closing a documented schema convention gap) and `(user_id, updated_at)` indexes to every synced table for query performance at 1000+ users. Cross-cutting: `DELETE /hashtags/{id}` now bumps `version` + `updated_at` on every transaction whose junction rows it soft-deletes (parent-bump rule, see [api-design-principles.md §3](api-design-principles.md)).

**Verify Part B:**
- `GET /v1/sync` with `sync_token=*` returns all active records plus a new token.
- Mutate a transaction, re-sync with the returned token, confirm only the mutated row comes back.
- Soft-delete a transaction, re-sync, confirm it appears as a tombstone (`deleted_at` set).

---

## Step 9.1 — Home Currency Recalculation ✅ Shipped → ⛔ RETIRED 2026-08-01

> **Retired 2026-08-01.** The home currency is now locked to PEN (`sql/018`) and `PUT /auth/settings` rejects `main_currency` with `422`. `app/helpers/recalculate_home_currency.py` and `tests/test_home_currency_recalc.py` were **deleted** — the helper contained a silent `1.0` rate fallback (audit finding WP1.1) that wrote wrong `amount_home_cents` for any transaction dated before the FX backfill floor. Everything below is kept as the dated record of what shipped and why; it no longer describes the engine. See [audit-2026-08-01-remediation-plan.md](audit-2026-08-01-remediation-plan.md) WP1.1 and the `sql/018` header for the restoration path.

*Deliverable (historical): changing `main_currency` in settings recalculates all home-currency amounts, idempotently and in batches, via a first-class job.*

**Implementation:** `app/helpers/recalculate_home_currency.py`, wired into `PUT /auth/settings` in `app/routers/auth.py`. Three passes: (1) regular transactions — `get_rate` lookup + recompute, (2) transfer pairs — dominant-side rule reapplication for zero-sum, (3) pending inbox items — `exchange_rate` refresh. Synchronous inside the settings request (Phase 1). Every updated row bumps `version + updated_at` for sync. Single `activity_log` entry includes recalc summary. 9 integration tests in `tests/test_home_currency_recalc.py`. *(commit `003c204`)*

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

## Step 9.2 — Personal Access Tokens (CLI auth) ✅ Shipped

*Deliverable: long-lived engine-issued tokens so clients that can't do interactive JWT refresh (CLI, scripts, cron) can authenticate.*

**Implementation:** `sql/016_personal_access_tokens.sql` adds the `personal_access_tokens` table (SHA-256 hash, cleartext `token_prefix` for display, nullable `name`, `revoked_at` soft-delete). `app/helpers/auth_token.py` owns the `ewe_pat_` prefix and hashing; `app/helpers/pat.py` exposes `create` / `revoke`; routes in `app/routers/pat.py` (`POST /auth/pat`, `DELETE /auth/pat/{pat_id}`), schemas in `app/schemas/pat.py`. Token resolution is unified in `app/deps.py` — a bearer token starting with `ewe_pat_` is looked up by hash, anything else is parsed as a Supabase JWT, and both resolve to the same `AuthUser`, so no downstream code knows which was used. Plaintext is returned exactly once at mint time. 11 integration tests in `tests/test_pat.py`. *(commit `3f729b2`)*

Depends on: Step 3 (auth middleware). Unblocks: the CLI, which authenticates exclusively via PAT — and under the local profile (Step 11) this becomes the *only* auth path, since no JWT provider runs locally.

**Deliberately not shipped:** `GET /auth/pat` (list) — deferred until a web dashboard needs a management UI. `last_used_at` tracking — dropped to avoid a per-request DB write on every authenticated call. Both recorded in `engine-spec.md §Auth`.

---

## Step 9.3 — Profile Mutation ✅ Shipped

*Deliverable: `PUT /auth/profile` — the single post-bootstrap path for changing identity fields on the `users` row.*

**Implementation:** `ProfileUpdateRequest` in `app/schemas/auth.py`, route in `app/routers/auth.py`, `update_profile` in `app/helpers/auth.py`. No SQL migration — it writes existing `users` columns.

**Why it exists:** `POST /auth/bootstrap` sets `display_name` only on the first call and never overwrites it on later logins, and `PUT /auth/settings` mutates `user_settings`, not `users` — so before this step there was no way to rename yourself after bootstrap. Mutable in v1: `display_name` only. `id`, `email`, and `last_login_at` are returned for context but read-only (`email` lives in Supabase Auth; `last_login_at` is owned by the bootstrap flow and is explicitly preserved so this endpoint can't masquerade as a login event).

Unlike `PUT /auth/settings` (8 fields, empty-body-as-fetch), an empty body here is a client bug and fails fast with `422`; explicit `"display_name": null` is also `422` — clearing the name isn't in v1 scope. Forward-compatible: future identity fields (e.g. `avatar_url`) drop into the request schema without touching the route or helper. 7 integration tests in `tests/test_auth_profile.py`. *(commit `7017615`)*

Depends on: Step 3 (bootstrap + `users` row).

---

## Step 9.4 — Opening Balances ✅ Shipped

*Deliverable: `POST /accounts/{id}/opening-balance` seeds an account's starting balance as a transaction under the `@Opening` system category; flow reports exclude it entirely.*

**Implementation:** `SystemCategoryKey.OPENING_BALANCE` + `@Opening` default name in `app/constants.py`; `OpeningBalanceRequest` in `app/schemas/accounts.py`; route in `app/routers/accounts.py`; `create_opening_balance` in `app/helpers/accounts.py` (validates real/active account, enforces one active opening per account with `409`, then delegates to `create_transaction` so validation, rate lookup, balance update, and activity log stay in one place); report exclusion in `app/helpers/monthly_report.py` (category panel + totals, keyed on `system_key = 'opening_balance'`). No SQL migration — the `system_key` column and partial unique index from `sql/010` accommodate the third key. 8 integration tests in `tests/test_opening_balance.py`.

Depends on: Step 4 (accounts, categories, `ensure_system_category`), Step 6 (transactions), Step 9 Part A (reports).

### Why

`POST /accounts` has no balance field — every account starts at 0 and `current_balance_cents` is transaction-derived. Before this step, the only way to seed a starting balance was an ordinary income transaction under a user-invented category, which permanently inflated reported income with no way for the engine or any client to recognize it. Account initialisation with an opening balance was contemplated in the original design (`lessons-todoist.md` §7) but never shipped. The endpoint works for **existing** accounts (not just at creation) because real users adopt the product with live accounts; a creation-time `opening_balance_cents` param on `POST /accounts` remains a possible future convenience for onboarding flows.

### Behavior

- Seed = ordinary transaction (editable/deletable; balance atomically applied; appears in transaction lists and `/sync`), auto-assigned to `@Opening` (`system_key = 'opening_balance'`, auto-created on first use, renameable).
- Flow reports (dashboard month panel + `/reports/monthly`): the `@Opening` row is hidden and its transactions contribute nothing to `inflow/outflow/net` — visible category rows sum exactly to `net_cents`. Account balances include seeds by construction.
- Guards: one active opening balance per account (`409`); client-supplied `transaction_id` (`409` on collision) makes bulk-import re-runs converge; zero amount / future date / person account / archived account → `422`.

### Verify

- Seed a fresh account → `201`, balance updated, `@Opening` exists with `is_system = true`. Second seed on the same account → `409`. Replay with same idempotency key → stored response, balance applied once.
- Monthly report for the seed month: no `@Opening` category row; totals unchanged by the seed. Rename `@Opening` → exclusion and guards still hold.

**Commit:** `feat: opening balances — @Opening system category + report exclusion`

---

> **Step 9.5 — Web Dashboard (Read-Only): removed as a numbered engine step, 2026-08-01.** It was never engine work — the dashboard is a separate repo (`expense_world_web`) consuming endpoints that already shipped in Step 9 Part A (`/dashboard`, `/reports/monthly`, `/transactions`). The seven must-have views it specified are in git history; the web dashboard as a *product* phase survives under "Web Dashboard — Expand Later" below.

## Step 10 — Engine Complete → Start CLI

**Engine is feature-complete.** All endpoints (Steps 0–9.4) are implemented, documented, and tested — 171 integration tests across 20 files, run in parallel via pytest-xdist (`pip install -r requirements-dev.txt`). Full suite runs in ~1s against local Postgres; it took ~4 min against the Supabase pooler before Step 11.

No operational tasks remain outstanding: the daily exchange-rate fetch ships as a launchd task under Step 11, and the historical backfill closed 2026-07-31 (Step 11.5). What is left in [TODO.md](../TODO.md) is a trigger-gated performance item, not a gap.

Next: write the `expense_world_cli` spec (fill in `cli-spec.md`) and start building CLI commands against the live engine.

---

## Step 11 — Local Deployment (decided and executed 2026-07-30)

*Deliverable: the entire system running on the owner's Mac — engine + Postgres local, daily FX fetch working, nightly backups proven — with the cloud mothballed until a second client exists.*

**Why:** single-user phase; Render free-tier cold starts made daily use slow; the FX cron was never wired on Render (paid-only) which blocked cross-currency PEN/USD writes. Full rationale + rejected alternatives: `expense_world_CLI/docs/decisions.md` → "Local-first deployment (2026-07-30)". **This is a deployment change, not an architecture change** — no edits to `app/` or `sql/`; §3b and the one-engine invariant hold verbatim.

**Ops home:** [deploy/local/README.md](../deploy/local/README.md) (procedures, launchd templates, backup/restore) · [deploy/cloud/README.md](../deploy/cloud/README.md) (mothball + iOS-day reactivation).

1. **11.1 — Postgres:** Homebrew install, service running, engine database + role created.
2. **11.2 — Auth stand-in:** minimal `auth` schema (`auth.users` with the owner's existing `user_id`, `auth.uid()` stub) so `sql/005`/`sql/006` apply cleanly; PAT auth unchanged. Final SQL committed to `deploy/local/`.
3. **11.3 — Schema + data:** run `sql/001`→`017`; `pg_dump` Supabase (direct connection) → restore local; row counts verified against the cloud ledger.
4. **11.4 — Engine as a service:** `.env` per `app/config.py` (direct-connection pool sizing), uvicorn under launchd, survives reboot.
5. **11.5 — FX:** daily launchd fetch task verified (`GET /v1/exchange-rates?target=PEN&base=USD` returns today), then the historical backfill + home-currency recalc so PEN/USD history is accurate. ✅ **Closed 2026-07-31:** backfill shipped as `app/jobs/backfill_exchange_rates.py` and run for 2024-03-02 → today (881 daily USD→PEN rows; provider has no data before 2024-03-02). No recalc needed — the ledger was wiped clean the same day, so there was nothing to convert.
6. **11.6 — Backups:** nightly `pg_dump` → **Google Drive** (via Drive for Desktop's local mount), 30-copy rotation, one restore drill passed before the step closes. *(iCloud Drive was the original target and is what earlier drafts of this line said. It fails **silently** under a launchd agent — new files can be created, but directory listing returns empty and existing files can't be modified, so rotation silently kept everything and the log append failed. Google Drive's mount has no such restriction and needs no Full Disk Access grant.)*
7. **11.7 — Cutover:** CLI verification gate against an isolated config (ping → full sync → contract suite at `http://127.0.0.1:8000`), then repoint the real `~/.expense-config`. Mothball the cloud per `deploy/cloud/README.md`.

8. **11.8 — Operational hardening (2026-07-31).** Four items found after the step was first called done:
   - **Login race, both periodic agents.** `RunAtLoad` starts them in parallel with Homebrew's postgres service, so either could reach a socket that did not exist yet. `backup.sh` aborted under `set -e` (no dump that day); `fetch_exchange_rates` raised an unhandled `ConnectionRefusedError` (exit 1, no rate until the next 6-hourly fire, every cross-currency write 422ing until then). Both now wait up to 60s. Details: [deploy/local/README.md](../deploy/local/README.md) "The login race".
   - **Test database separated.** The suite ran against the ledger database; `exchange_rates` has no `user_id`, so its seed row could not be cleaned up and would win the day against the real fetch. Now `expense_world_test`, with a fail-closed allowlist guard in `tests/conftest.py`. Setup: [deploy/local/create-test-db.sh](../deploy/local/create-test-db.sh).
   - **Ledger wiped clean** to start fresh, keeping identity, PATs and `global_currencies`. Everything prior was contract-test residue.
   - **Health check** — [deploy/local/healthcheck.sh](../deploy/local/healthcheck.sh) covers engine, database, agent exit codes, backup freshness and today's FX rate.

**Verify (step gate):** a full day of real use — capture, dashboard, monthly report — with every command instant, plus one nightly backup and one FX fetch confirmed in the logs. **Not yet met** — the ledger holds no real data. This is what actually closes Step 11.

**Docs to flip when this ships — ✅ done for the engine repo (2026-07-30):** engine CLAUDE.md hosting lines · `docs/api-design-principles.md` Database/Engine/Auth paragraphs · `docs/engine-spec.md` base URL. All three are flipped; do not re-action.

*CLI repo — verify separately if not already done:* `cli-runtime.md` "Working against the live engine", `cli-spec.md` `engine_url` example, CLAUDE.md "hits the production engine" wording, and the `roadmap.md` header line naming the onrender URL. (Historical mentions — dated status lines, `polish-backlog.md`'s cold-start rationale — stay per the absolute-dates rule.)

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

Once the CLI is stable and you've used the system for a while, the web dashboard can be expanded incrementally — add entry, add editing, add more views. By that point you'll know exactly what you actually want. Design reference will live in `ios-spec.md` (unwritten — it is intended to serve both web and iOS; `docs/design-philosophy.md` is the only UX reference that exists today).

**Before any client UI ships:** Configure Supabase Auth providers (Apple sign-in, Google sign-in) in the Supabase dashboard. Not needed during engine development — only when real users log in via a UI.

## iOS (Maybe)

Begins after the web dashboard proves insufficient for mobile use. If the Next.js PWA on Vercel is good enough pinned to your home screen, iOS may never be needed. Spec in `ios-spec.md` — to be written if and when this phase begins.

---

*Last updated: 2026-08-01 (removed Step 9.5 Web Dashboard as a numbered engine step; Render FX cron recipe moved to `deploy/cloud/README.md`)*
