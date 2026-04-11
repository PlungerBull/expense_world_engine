# Expense Engine — API Spec

> The `expense_world_engine` is the Brain. This document defines every endpoint, every business logic rule, and every validation the engine enforces. Nothing exists for any client unless it is defined here first.
>
> Architecture: `api-design-principles.md` | Schema: `schema-reference.md`

---

## Base Conventions

**Base URL:** `https://expense-world-engine.onrender.com/v1` (production) / `http://localhost:8000/v1` (local)

**Authentication:** Every request requires `Authorization: Bearer <token>`. The engine validates the Supabase JWT, extracts `user_id`, and passes it to all downstream logic. Unauthenticated requests return `401`.

**Idempotency:** Write operations (`POST`, `PUT`, `DELETE`) should include `X-Idempotency-Key: <uuid>`. The engine deduplicates against `idempotency_keys` table. Duplicate requests return the stored response verbatim.

**Sign convention — requests:** `amount_cents` in request bodies uses a signed convention. The engine infers `transaction_type` from the sign — the caller never fills it in manually. Negative = expense/outflow (subtracts from balance). Positive = income/inflow (adds to balance). Transfers are identified by the presence of a `transfer` field in the request body, not by sign.

**Sign convention — storage:** Internally, `amount_cents` is always stored as a positive integer. `transaction_type` (1=expense, 2=income, 3=transfer) and `transfer_direction` (1=debit, 2=credit) are set by the engine based on the inferred direction. Callers never interact with these fields on writes.

**Sign convention — responses:** `amount_cents` in responses is always positive. `transaction_type` tells the client the direction. Pass `?debit_as_negative=true` on any read endpoint to receive negative amounts for expenses and outflows — useful for clients that prefer signed display.

**Null over omission:** All optional fields are always present in responses, set to `null` when empty. The response shape never changes based on data presence.

**Error format:**
```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Human-readable description.",
    "fields": { "amount_cents": "Must not be zero." }
  }
}
```

**Pagination:** List endpoints accept `?limit=50&offset=0`. Default limit: 50. Max limit: 200. Response includes `total`, `limit`, `offset`.

**Soft-deleted records:** Excluded from all list responses by default. Pass `?include_deleted=true` to include them.

**Optimistic locking:** All mutable resources include a `version` field in responses, incremented on every update. Clients can use this for conflict detection.

---

## Build Phases (Engine)

| Phase | Endpoints | Goal |
|---|---|---|
| 1 | Auth bootstrap, Accounts, Categories, Inbox, Transactions (ledger), Hashtags | Core tracking — fully working expense logger |
| 2 | Sync endpoints, Activity log reads, Dashboard + reporting | Sync-ready, reportable |
| 3 | Reconciliations | Bank statement matching |
| 4 | Transfers + People (`/` syntax, person accounts) | Debt tracking |
| 5 | Batch import, Recurrence | Power features |

Each phase is verified via Swagger UI before any CLI or iOS code is written.

---

## Health Check

### `GET /health`
Infrastructure endpoint. Returns `200` if the engine is running. No authentication required. Not versioned under `/v1`.

**Response:** `{"status": "ok"}`

---

## Auth & User Bootstrap

### `POST /auth/bootstrap`
Called by any client immediately after a successful Supabase sign-in, on every new device. Creates the `users` and `user_settings` rows if they don't exist (idempotent). Returns the full user profile.

**Request body:**
```json
{
  "display_name": "Alex",
  "timezone": "America/Lima"
}
```

**Response:** `user` object + `user_settings` object.

`user` fields: `id`, `email`, `display_name`, `last_login_at`, `created_at`, `updated_at`.

`user_settings` fields: `user_id`, `theme`, `start_of_week`, `main_currency`, `transaction_sort_preference`, `display_timezone`, `sidebar_show_bank_accounts`, `sidebar_show_people`, `sidebar_show_categories`, `created_at`, `updated_at`.

**Business logic:**
- If `users` row already exists for this `user_id`, skip creation but update `last_login_at` and `updated_at` to `now()`.
- If `user_settings` row already exists, skip creation.
- Always returns current state regardless of whether rows were created.

---

### `GET /auth/me`
Returns the authenticated user's profile and settings in a single response. Returns `404` if the user or settings row does not exist (edge case — should not occur after bootstrap).

### `PUT /auth/settings`
Updates `user_settings`. Partial update — only supplied fields are changed. If no fields are supplied, returns current settings without making changes.

**Special case — `main_currency` change:** If `main_currency` changes, the engine recalculates `amount_home_cents` on all the user's `expense_transactions` using historical rates for each transaction's date, updates `current_balance_home_cents` on all accounts, and updates `exchange_rate` on pending inbox items. A single `activity_log` entry records the currency change (individual transaction updates are not logged). The client receives a mechanism to know when recalculation is complete. *(Deferred to Step 9.1 — requires historical exchange rates and transaction endpoints to exist first. Until then, `main_currency` changes apply to new transactions only; existing `amount_home_cents` values are not retroactively recalculated.)*

---

## Bank Accounts

### `GET /accounts`
Returns all active bank accounts. Includes `is_person = false` accounts only. Use `?include_people=true` to include person virtual accounts. Use `?include_archived=true` to include archived accounts. Use `?include_deleted=true` to include soft-deleted accounts.

Each account response includes `current_balance_cents` and `current_balance_home_cents` (balance converted to `main_currency`).

### `POST /accounts`
Creates a new bank account.

**Required:** `name`, `currency_code`
**Optional:** `color`, `sort_order`
**Forbidden:** `is_person` — person accounts are created automatically by the transfer engine when a person account is involved in a transfer. They cannot be created directly via this endpoint.

**Validation:**
- `name` must be unique per `(user_id, currency_code)`.
- `currency_code` must exist in `global_currencies`.
- `currency_code` is immutable after creation — any subsequent `PUT` that includes it returns `422`.

### `GET /accounts/{id}`
### `PUT /accounts/{id}`

Fields that can be updated: `name`, `color`, `sort_order`.
`currency_code` is immutable. Returns `422` if included in the request body.

### `DELETE /accounts/{id}`
Soft-deletes the account (`deleted_at = now()`). Returns `409` if the account has any non-deleted transactions — the client must archive instead.

### `POST /accounts/{id}/archive`
Sets `is_archived = true`. The account disappears from all pickers and entry flows but all historical transactions remain intact and participate in reports.

---

## Categories

### `GET /categories`
Returns all active categories, sorted by `sort_order`. System categories (`is_system = true`) are always included and always appear first. Supports standard pagination. Use `?include_deleted=true` to include soft-deleted categories.

### `POST /categories`
**Required:** `name`, `color`
**Optional:** `sort_order`

Categories carry no type restriction. The same category can be used on expenses, income, and transfers — including refunds (same category as the original expense, positive amount).

**Auto-creation (engine-side, not via this endpoint):**
- `@Debt` — auto-created the first time a person account is involved in a transaction.
- `@Transfer` — auto-created the first time a real-account transfer is created.
Both are created with `is_system = true`.

### `PUT /categories/{id}`
Cannot rename or delete system categories (`is_system = true`). Returns `403`.

### `DELETE /categories/{id}`
Soft-delete. Returns `409` if the category is referenced by any non-deleted transaction (inbox or ledger). System categories always return `403`.

---

## Hashtags

### `GET /hashtags`
Returns all active hashtags, sorted by `sort_order`. Supports standard pagination. Use `?include_deleted=true` to include soft-deleted hashtags.

### `POST /hashtags`
**Required:** `name`
**Optional:** `sort_order`

### `PUT /hashtags/{id}`
### `DELETE /hashtags/{id}`
Soft-delete. Removes all `expense_transaction_hashtags` rows for this hashtag atomically.

---

## Inbox

### `GET /inbox`
Returns all active inbox items (`status = 1`, `deleted_at IS NULL`).

Optional filters: `?ready=true` (only items ready to promote — all required fields present and `date ≤ now()`), `?overdue=true` (items with `date` in the past).

### `POST /inbox`
Creates a new inbox item. All fields optional except `user_id` (from JWT). Missing required promotion fields are fine — the item is just not ready to promote yet.

`amount_cents` follows the standard sign convention: negative = expense, positive = income. The engine infers `transaction_type` from the sign and stores `amount_cents` as positive (same as the ledger). `transaction_type` is stored on the inbox row so direction is preserved through to promotion.

Auto-populates `exchange_rate` from `exchange_rates` table for the transaction's `date` and `account_id.currency_code` if both are present. Falls back to most recent available rate for that pair if no exact date match.

### `GET /inbox/{id}`
### `PUT /inbox/{id}`
Partial update. Re-evaluates promotion readiness after every update. If `date` changes and `account_id` is set, re-fetches and updates `exchange_rate` automatically (user can still override).

### `DELETE /inbox/{id}`
Soft-delete.

### `POST /inbox/{id}/promote`
Promotes a ready inbox item to the ledger.

**Validation (engine enforces, not the client):**
- `title` is present and not `'UNTITLED'`
- `amount_cents` is present and not zero
- `date` is present and `≤ now()`
- `account_id` is present and references an active, non-archived account
- `category_id` is present and references an active category

If any condition fails, returns `422` with the specific failing fields.

**On success (atomic):**
1. Creates `expense_transactions` row with `inbox_id` pointing back to this inbox item. Copies `transaction_type` from the inbox row. Computes `amount_home_cents` from `amount_cents × exchange_rate`.
2. Sets `status = 2` (promoted) on the inbox row.
3. Sets `deleted_at` on the inbox row (soft delete).
4. Updates `current_balance_cents` on the account (decrements for expenses, increments for income).
5. Writes `activity_log` entry (action=1 created) for the new transaction.
6. Writes `activity_log` entry (action=3 deleted) for the inbox item.

`status = 2` distinguishes a promoted inbox item from a dismissed one (`status = 3`) — both end up soft-deleted, but the reason is preserved.

Returns the newly created `expense_transactions` object.

---

## Transactions (Ledger)

### `GET /transactions`
Returns all active ledger transactions. Supports filtering:
- `?account_id=` — filter by account
- `?category_id=` — filter by category
- `?hashtag_id=` — filter by hashtag
- `?date_from=` / `?date_to=` — date range (ISO 8601)
- `?cleared=true/false`
- `?search=` — full-text search across `title` and `description`

### `POST /transactions`
Creates a transaction directly in the ledger, bypassing the inbox. Used by the CLI for fast entry when all required fields are known.

**Required:** `title`, `amount_cents`, `date`, `account_id`, `category_id`
**Optional:** `description`, `exchange_rate` (auto-populated if omitted), `cleared`

Auto-populates `exchange_rate` and computes `amount_home_cents` same as inbox.

**On success (atomic):**
1. Creates `expense_transactions` row.
2. Updates `current_balance_cents` on the account.
3. Writes `activity_log` entry.

### `GET /transactions/{id}`
### `PUT /transactions/{id}`
Partial update.

**Field locking:** If the transaction belongs to a completed reconciliation (`reconciliation_id` is set and reconciliation `status = 2`), these fields are read-only: `amount_cents`, `account_id`, `title`, `date`. Attempting to update them returns `422`.

**Date change:** If `date` changes, the engine automatically re-fetches the historical exchange rate for the new date and recalculates `amount_home_cents`. The user's manually set `exchange_rate` is replaced with the historical rate for the new date. (If the user then wants to override it, they can in a follow-up PUT.)

**Balance update:** If `amount_cents` or `account_id` changes, `current_balance_cents` is updated atomically on the affected account(s).

### `DELETE /transactions/{id}`
Soft-delete. Updates `current_balance_cents` on the account atomically.

If the transaction belongs to a completed reconciliation, returns a warning in the response body but still allows deletion. The reconciliation's totals become stale — the engine does not auto-adjust them.

If the transaction has a `transfer_transaction_id`, both the transaction and its paired sibling are soft-deleted atomically.

### `POST /transactions/batch`
Batch create. Array of transaction objects, processed as a single database transaction — all succeed or all fail.

**Use cases:** Bulk historical entry. CSV import is a later phase — when implemented, it will also use this endpoint.

Returns an array of created transaction objects and an array of any validation errors (with the index of the failing item).

---

## Transfers

Transfers are not a separate endpoint — they are created via `POST /transactions` or `POST /inbox` using the `transfer` field.

### Transfer request shape
Include a `transfer` object on any transaction create request:

```json
{
  "title": "BCP to Chase",
  "amount_cents": -6000,
  "account_id": "<bcp_pen_id>",
  "category_id": "<other_category_id>",
  "date": "2024-03-15T00:00:00Z",
  "transfer": {
    "account_id": "<chase_usd_id>",
    "amount_cents": 1500
  }
}
```

**Business logic (atomic):**
1. Creates the primary transaction (the one in the request body).
2. Creates the paired transaction on `transfer.account_id` with `transfer.amount_cents`.
3. Links both via `transfer_transaction_id` (each row points to the other).
4. Auto-assigns categories: if either account `is_person = true`, that side gets `@Debt`; both real accounts get `@Transfer`. These override any `category_id` passed in the request.
5. Auto-creates `@Debt` or `@Transfer` system categories if they don't exist yet.
6. **Zero-sum validation:** The engine does not enforce that the two `amount_cents` values are equal in raw number — they may be in different currencies. It does enforce that the two transactions are directionally opposite (one negative, one positive). Returns `422` if both are the same sign. **Explicit decision:** No magnitude equality check is performed even when both accounts share the same currency. This keeps the logic simple and allows users to record unequal amounts intentionally (e.g., fees absorbed during transfer).
7. Updates `current_balance_cents` on both accounts.
8. Writes `activity_log` entries for both transactions.

---

## Reconciliations

### `GET /reconciliations`
Returns all reconciliation batches for the user.

### `POST /reconciliations`
Creates a new draft reconciliation batch.

**Required:** `account_id`, `name`
**Optional:** `date_start`, `date_end`, `beginning_balance_cents`, `ending_balance_cents`

### `GET /reconciliations/{id}`
Returns the reconciliation plus all transactions currently assigned to it.

### `PUT /reconciliations/{id}`
Updates metadata fields. Cannot update `status` directly — use the complete/revert endpoints.

### `POST /reconciliations/{id}/complete`
Marks the reconciliation as complete (`status = 2`). From this point, the four locked fields (`amount_cents`, `account_id`, `title`, `date`) become read-only on all assigned transactions.

**Validation:** Returns `422` if no transactions are assigned to the batch.

### `POST /reconciliations/{id}/revert`
Reverts status to draft (`status = 1`). Unlocks all fields on assigned transactions.

### `DELETE /reconciliations/{id}`
Soft-delete. Only allowed if `status = 1` (draft). Returns `409` if status is completed — revert first.

---

## Sync

### `GET /sync`
Delta sync endpoint. Returns all records that have changed since the client's last sync.

**Query params:**
- `sync_token=*` — full fetch, returns all active records and creates a new checkpoint.
- `sync_token=<token>` — delta fetch, returns only records with `version` higher than the checkpoint.

**Response shape:**
```json
{
  "sync_token": "<new_token>",
  "accounts": [...],
  "categories": [...],
  "hashtags": [...],
  "inbox": [...],
  "transactions": [...],
  "reconciliations": [...]
}
```

Deleted records are included with `deleted_at` set (tombstones). The client removes any record from local state where `deleted_at` is not null.

---

## Dashboard & Reporting

### `GET /dashboard`

Returns the current calendar month overview. Single endpoint, one call, everything needed to render the main dashboard view.

**Response shape:**

```json
{
  "month": { "year": 2026, "month": 4 },
  "bank_accounts": [
    {
      "id": "...",
      "name": "BCP Soles",
      "currency_code": "PEN",
      "current_balance_cents": 125000,
      "current_balance_home_cents": 125000
    }
  ],
  "people": [
    {
      "id": "...",
      "name": "Alex",
      "currency_code": "PEN",
      "current_balance_cents": -4500,
      "current_balance_home_cents": -4500
    }
  ],
  "categories": [
    {
      "id": "...",
      "name": "Food",
      "spent_cents": 50000,
      "spent_home_cents": 50000,
      "hashtag_breakdown": [
        {
          "hashtag_combination": ["<lunch_id>", "<work_id>"],
          "spent_cents": 30000,
          "spent_home_cents": 30000
        },
        {
          "hashtag_combination": ["<groceries_id>"],
          "spent_cents": 15000,
          "spent_home_cents": 15000
        },
        {
          "hashtag_combination": [],
          "spent_cents": 5000,
          "spent_home_cents": 5000
        }
      ]
    }
  ],
  "totals": {
    "inflow_cents": 800000,
    "inflow_home_cents": 800000,
    "outflow_cents": 320000,
    "outflow_home_cents": 320000,
    "net_cents": 480000,
    "net_home_cents": 480000
  }
}
```

**Field rules:**

- `bank_accounts` includes only `is_person = false`, `is_archived = false`, `deleted_at IS NULL`. Sorted by `sort_order`.
- `people` includes only `is_person = true`, `deleted_at IS NULL`. Same shape as `bank_accounts`, separated for client convenience.
- `categories` includes every non-deleted category, even if `spent_cents = 0` (so the client can render the full category list without a second call). Sorted by `sort_order`.
- **`hashtag_breakdown`** — array of `{ hashtag_combination, spent_cents, spent_home_cents }` rows. Aggregation is `GROUP BY (category_id, sorted_array_of_hashtag_ids)`. The hashtag set is sorted by `id` before grouping so `[#a, #b]` and `[#b, #a]` collapse to the same row. Transactions with no hashtags appear as a row with `hashtag_combination: []`. **The sum of all `hashtag_breakdown` rows under a category equals that category's `spent_cents` exactly** — no double-counting, no orphaned amounts.
- `totals.inflow_cents` / `outflow_cents` are the sum of all positive and negative transactions in the current month, expressed in `main_currency`. Native-currency totals are not meaningful when accounts span currencies, so only `_home_cents` is authoritative; the non-home fields are provided for single-currency users.
- All `*_home_cents` fields are pre-converted by the engine. Clients never compute currency conversions.
- "Current month" means `[first_day_of_month, last_day_of_month]` in the user's `display_timezone`.

### `GET /reports/monthly`

Returns flow data (what happened) for any historical month or month range. **Does not return balances** — balances are a "now" concept and live on `/dashboard` only. If you ever need point-in-time historical balances, that's a separate endpoint.

**Response shape (single month):**

```json
{
  "month": { "year": 2026, "month": 3 },
  "categories": [
    {
      "id": "...",
      "name": "Food",
      "spent_cents": 50000,
      "spent_home_cents": 50000,
      "hashtag_breakdown": [
        { "hashtag_combination": ["<lunch_id>", "<work_id>"], "spent_cents": 30000, "spent_home_cents": 30000 },
        { "hashtag_combination": ["<groceries_id>"], "spent_cents": 15000, "spent_home_cents": 15000 },
        { "hashtag_combination": [], "spent_cents": 5000, "spent_home_cents": 5000 }
      ]
    }
  ],
  "totals": {
    "inflow_cents": 800000,
    "inflow_home_cents": 800000,
    "outflow_cents": 320000,
    "outflow_home_cents": 320000,
    "net_cents": 480000,
    "net_home_cents": 480000
  }
}
```

`categories` and `hashtag_breakdown` follow the exact same rules as `/dashboard` (every non-deleted category included, breakdown rows sum to the parent category total, `hashtag_combination: []` for transactions with no hashtags). `totals` uses the same inflow/outflow/net structure.

**Query params:**
- `year`, `month` — single month. Returns the shape above.
- `from_year`, `from_month`, `to_year`, `to_month` — multi-month range (inclusive on both ends). Response wraps per-month payloads in a `months` array, oldest first:

```json
{
  "months": [
    { "month": { "year": 2025, "month": 11 }, "categories": [...], "totals": {...} },
    { "month": { "year": 2025, "month": 12 }, "categories": [...], "totals": {...} }
  ]
}
```

The two query forms are mutually exclusive. Passing both → `422`. Passing neither → `422`. Range queries are capped at 24 months → `422` if exceeded.

---

## Activity Log

### `GET /activity`
Returns the activity log for the user. Supports filtering by `resource_type` and `resource_id`. Sorted by `created_at` descending. Useful for debugging and audit.

---

## Exchange Rates

### `GET /exchange-rates`
**Query params:** `base` (default `USD`), `target`, `date` (ISO date, default today)

Returns the rate for the given pair and date. Falls back to the most recent available rate if no exact match exists for the requested date.

Used internally by the engine. Also exposed for CLI use.
