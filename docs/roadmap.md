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
3. Deletion of a transfer transaction deletes both rows atomically

**Verify:** Create a real-to-real transfer (both sides get @Transfer). Create a real-to-person transfer (person side gets @Debt). Try to create a transfer where both sides are the same sign (expect 422).

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

1. `GET /sync` — delta sync with sync token pattern. Reads `sync_checkpoints`, returns records with `version` higher than checkpoint, includes tombstones.
2. **`GET /dashboard`** — current calendar month summary. Single call, everything needed for the main view. Response includes:
   - **`bank_accounts`** — all real accounts (`is_person = false`) with `current_balance_cents` + `current_balance_home_cents`
   - **`people`** — all person accounts (`is_person = true`) with outstanding balances in both currencies. Same shape as `bank_accounts`, separated for client convenience.
   - **`categories`** — every category with `spent_cents` (in `main_currency`, since multiple accounts may contribute) and `spent_home_cents` for the current month. Also returns `hashtag_breakdown`: an array of `{ hashtag_combination: [hashtag_id, ...], spent_cents, spent_home_cents }` rows that sum cleanly to the parent category total. The combination is the *exact set* of hashtags on a transaction — `[#lunch, #work]` and `[#lunch]` are different rows. Transactions with no hashtags appear as a row with `hashtag_combination: []`.
   - **`totals`** — current month inflow, outflow, and net, in both currencies.
3. **`GET /reports/monthly`** — historical month data. Same response shape as `/dashboard`. Query params:
   - `?year=&month=` — single month (existing behavior)
   - `?from_year=&from_month=&to_year=&to_month=` — multi-month range. Response wraps the per-month payloads in a `months` array, oldest first. Used by the 6-month trend table.
4. `GET /activity` — activity log reads
5. **Exchange rate daily fetch job** — implement the background job that calls Frankfurter.app (`https://api.frankfurter.app/latest?from=USD&to=PEN`) once per day and inserts a row into `exchange_rates`. Run on Render as a scheduled task. Seed historical rates for the past 12 months on first run (Frankfurter supports historical queries via `https://api.frankfurter.app/{date}?from=USD&to=PEN`). Wire up `GET /exchange-rates` endpoint.

**Hashtag-combination grouping rule:** Aggregation is `GROUP BY (category_id, sorted_array_of_hashtag_ids)`. The hashtag set is sorted by `id` before grouping so `[#a, #b]` and `[#b, #a]` are the same group. The sum of all `hashtag_breakdown` rows under a category equals the category's `spent_cents` exactly — no double-counting, no orphaned amounts.

**Verify:**
- Trigger the fetch job manually, confirm a row appears in `exchange_rates`. Call `GET /exchange-rates?base=USD&target=PEN`.
- Call `GET /dashboard`. Confirm `bank_accounts`, `people`, `categories` (with `hashtag_breakdown`), and `totals` are all populated. Sum of `hashtag_breakdown` rows equals each category's `spent_cents`.
- Call `GET /reports/monthly?from_year=2025&from_month=11&to_year=2026&to_month=4` and confirm 6 months returned in order.

**Commit:** `feat: sync endpoint, dashboard, monthly reports, activity log reads, exchange rate fetch job`

---

## Step 9.1 — Home Currency Recalculation

*Deliverable: changing `main_currency` in settings recalculates all home-currency amounts and the client knows when it's done.*

Depends on: Step 6 (transactions exist), Step 9 (historical exchange rates seeded, background job infrastructure in place).

When `PUT /auth/settings` detects that `main_currency` changed (compare old value to new), trigger a recalculation:

1. **`amount_home_cents` on all `expense_transactions`** — batch UPDATE joining `exchange_rates` for per-date rate lookup. If `account.currency_code == new main_currency`, set `amount_home_cents = amount_cents`. Otherwise, convert using the historical rate for that transaction's `date`.
2. **`current_balance_home_cents` on all `expense_bank_accounts`** — recompute using today's rate against the new home currency.
3. **`exchange_rate` on pending inbox items** (`status = 1`) — update to reflect conversion to the new home currency so future promotions compute correctly.
4. **Single `activity_log` entry** — resource_type `user_settings`, action `updated`, recording `main_currency` changed from X to Y. Individual transaction updates are not logged (bulk recalculation, not user edits).
5. **Completion tracking** — the response must include a mechanism for the client to know when recalculation is finished. Design decision needed: a `recalculation_jobs` table with polling, a simpler `recalculation_pending` flag on `user_settings`, or a synchronous approach if transaction volume is reliably low (two currencies, single user).

**Verify:** Set `main_currency` to USD. Create transactions on a PEN account. Switch `main_currency` to PEN. Confirm all `amount_home_cents` values are recalculated. Confirm `current_balance_home_cents` on accounts updated. Confirm pending inbox items have updated `exchange_rate`. Confirm a single activity log entry was written.

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

Phase 1 and 2 of the engine are done and deployed. Now write the `expense_world_cli` spec (fill in `cli-spec.md`) and start building CLI commands against the live engine.

---

## Later Phases (Engine)

| Phase | Scope |
|---|---|
| Phase 4 | People & person accounts UI via CLI |
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
