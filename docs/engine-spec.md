# Expense Engine — API Spec

> The `expense_world_engine` is the Brain. This document defines every endpoint, every business logic rule, and every validation the engine enforces. Nothing exists for any client unless it is defined here first.
>
> Architecture: `api-design-principles.md` | Schema: `schema-reference.md`

---

## Base Conventions

**Base URL:** `https://expense-world-engine.onrender.com/v1` (production) / `http://localhost:8000/v1` (local)

**Authentication:** Every request requires `Authorization: Bearer <token>`. The engine validates the Supabase JWT, extracts `user_id`, and passes it to all downstream logic. Unauthenticated requests return `401`.

**Client-supplied UUIDs:** Every `POST` that creates a resource requires an `id: UUID` field in the request body. The client generates the UUID locally (e.g., `uuid4()`) before making the call — the server never picks the id. This enables offline-first clients to reference a resource before the request completes, and makes idempotent retries trivial: a second POST with the same `id` returns `409 CONFLICT` (existing resource), not a duplicate.

**Idempotency:** Write operations (`POST`, `PUT`, `DELETE`) should include `X-Idempotency-Key: <uuid>`. The engine records `(user_id, key) → (response_body, response_status)` in `idempotency_keys` and acquires a transaction-scoped advisory lock on every incoming request to serialize concurrent retries with the same key at the DB. Duplicate requests return the stored response **verbatim, including the original HTTP status code** — no per-route drift. TTL is 24 hours.

**Sign convention — requests:** `amount_cents` in request bodies uses a signed convention. The engine infers `transaction_type` from the sign — the caller never fills it in manually. Negative = expense/outflow (subtracts from balance). Positive = income/inflow (adds to balance). Transfers are identified by the presence of a `transfer` field in the request body, not by sign.

**Sign convention — storage:** Internally, `amount_cents` is always stored as a positive integer. `transaction_type` (1=expense, 2=income, 3=transfer) and `transfer_direction` (1=debit, 2=credit) are set by the engine based on the inferred direction. Callers never interact with these fields on writes.

**Sign convention — responses:** `amount_cents` in responses is always positive. `transaction_type` tells the client the direction. Pass `?debit_as_negative=true` on any amount-bearing read endpoint to receive negative amounts for expenses and outflows — useful for clients that prefer signed display. Supported on: `/transactions` list + detail, `/inbox` list + detail, `/reconciliations/{id}`, `/sync`. Accepted but a no-op on `/dashboard` and `/reports/monthly`, whose aggregates are already signed by construction (category spent is positive for income and negative for expense; totals return split positive inflow/outflow).

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

**`fields` semantics:** On `VALIDATION_ERROR` responses, `fields` is always an object (possibly empty) — never `null`. Clients can uniformly iterate `Object.keys(error.fields)` without a null check. Two precondition-unmet codes also carry field-scoped payloads: `SETTINGS_MISSING` (`fields: {"user_settings": ...}`) and `RATE_UNAVAILABLE` (`fields: {"exchange_rate": ...}`), both returned as `422`. On other non-validation errors (`UNAUTHORIZED`, `NOT_FOUND`, `FORBIDDEN`, `CONFLICT`, `INTERNAL_ERROR`), `fields` is `null` — those errors aren't field-scoped. The envelope key is still present in every response.

**Global exception coverage:** Four handlers are registered: `AppError` (canonical raises from domain code), `RequestValidationError` (Pydantic), `StarletteHTTPException` (routing-level 404/405/413/415/429), and a catch-all `Exception` handler returning `500 INTERNAL_ERROR` after logging the traceback server-side. Tracebacks never leak to clients; every error response carries the canonical envelope.

**Pagination:** List endpoints accept `?limit=50&offset=0`. FastAPI rejects out-of-range values at the query layer (`limit` must be `[1, 200]`, `offset` must be `≥0`) with `422 VALIDATION_ERROR` before the handler runs. Response shape: `{items, total, limit, offset}`.

**Soft-deleted records:** Excluded from all list responses by default. Pass `?include_deleted=true` to include them.

**Restore semantics:** Every resource with a delete endpoint also exposes `POST /{resource}/{id}/restore`. Restores clear `deleted_at` and write a `RESTORED` activity log entry. See per-resource sections for collision rules (e.g., restoring a category whose name now collides with an active one returns `409`).

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
Called by any client immediately after a successful Supabase sign-in, on every new device. Creates the `users` and `user_settings` rows if they don't exist (idempotent upsert). Returns the full user profile.

**Status code:** Returns `200`, not `201`. Bootstrap has upsert semantics — first call creates the rows, subsequent calls bump `last_login_at` on the existing rows. First-call and replay statuses are both 200.

**Request body:**
```json
{
  "display_name": "Alex",
  "timezone": "America/Lima"
}
```

Note: `/auth/bootstrap` does **not** take a client-supplied `id` — the `users.id` is always the Supabase JWT `sub` claim.

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
Updates `user_settings`. Partial update — only supplied fields are changed. If no fields are supplied, returns current settings without making changes. Every successful update bumps `version` and `updated_at` so the next `GET /sync` surfaces the change to other devices.

**Special case — `main_currency` change:** If `main_currency` actually changes (old != new), the engine runs a synchronous recalculation inside the same request that rewrites home-currency amounts across three passes:

1. **Regular transactions** (`transfer_transaction_id IS NULL`): for each row, look up `get_rate(account_currency → new_main_currency, tx.date)`, rewrite `exchange_rate` and `amount_home_cents`.
2. **Transfer pairs** (`transfer_transaction_id IS NOT NULL`): reapply the dominant-side rule — the leg whose account currency matches the new `main_currency` is dominant (`amount_home_cents = amount_cents`, `exchange_rate = 1.0`); the other leg's `amount_home_cents` is forced to match so the pair nets to zero.
3. **Pending inbox items** (`status = 1`, `account_id IS NOT NULL`): recompute `exchange_rate` so future promotions use the correct home currency.

`current_balance_home_cents` on accounts does **not** need updating — it is computed at read time, not stored. Every updated transaction row bumps `version + updated_at` so `/sync` surfaces the changes. A **single** `activity_log` entry records the currency change plus the recalculation summary: `regular_transactions`, `transfer_transactions`, `orphan_transfer_legs`, `inbox_items`, and `total`. The `orphan_transfer_legs` counter surfaces transfer legs whose sibling was soft-deleted — the helper can't reapply the dominant-side rule without both legs, so it skips them and records the count so ops can resolve the orphans via the transactions API. Individual transaction updates are **not** logged — this is a deliberate aggregate-exception to the "every mutation gets an activity_log entry" rule. A full recalc on a busy user can rewrite tens of thousands of rows in a single request; per-row entries would inflate `activity_log` by orders of magnitude without answering useful audit questions. The single `user_settings` UPDATED entry is the canonical record. No-op when `main_currency` doesn't actually change. Synchronous in Phase 1; a future async variant with `recalculation_job_id` polling can be added when volumes require it. See [roadmap.md Step 9.1](../docs/roadmap.md).

**Settings preconditions:** Endpoints that read `user_settings` (dashboard, reports, recalc) return `422 SETTINGS_MISSING` with `fields: {"user_settings": "Must be provisioned via POST /v1/auth/bootstrap."}` if the user has not completed bootstrap. This is a precondition-unmet state, not a conflict.

**Exchange-rate preconditions:** Any write that needs to compute `amount_home_cents` for a cross-currency account (`POST /transactions`, `PUT /transactions/{id}` with a `date` change, `POST /transactions/batch`, `POST /inbox`, `PUT /inbox/{id}` with a `date` change) returns `422 RATE_UNAVAILABLE` with `fields: {"exchange_rate": "No rate on or before <date> for <from>-><to>. Wait for the daily fetch or supply an explicit exchange_rate."}` when no `exchange_rates` row exists on or before the transaction's `date`. No silent `1.0` fallback — a missing rate fails loudly so `amount_home_cents` cannot be corrupted. Clients can either retry after the daily FX cron runs (see [TODO.md](../TODO.md)) or bypass the lookup by supplying an explicit `exchange_rate` on the request. Same-currency accounts short-circuit to the identity rate and never hit this path.

---

## Bank Accounts

### `GET /accounts`
Returns all active bank accounts. Includes `is_person = false` accounts only. Use `?include_people=true` to include person virtual accounts. Use `?include_archived=true` to include archived accounts. Use `?include_deleted=true` to include soft-deleted accounts.

Each account response includes `current_balance_cents` and `current_balance_home_cents` (balance converted to `main_currency`).

### `POST /accounts`
Creates a new bank account (real account only — `is_person = false`).

**Required:** `id` (client-supplied UUID), `name`, `currency_code`
**Optional:** `color`, `sort_order`
**Forbidden:** `is_person`, and any unknown field. Person accounts are **not** created through this endpoint; they are created explicitly via the People API (see **People / Person Accounts** below). Requests that include `is_person` (with any value) or any other unknown field return `422 VALIDATION_ERROR`.

**Validation:**
- `name` must be unique per `(user_id, currency_code)`.
- `currency_code` must exist in `global_currencies`.
- `currency_code` is immutable after creation — any subsequent `PUT` that includes it returns `422`.
- `id` must not collide with an existing account — returns `409 CONFLICT` if taken.

### `GET /accounts/{id}`
### `PUT /accounts/{id}`

Fields that can be updated: `name`, `color`, `sort_order`.
`currency_code` is immutable. Returns `422` if included in the request body.

### `DELETE /accounts/{id}`
Soft-deletes the account (`deleted_at = now()`). Returns `409` if the account has any non-deleted transactions — the client must archive instead.

### `POST /accounts/{id}/restore`
Undoes a soft-delete by clearing `deleted_at`. Returns `404` if no soft-deleted account with that id exists. Writes a `RESTORED` activity log entry with before/after snapshots.

### `POST /accounts/{id}/archive`
Sets `is_archived = true`. The account disappears from all pickers and entry flows but all historical transactions remain intact and participate in reports. Bumps `version` and writes an `UPDATED` activity log entry.

### `POST /accounts/{id}/unarchive`
Inverse of `/archive`: sets `is_archived = false` and bumps `version`. Returns `404` if no active account with that id exists. Writes an `UPDATED` activity log entry. Calling on an already-active account is allowed (still bumps version + writes activity) so the round-trip is idempotent at the HTTP layer and explicit in the audit trail.

---

## People / Person Accounts

Person accounts (`is_person = true`) represent people the user lends to or borrows from (debt tracking). They share the `expense_bank_accounts` table with real accounts but are created, listed, and managed through a dedicated People API.

**Design rule:** Person accounts are **only** created via the explicit People API described below. They are **never** auto-created as a side effect of creating a transfer, promoting an inbox item, or any other action. A transfer targeting a non-existent person returns `422 VALIDATION_ERROR`; the client must create the person first, then retry the transfer with the resolved `account_id`.

**Rationale:** Explicit creation keeps the user in control of their people list, avoids mystery rows, and prevents race conditions where two devices initiating a transfer to the same new person create duplicate person accounts.

### `POST /people` *(Phase 4 — planned, not yet implemented)*
Creates a person account.

**Required:** `id` (client-supplied UUID), `name`, `currency_code`
**Optional:** `color`, `sort_order`

Response shape is identical to a bank account with `is_person = true`.

Until this endpoint ships, person accounts cannot be created through the API. The data path is ready (reads, balances, dashboard segregation, `@Debt` auto-categorization on transfers) — only the creation endpoint is pending.

---

## Categories

### `GET /categories`
Returns all active, non-archived categories, sorted by `sort_order`. System categories (`is_system = true`) are always included and always appear first. Supports standard pagination. Use `?include_archived=true` to include archived categories. Use `?include_deleted=true` to include soft-deleted categories.

### `POST /categories`
**Required:** `id` (client-supplied UUID), `name`, `color`
**Optional:** `sort_order`

**Name normalization:** `name` is trimmed before storage. An empty-after-trim name returns `422 VALIDATION_ERROR` with `fields: {"name": "Must not be empty."}`. Uniqueness is **case-insensitive** per user: "Food", "food", and "FOOD" collide. A conflicting name returns `409 CONFLICT`. The database enforces this with a partial unique index on `(user_id, LOWER(name)) WHERE deleted_at IS NULL`, so deleting a category and creating a new one with the same name works as expected.

Categories carry no type restriction. The same category can be used on expenses, income, and transfers — including refunds (same category as the original expense, positive amount).

**Auto-creation (engine-side, not via this endpoint):**
- `@Debt` — auto-created the first time a person account is involved in a transaction.
- `@Transfer` — auto-created the first time a real-account transfer is created.
Both are created with `is_system = true` and a stable `system_key` column (`"debt"` / `"transfer"`) — the engine looks them up by `system_key`, not by display name. This means users can freely rename the display text without breaking future transfer pipelines (which was a bug before the `system_key` column was added).

### `PUT /categories/{id}`
System categories (`is_system = true`) CAN be renamed — the engine identifies them by `system_key`, not by `name`. Any other field is also editable. Returns `404` if the category is missing. The same name normalization rules as `POST` apply: renames are trimmed, empty names return `422`, and case-insensitive conflicts return `409`.

### `DELETE /categories/{id}`
Soft-delete. Returns `409` if the category is referenced by any non-deleted transaction (inbox or ledger). System categories (`is_system = true`) always return `403` — they must remain available for the transfer pipeline.

### `POST /categories/{id}/restore`
Undoes a soft-delete. Returns `404` if no soft-deleted category with that id exists. Returns `409` if an active category already uses the same name (the name collision check prevents silent duplicates). Writes a `RESTORED` activity log entry.

### `POST /categories/{id}/archive`
Sets `is_archived = true`. The category disappears from default `GET /categories` listings and dashboard month panels but remains attached to historical transactions. Bumps `version` and writes an `UPDATED` activity log entry. Returns `404` if the category is missing or soft-deleted. Returns `403` for system categories (`is_system = true`) — the transfer pipeline relies on them remaining available, mirroring the delete guard.

**Attach guard:** Once archived, the engine refuses to attach the category to any new or updated transaction. `POST /transactions`, `PUT /transactions/{id}`, `POST /transactions/batch`, and `POST /inbox/{id}/promote` all return `422 VALIDATION_ERROR` with `fields: {"category_id": "Must reference an active, non-archived category."}` if `category_id` references an archived row. The same guard fires on `POST /transactions/{id}/restore` if the original category has been archived in the meantime — the restore is rejected until the category is unarchived. Inbox items pointing at an archived category are also dropped from `GET /inbox?ready=true`. This is the same parity rule that already applies to archived accounts.

### `POST /categories/{id}/unarchive`
Inverse of `/archive`: sets `is_archived = false` and bumps `version`. Returns `404` if no active category with that id exists. Writes an `UPDATED` activity log entry.

---

## Hashtags

### `GET /hashtags`
Returns all active, non-archived hashtags, sorted by `sort_order`. Supports standard pagination. Use `?include_archived=true` to include archived hashtags. Use `?include_deleted=true` to include soft-deleted hashtags.

### `POST /hashtags`
**Required:** `id` (client-supplied UUID), `name`
**Optional:** `sort_order`

**Name normalization:** `name` is trimmed before storage. An empty-after-trim name returns `422 VALIDATION_ERROR`. Uniqueness is **case-insensitive** per user and scoped to non-deleted rows via a partial unique index on `(user_id, LOWER(name)) WHERE deleted_at IS NULL`. A conflicting name returns `409 CONFLICT`.

### `PUT /hashtags/{id}`
The same name normalization rules as `POST` apply to renames.
### `DELETE /hashtags/{id}`
Soft-delete. Cascades: soft-deletes all `expense_transaction_hashtags` junction rows for this hashtag and bumps each affected parent transaction's `version + updated_at` so the next `/sync` delta carries the hashtag_ids change. Writes a single `DELETED` activity log entry for the hashtag itself; per-junction-row entries are deliberately NOT written (see "Activity log aggregate exceptions" below).

### `POST /hashtags/{id}/restore`
Undoes a soft-delete of the hashtag row itself. Does NOT automatically restore the cascaded junction rows — the restored hashtag comes back as an empty label that the user can re-apply manually to transactions. Silently re-tagging could surprise users. Returns `404` if no soft-deleted hashtag with that id exists. Returns `409` if an active hashtag already uses the same name.

### `POST /hashtags/{id}/archive`
Sets `is_archived = true`. The hashtag disappears from default `GET /hashtags` listings but its `expense_transaction_hashtags` junction rows are intentionally left intact — archive is a soft hide, not a destruction of links. Bumps `version` and writes an `UPDATED` activity log entry. Returns `404` if the hashtag is missing or soft-deleted.

**Attach guard:** Once archived, the engine refuses to attach the hashtag to any new or updated transaction via `hashtag_ids`. `POST /transactions` and `PUT /transactions/{id}` return `422 VALIDATION_ERROR` with `fields: {"hashtag_ids": "Invalid IDs: ..."}` if any id in the list references an archived hashtag. Existing junction rows on transactions that already had this hashtag attached before archive remain intact and continue to surface in dashboards and reports.

### `POST /hashtags/{id}/unarchive`
Inverse of `/archive`: sets `is_archived = false` and bumps `version`. Returns `404` if no active hashtag with that id exists. Writes an `UPDATED` activity log entry.

---

## Inbox

### `GET /inbox`
Returns all active inbox items (`status = 1`, `deleted_at IS NULL`).

Optional filters: `?ready=true` (only items ready to promote — all required fields present and `date ≤ now()`), `?overdue=true` (items with `date` in the past).

### `POST /inbox`
Creates a new inbox item.

**Required:** `id` (client-supplied UUID). All other fields are optional — the engine accepts sparse inbox rows and waits for later edits to complete them.

`amount_cents` follows the standard sign convention: negative = expense, positive = income. The engine infers `transaction_type` from the sign and stores `amount_cents` as positive (same as the ledger). `transaction_type` is stored on the inbox row so direction is preserved through to promotion.

Auto-populates `exchange_rate` from `exchange_rates` table for the transaction's `date` and `account_id.currency_code` if both are present. If either field is absent, the column stores the DB default of `1.0` (partial inbox rows are allowed; rate is resolved at promote time). If both are present but no rate row exists on or before `date` for the pair, the request fails with `422 RATE_UNAVAILABLE` — no silent fallback. Same-currency accounts always resolve via the identity-rate short-circuit.

**Response shape:** Inbox rows include both native AND home-currency amounts:
- `amount_cents` + `amount_home_cents` — computed as `amount_cents × exchange_rate` at read time.
- `transfer_amount_cents` + `transfer_amount_home_cents` — same computation, using the stored (signed) transfer amount.

Pass `?debit_as_negative=true` on `GET /inbox` or `GET /inbox/{id}` to have the primary `amount_cents` and `amount_home_cents` returned negated for EXPENSE items and for the outflow leg of transfer items. `transfer_amount_cents` is left as-stored (it's already signed on purpose — the promote flow uses it for zero-sum validation).

### `GET /inbox/{id}`
### `PUT /inbox/{id}`
Partial update. Re-evaluates promotion readiness after every update. If `date` changes and `account_id` is set, re-fetches and updates `exchange_rate` automatically (user can still override).

### `DELETE /inbox/{id}`
Soft-delete. Sets `deleted_at = now()` without touching `status`, so the row remains `status = 1` (PENDING) + `deleted_at IS NOT NULL` — distinct from the PROMOTED end-state (`status = 2` + `deleted_at IS NOT NULL`).

### `POST /inbox/{id}/restore`
Undoes a soft-delete on a **pending** inbox item (`status = 1`). Clears `deleted_at` and writes a `RESTORED` activity log entry.

Returns `409 CONFLICT` if the row is soft-deleted but `status != 1` — promoted inbox items are not restorable here because the ledger transaction they created still exists, and restoring the inbox side would leave the user one promote-click away from a duplicate ledger row. The error message points the client at the ledger: to undo a promotion, delete the ledger transaction instead.

Returns `404 NOT_FOUND` if no soft-deleted inbox row with that id exists (including "row exists but is still active" — use that route's natural affordances instead).

### `POST /inbox/{id}/promote`
Promotes a ready inbox item to the ledger.

**Request body (required):**
```json
{
  "id": "<uuid>",
  "transfer_id": "<uuid or null>"
}
```

- `id` — the client-supplied UUID for the newly-created ledger `expense_transactions` row.
- `transfer_id` — the client-supplied UUID for the paired sibling ledger row when promoting a transfer inbox item. Required when the inbox row has `transfer_account_id` + `transfer_amount_cents` set; must be `null` (or omitted) otherwise. Returns `422` if mismatched.

**Validation (engine enforces, not the client):**
- `title` is present and not `'UNTITLED'`
- `amount_cents` is present and not zero
- `date` is present and `≤ now()`
- `account_id` is present and references an active, non-archived account
- `category_id` is present and references an active category (non-transfer items only — transfer items auto-assign the system category)

If any condition fails, returns `422` with the specific failing fields.

**On success (atomic):**
1. Creates `expense_transactions` row(s) using the client-supplied `id` (and `transfer_id` for the sibling). `inbox_id` points back to this inbox item. Copies `transaction_type` from the inbox row. Computes `amount_home_cents` from `amount_cents × exchange_rate`.
2. Sets `status = 2` (promoted) on the inbox row.
3. Sets `deleted_at` on the inbox row (soft delete).
4. Updates `current_balance_cents` on the account (decrements for expenses, increments for income).
5. Writes `activity_log` entry (action=1 CREATED) for the new transaction(s).
6. Writes `activity_log` entry (action=3 DELETED) for the inbox item.

`status = 2` distinguishes a promoted inbox item from a dismissed one (which stays at `status = 1` with `deleted_at` set) — both end up soft-deleted, but the reason is preserved via the status column. Only the PENDING + deleted combination is restorable via `POST /inbox/{id}/restore`.

Returns the newly created `expense_transactions` object (primary leg for transfer promotions).

---

## Transactions (Ledger)

**Hashtag wire format:** every transaction returned by the sync endpoint includes a `hashtag_ids: [uuid, ...]` array (sorted ascending) listing every hashtag attached to it. The junction table `expense_transaction_hashtags` is internal storage only — clients never see junction rows. Mutations to a transaction's hashtag set bump the parent transaction's `version` and `updated_at` in the same DB transaction so delta sync always surfaces the change. Individual list/get endpoints return the same `version`/`updated_at` columns but do not currently embed `hashtag_ids` (clients fetching a single transaction can use `?hashtag_id=` filtered listings or the dedicated hashtag endpoints).

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

**Required:** `id` (client-supplied UUID), `title`, `amount_cents`, `date`, `account_id`, `category_id`
**Optional:** `description`, `exchange_rate` (auto-populated if omitted), `cleared`, `hashtag_ids`, `transfer`

For transfer requests, the `transfer` object additionally requires its own `id` field — the UUID of the sibling ledger row. Both `id` and `transfer.id` must be distinct and client-generated. Example:

```json
{
  "id": "<primary_uuid>",
  "title": "BCP to Chase",
  "amount_cents": -6000,
  "transfer": {
    "id": "<sibling_uuid>",
    "account_id": "<chase_usd_id>",
    "amount_cents": 1500
  }
}
```

Auto-populates `exchange_rate` and computes `amount_home_cents` same as inbox. Returns `409 CONFLICT` if `id` or `transfer.id` already exists.

**On success (atomic):**
1. Creates `expense_transactions` row.
2. Updates `current_balance_cents` on the account.
3. Writes `activity_log` entry.

### `GET /transactions/{id}`
### `PUT /transactions/{id}`
Partial update.

**Field locking:** If the transaction belongs to a completed reconciliation (`reconciliation_id` is set and reconciliation `status = 2`), these fields are read-only: `amount_cents`, `account_id`, `title`, `date`. Attempting to update them returns `422`.

**Transfer edit guard:** If the transaction is part of a transfer pair (`transfer_transaction_id` is set), these fields are read-only: `amount_cents`, `account_id`, `date`, `exchange_rate`, `amount_home_cents`. Attempting to update them returns `422`. Transfers must be deleted and re-created to change any of these — the PUT path mutates only the edited leg, so allowing any of them through would silently desync the pair (different dates, mismatched historical rates, legs that no longer net to zero in home currency). Other fields (`title`, `description`, `cleared`, `category_id`, `hashtag_ids`) remain editable per-leg.

**Date change:** If `date` changes on a non-transfer transaction, the engine automatically re-fetches the historical exchange rate for the new date and recalculates `amount_home_cents`. The user's manually set `exchange_rate` is replaced with the historical rate for the new date. (If the user then wants to override it, they can in a follow-up PUT.) This does not apply to transfer legs — `date` is blocked by the transfer edit guard above, so the re-rate path is never reached for transfers.

**Balance update:** If `amount_cents` or `account_id` changes, `current_balance_cents` is updated atomically on the affected account(s).

### `DELETE /transactions/{id}`
Soft-delete. Updates `current_balance_cents` on the account atomically.

**Response shape:** Always includes a `warnings: list[str]` field. Empty list when the delete is clean; populated with one or more strings when something notable happened. Currently the only warning emitted is `"Transaction belonged to a completed reconciliation. Reconciliation totals may be stale."` — the delete is still allowed (the engine does not auto-adjust the reconciliation's totals); the field surfaces the staleness so clients can render a notice.

If the transaction has a `transfer_transaction_id`, both the transaction and its paired sibling are soft-deleted atomically.

### `POST /transactions/{id}/restore`
Undoes a soft-delete on a transaction. Re-applies the balance impact on the account, re-activates the cascaded hashtag junction rows, and atomically restores the transfer sibling if the row is part of a pair. Returns the restored transaction with the same `warnings: list[str]` envelope as DELETE (empty when restore is clean).

**Reconciliation handling:** The transaction's `reconciliation_id` survives on the soft-deleted row. On restore, the link is conditionally cleared:

| Recon state at restore time | Action | Warning |
|---|---|---|
| `reconciliation_id` is null | nothing | no |
| Recon missing or soft-deleted | unlink (`reconciliation_id = null`) | yes |
| Recon `status = 2` (completed) | unlink | yes |
| Recon `status = 1` (draft) and active | **link preserved** | no |

Completed reconciliations lock four fields (`amount_cents`, `account_id`, `title`, `date`) on assigned transactions — silently re-linking would leave the restored row with frozen fields the user can't edit, so the engine forces an unlink and emits a warning. The DRAFT-and-active case is the user's good-path expectation: deleted by mistake mid-reconciliation, restoring back into the same batch is the natural undo.

This is intentionally asymmetric to `restore_reconciliation` (which never re-links transactions). The asymmetry is appropriate: restoring a single transaction is a small-blast-radius user undo where preserving the link in the common case matches expectations; restoring a reconciliation could re-touch many transactions that have since been edited or moved.

**Hashtag junctions:** Re-activated precisely. The cascade-restore `WHERE` clause matches junction rows whose `deleted_at` exactly equals the parent's pre-restore `deleted_at`, which (because `now()` returns one value per Postgres transaction) catches only the rows that the original delete cascade soft-deleted — not pre-existing soft-deleted junctions from earlier hashtag edits. This intentionally differs from `restore_hashtag` (which doesn't re-link junctions) because hashtag-restore touches MANY transactions while transaction-restore touches ONE.

**Failure modes:**
- `404 NOT_FOUND` — no soft-deleted row with that id (including "row exists but is already active").
- `422 VALIDATION_ERROR` — the row's `account_id` or `category_id` (or the transfer sibling's) is no longer active and non-archived. All blockers reported in a single `fields` dict before any mutation, so a 422 leaves the soft-deleted row untouched.
- `409 CONFLICT` — the row is part of a transfer pair but the sibling is missing or no longer soft-deleted (refusing to restore an asymmetric pair).

### `POST /transactions/batch`
Batch create. Array of transaction objects, processed as a single database transaction — all succeed or all fail.

Every item in the batch must carry its own client-supplied `id`. Duplicate ids within a single batch are rejected up front with `422 VALIDATION_ERROR` (`fields.items[i].id = "Duplicate id within batch."`). Transfers are not supported in batch creates; include a `transfer` field on any item and the whole batch is rejected.

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
   - **Note:** The transfer engine does **not** auto-create person accounts. Both `account_id` values in the request must reference accounts that already exist and are non-archived. If `transfer.account_id` references a non-existent or archived person, the request returns `422 VALIDATION_ERROR`. Callers create person accounts explicitly via the People API before initiating a transfer to that person.
6. **Zero-sum validation:** The engine does not enforce that the two `amount_cents` values are equal in raw number — they may be in different currencies. It does enforce that the two transactions are directionally opposite (one negative, one positive). Returns `422` if both are the same sign. **Explicit decision:** No magnitude equality check is performed even when both accounts share the same currency. This keeps the logic simple and allows users to record unequal amounts intentionally (e.g., fees absorbed during transfer).
7. **Home currency zero-sum (cross-currency transfers):** For transfers between accounts in different currencies, the engine uses the **implied rate from the entered amounts** (the rate the user actually got), not the market rate, when computing `amount_home_cents`. The side whose currency matches `main_currency` is dominant — its home value equals its native amount. The other side's `amount_home_cents` is forced to equal the dominant side's by direct assignment, and its `exchange_rate` is derived from that (stored for audit/display). This guarantees the pair nets to zero in home currency by construction, matching how production fintech systems (Stripe, Wise, QuickBooks Online) treat the execution rate as the historical spot rate for the transaction. No separate FX gain/loss is recognized at transaction time — that's a period-end remeasurement concern handled elsewhere (if ever).
8. Updates `current_balance_cents` on both accounts.
9. Writes `activity_log` entries for both transactions.

---

## Reconciliations

### `GET /reconciliations`
Returns all reconciliation batches for the user.

### `POST /reconciliations`
Creates a new draft reconciliation batch.

**Required:** `id` (client-supplied UUID), `account_id`, `name`
**Optional:** `date_start`, `date_end`, `beginning_balance_cents`, `ending_balance_cents`

**Response shape:** Every reconciliation response includes `beginning_balance_home_cents` and `ending_balance_home_cents` alongside the native fields. The engine converts using the account's currency and the exchange rate at `date_end` (or today if `date_end` is null). Values are `null` only when no rate is available for the pair; list endpoints deduplicate rate lookups by `(currency, date_end)` so a page of N reconciliations produces at most K rate reads where K = distinct currency/date pairs.

### `GET /reconciliations/{id}`
Returns the reconciliation plus a **paged window** of its assigned transactions.

**Query params:** `limit` (default 50, max 200, min 1), `offset` (default 0), `debit_as_negative` (bool, default false).

**Response additions:** the embedded list is wrapped with pagination metadata:

| Field | Type | Meaning |
|---|---|---|
| `transactions` | array | Up to `limit` transactions, ordered by `date DESC, created_at DESC`. |
| `transactions_total` | int | Total count of non-deleted transactions assigned to the reconciliation. |
| `transactions_limit` | int | Echoes the requested limit. |
| `transactions_offset` | int | Echoes the requested offset. |
| `transactions_truncated` | bool | `true` when `offset + transactions.length < transactions_total` — i.e. there are more rows beyond this page. |

For large reconciliations, the paged list endpoint `GET /transactions?reconciliation_id={id}` is a standalone escape hatch that supports the full filter surface (date range, category, hashtag, search).

### `PUT /reconciliations/{id}`
Updates metadata fields. Cannot update `status` directly — use the complete/revert endpoints.

**Field locking on COMPLETED status:** Once `status = 2`, the following fields are frozen: `beginning_balance_cents`, `ending_balance_cents`, `date_start`, `date_end`. Any attempt to edit them returns `422 VALIDATION_ERROR` with a `fields` map naming each attempted locked key (`"Locked while reconciliation is completed."`). To edit these fields, call `POST /reconciliations/{id}/revert` first. `name` stays editable on completed batches so archived reconciliations can be re-labelled.

### `POST /reconciliations/{id}/complete`
Marks the reconciliation as complete (`status = 2`). From this point, the four locked fields (`amount_cents`, `account_id`, `title`, `date`) become read-only on all assigned transactions, and the reconciliation's own balance/date fields are locked (see `PUT` above).

**Atomicity:** the handler locks every assigned transaction with `SELECT ... FOR UPDATE` before flipping the status, bumps `version + updated_at` on each one, and writes the `activity_log` entry — all inside the same DB transaction. Concurrent transaction edits serialize behind the status flip, and delta-sync clients see the transaction-lock state change on the same tick as the reconciliation status.

**Validation:** Returns `422` if no transactions are assigned to the batch.

### `POST /reconciliations/{id}/revert`
Reverts status to draft (`status = 1`). Unlocks all fields on assigned transactions, including the reconciliation's own balance/date fields. Same atomicity guarantees as `complete`: assigned transactions are locked with `FOR UPDATE`, versions bumped, status flipped — all in one DB transaction.

### `DELETE /reconciliations/{id}`
Soft-delete. Only allowed if `status = 1` (draft). Returns `409` if status is completed — revert first. Cascade-unassigns every transaction that was linked to this batch (`reconciliation_id` set back to `null` with `version + updated_at` bumps).

### `POST /reconciliations/{id}/restore`
Undoes a soft-delete on the reconciliation row. The transactions that were unassigned during delete are NOT re-linked — they may have since been assigned elsewhere or edited in ways that break the original balance assumptions. The restored reconciliation comes back empty and the user re-assigns manually. Returns `404` if no soft-deleted reconciliation with that id exists.

---

## Sync

### `GET /sync`

Delta sync endpoint. Returns every record that has changed since the client's last sync, plus tombstones for soft-deleted records. Single call gives the client everything it needs to bring its local replica up to date.

**Headers:**
- `Authorization: Bearer <jwt>` — required (standard).
- `X-Client-Id: <uuid>` — **required**. A stable UUID per device/install, generated client-side on first launch and persisted (Keychain on iOS, localStorage on web, config file on CLI). Each `(user_id, client_id)` pair has its own checkpoint, so multi-device sync is independent — device A's sync doesn't affect device B's bookmark. Mirrors the `X-Idempotency-Key` pattern.

**Query params:**
- `sync_token=*` — full fetch. Returns every non-deleted record for the user, no tombstones, and creates a fresh checkpoint. First-launch behavior.
- `sync_token=<uuid>` — delta fetch. Returns only records whose `updated_at` is newer than the checkpoint, including soft-deleted rows as tombstones.
- `debit_as_negative=true` (optional) — applies to every `transactions[]` row and every `inbox[]` row in the response, using the same semantics as the single-resource endpoints. Reconciliation balances don't negate (they're signed balances, not directional amounts). Account rows are unaffected.

The token is opaque to the client — never parse it, just store it and send it back. Server-side the token maps to a `last_sync_at` timestamp in `sync_checkpoints`; the delta query is `WHERE updated_at > last_sync_at`. All reads and the checkpoint write happen inside one Postgres `REPEATABLE READ` transaction so the snapshot is consistent across every table — a concurrent mutation either lands entirely in this sync or entirely in the next.

**Response shape (always 8 top-level keys, null-over-omission):**
```json
{
  "sync_token": "<new_opaque_uuid>",
  "accounts": [ /* expense_bank_accounts rows */ ],
  "categories": [ /* expense_categories rows */ ],
  "hashtags": [ /* expense_hashtags rows */ ],
  "inbox": [ /* expense_transaction_inbox rows */ ],
  "transactions": [ /* expense_transactions rows with hashtag_ids */ ],
  "reconciliations": [ /* expense_reconciliations rows */ ],
  "settings": { /* user_settings singleton, or null on delta if unchanged */ }
}
```

**Row shapes:** every row matches the same schema returned by the resource's individual list/get endpoints — `version`, `updated_at`, `deleted_at`, and all native + home-currency fields. `accounts` rows return `current_balance_home_cents: null` in sync responses; clients that need home-currency balances call `/dashboard`, which is the canonical place for derived values. `inbox` rows include `amount_home_cents` / `transfer_amount_home_cents` computed from the stored `exchange_rate`. `reconciliations` rows include `beginning_balance_home_cents` / `ending_balance_home_cents` computed from the account's currency at `date_end` (resolved via a deduplicated batched rate lookup).

**Transactions and `hashtag_ids`:** every transaction row in the sync response includes a `hashtag_ids: [uuid, ...]` array (sorted ascending) listing every hashtag attached to that transaction. The junction table `expense_transaction_hashtags` is internal storage and never appears on the wire. When a hashtag is added or removed from a transaction — even with no other field change — the parent transaction's `version` and `updated_at` are bumped in the same DB transaction, so the next delta sync surfaces the change.

**Tombstones:** soft-deleted rows (`deleted_at IS NOT NULL`) appear in delta responses as full row payloads with `deleted_at` set. Client treats any row with non-null `deleted_at` as instruction to remove it from local state. Wildcard fetches never return tombstones (the client has never seen those rows so there's nothing to delete locally).

**`settings`:** the `user_settings` singleton appears as an object on wildcard fetches and on deltas where settings have changed since the checkpoint. On a delta where settings are unchanged, the field is `null`.

**Errors (standard error envelope):**
- `401` — missing or invalid JWT.
- `422 VALIDATION_ERROR` — missing or non-UUID `X-Client-Id` header, or missing `sync_token` query param.
- `422 VALIDATION_ERROR` — `sync_token` is neither `*` nor a known token for this `(user_id, client_id)` pair. Client must retry with `sync_token=*` to recover.

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
          "hashtag_ids": ["<lunch_id>", "<work_id>"],
          "spent_cents": 30000,
          "spent_home_cents": 30000
        },
        {
          "hashtag_ids": ["<groceries_id>"],
          "spent_cents": 15000,
          "spent_home_cents": 15000
        },
        {
          "hashtag_ids": [],
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
- **`hashtag_breakdown`** — array of `{ hashtag_ids, spent_cents, spent_home_cents }` rows. Aggregation is `GROUP BY (category_id, sorted_array_of_hashtag_ids)`. The hashtag set is sorted by `id` before grouping so `[#a, #b]` and `[#b, #a]` collapse to the same row. Transactions with no hashtags appear as a row with `hashtag_ids: []`. **The sum of all `hashtag_breakdown` rows under a category equals that category's `spent_cents` exactly** — no double-counting, no orphaned amounts.
- `totals.inflow_cents` / `outflow_cents` are the sum of all positive and negative transactions in the current month, expressed in `main_currency`. Native-currency totals are not meaningful when accounts span currencies, so only `_home_cents` is authoritative; the non-home fields are provided for single-currency users.
- All `*_home_cents` fields are pre-converted by the engine. Clients never compute currency conversions.
- `bank_accounts[].current_balance_home_cents` and `people[].current_balance_home_cents` are `Optional[int]`. They are always populated for same-currency accounts (identity rate). For cross-currency accounts, they are `null` only when no exchange rate is available from the account's currency to `main_currency` for today's date — in that case, clients should display the native balance as a fallback.
- "Current month" means `[first_day_of_month, last_day_of_month]` in the user's `display_timezone`.
- `?debit_as_negative=true` is accepted for API consistency with other read endpoints but is a no-op here — dashboard aggregates are already signed by construction (per-category `spent_cents` is positive for income and negative for expense; totals return split positive `inflow_cents`/`outflow_cents` with `net_cents` as their difference).

**`?include_archived=true`** — when set, the response additionally includes three "archived" panels alongside the active ones:

```json
{
  "archived_accounts": [
    { "id": "...", "name": "Old BCP Soles", "currency_code": "PEN",
      "current_balance_cents": 0, "current_balance_home_cents": 0 }
  ],
  "archived_categories": [
    { "id": "...", "name": "Crypto", "lifetime_spent_cents": -250000,
      "lifetime_spent_home_cents": -250000 }
  ],
  "archived_hashtags": [
    { "id": "...", "name": "#vacation-2024", "lifetime_spent_cents": -480000,
      "lifetime_spent_home_cents": -480000 }
  ]
}
```

When `include_archived=false` (the default), all three fields are returned as `null` (per the null-over-omission rule). Their semantics:

- `archived_accounts` — same row shape as `bank_accounts`. `is_person = true` is excluded; person accounts have no archive concept yet (deferred until the People API ships). `current_balance_cents` is the lifetime balance (no further transactions can land on archived rows in clients that respect the picker).
- `archived_categories` / `archived_hashtags` — `lifetime_spent_cents` follows the same signed convention as the month-scoped `spent_cents`: positive for income / transfer credits, negative for expenses / transfer debits. Sums every non-deleted transaction ever attributed to the row, with no date floor. A transaction with multiple hashtags counts once under each hashtag — totals across hashtags do NOT sum to the global flow total, by design (each hashtag's lifetime view is independent).

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
        { "hashtag_ids": ["<lunch_id>", "<work_id>"], "spent_cents": 30000, "spent_home_cents": 30000 },
        { "hashtag_ids": ["<groceries_id>"], "spent_cents": 15000, "spent_home_cents": 15000 },
        { "hashtag_ids": [], "spent_cents": 5000, "spent_home_cents": 5000 }
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

`categories` and `hashtag_breakdown` follow the exact same rules as `/dashboard` (every non-deleted category included, breakdown rows sum to the parent category total, `hashtag_ids: []` for transactions with no hashtags). `totals` uses the same inflow/outflow/net structure.

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
Returns the activity log for the user. Supports filtering by `resource_type` (string) and `resource_id` (**UUID**). Sorted by `created_at` descending. Useful for debugging and audit.

**Validation:** `resource_id` is typed as UUID — non-UUID values return `422 VALIDATION_ERROR` before the query runs.

**Response fields:** each activity row includes `id`, `user_id`, `resource_type`, `resource_id`, `action`, `before_snapshot`, `after_snapshot`, `changed_by` (the user-id anchor), `actor_type`, and `created_at`. `actor_type` separates the performer of the mutation from the resource owner — values are `"user"` (default), `"system"` (cron-driven writes such as scheduled rate refreshes), and `"admin"` (reserved for future back-office flows). Pair `changed_by` with `actor_type` to resolve attribution.

### Action codes
| Value | Name | Emitted when |
|---|---|---|
| 1 | `CREATED` | Any resource is inserted |
| 2 | `UPDATED` | Any mutable field on an existing resource changes |
| 3 | `DELETED` | A resource is soft-deleted (`deleted_at` set) |
| 4 | `RESTORED` | A soft-deleted resource is restored via `POST /{resource}/{id}/restore` |

### Aggregate exceptions

The "every mutation gets an activity_log row" rule has three deliberate exceptions. Each is documented where the mutation happens so future readers can trace the decision:

1. **Junction-row mutations on `expense_transaction_hashtags`** are NOT logged per-link. The parent transaction's `UPDATED` snapshot carries the new `hashtag_ids` list, so the change is captured at parent granularity. Per-link entries would multiply audit row count by the average hashtags per transaction without answering useful questions.
2. **`recalculate_home_currency` bulk UPDATEs** on `expense_transactions` and `expense_transaction_inbox` are NOT logged per-row. A single `UPDATED` row on `user_settings` carries a `recalculation` summary block (rows touched per pass) and is the canonical audit record. Per-row entries would inflate `activity_log` by orders of magnitude for a single user request.
3. **`users.last_login_at` bumps** on repeat bootstrap calls are NOT logged. Login bumps are operational metadata, not user actions worth auditing. If session-level audit becomes a requirement, the right home is a dedicated `auth_sessions` table, not inflated `activity_log`.

---

## Exchange Rates

### `GET /exchange-rates`
**Query params:** `base` (default `USD`), `target`, `date` (ISO date, default today)

Returns the rate for the given pair and date. Falls back to the most recent available rate if no exact match exists for the requested date.

Used internally by the engine. Also exposed for CLI use.
