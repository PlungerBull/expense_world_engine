# Expense Engine — API Spec

> The `expense_world_engine` is the Brain. This document defines every endpoint, every business logic rule, and every validation the engine enforces. Nothing exists for any client unless it is defined here first.
>
> Schema: `schema-reference.md` | Conventions: `../CLAUDE.md`

---

## Base Conventions

**Base URL:** `http://127.0.0.1:8000/v1` (local profile — active since 2026-07-30) / `https://expense-world-engine.onrender.com/v1` (cloud profile, mothballed; see `deploy/cloud/README.md`)

**Authentication:** Every request requires `Authorization: Bearer ewe_pat_…` — an engine-issued Personal Access Token, looked up by SHA-256 hash. PATs are the **only** auth mechanism: the JWT branch was deleted 2026-08-03 (it verified tokens against a placeholder secret committed in `.env.example` — see `CLAUDE.md`, "Auth on every route"). Unauthenticated, invalid, or revoked tokens return `401`.

**Client-supplied UUIDs:** Every `POST` that creates a resource requires an `id: UUID` field in the request body. The client generates the UUID locally (e.g., `uuid4()`) before making the call — the server never picks the id. This enables offline-first clients to reference a resource before the request completes, and makes idempotent retries trivial: a second POST with the same `id` returns `409 CONFLICT` (existing resource), not a duplicate.

**Idempotency:** Write operations (`POST`, `PUT`, `DELETE`) should include `X-Idempotency-Key: <uuid>`. The engine records `(user_id, key) → (response_body, response_status, request_hash)` in `idempotency_keys` and acquires a transaction-scoped advisory lock on every incoming request to serialize concurrent retries with the same key at the DB. Duplicate requests return the stored response **verbatim, including the original HTTP status code** — no per-route drift. Keys are **permanent** (`sql/026`): there is no TTL, no purge, and a replay works identically a year later. Replay requires the *same request* — each key stores a fingerprint (sha256 over method, path, query string, raw body), and reusing a key with a different request returns `409 CONFLICT` instead of the unrelated snapshot. One exception: `POST /auth/pat` responses are never snapshotted (they carry the one-time plaintext token); replaying that key returns `409 CONFLICT`.

**Sign convention — requests:** `amount_cents` in request bodies uses a signed convention. The engine infers `transaction_type` from the sign — the caller never fills it in manually. Negative = expense/outflow (subtracts from balance). Positive = income/inflow (adds to balance). Transfers are identified by the presence of a `transfer` field in the request body, not by sign.

**Sign convention — storage:** Internally, `amount_cents` is always stored as a positive integer (`CHECK (amount_cents > 0)`). `transaction_type` (1=outflow, 2=inflow, `CHECK (transaction_type IN (1, 2))` — both `sql/020`) is set by the engine from the inferred direction, on **every** row. There is no transfer type: a transfer is two ordinary rows paired by `transfer_transaction_id`, and that FK is the only discriminator. Callers never interact with `transaction_type` on writes.

This holds on **every** amount-bearing column in **every** table, including the inbox's `transfer_amount_cents` — no column's sign carries meaning anywhere in the engine. On an inbox transfer draft, `transaction_type` describes the primary leg (the inbox row itself) and the sibling's direction is its inverse. ⚠️ The inbox was the one exception until 2026-08-03: it had transfer columns but no direction column, so the sign was load-bearing (audit WP7.2, `sql/019`; the direction column itself was then folded into `transaction_type` by `sql/020`).

**Sign convention — responses:** `amount_cents` in responses is always positive. `transaction_type` tells the client the direction. Pass `?debit_as_negative=true` on any amount-bearing read endpoint to receive negative amounts for expenses and outflows — useful for clients that prefer signed display. Supported on: `/transactions` list + detail, `/inbox` list + detail, `/reconciliations/{id}`. On an inbox transfer row the flag negates *both* legs, in opposite directions — an inbox row carries the sibling amount too, and a transfer whose two amounts point the same way is nonsense. Accepted but a no-op on `/dashboard` and `/reports/monthly`, whose aggregates are already signed by construction (category spent is positive for income and negative for expense; totals return split positive inflow/outflow).

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

**`fields` semantics:** On `VALIDATION_ERROR` responses, `fields` is always an object (possibly empty) — never `null`. Clients can uniformly iterate `Object.keys(error.fields)` without a null check. One precondition-unmet code also carries a field-scoped payload: `SETTINGS_MISSING` (`fields: {"user_settings": ...}`), returned as `422`. (The former `RATE_UNAVAILABLE` code is retired — no write path performs a rate lookup since `sql/021`.) On other non-validation errors (`UNAUTHORIZED`, `NOT_FOUND`, `FORBIDDEN`, `CONFLICT`, `INTERNAL_ERROR`), `fields` is `null` — those errors aren't field-scoped. The envelope key is still present in every response.

**Global exception coverage:** Four handlers are registered: `AppError` (canonical raises from domain code), `RequestValidationError` (Pydantic), `StarletteHTTPException` (routing-level 404/405/413/415/429), and a catch-all `Exception` handler returning `500 INTERNAL_ERROR` after logging the traceback server-side. Tracebacks never leak to clients; every error response carries the canonical envelope.

**Pagination:** List endpoints accept `?limit=50&offset=0`. FastAPI rejects out-of-range values at the query layer (`limit` must be `[1, 200]`, `offset` must be `≥0`) with `422 VALIDATION_ERROR` before the handler runs. Response shape: `{items, total, limit, offset}`.

**Soft-deleted records:** Excluded from all list responses by default. Pass `?include_deleted=true` to include them.

**Restore semantics:** Every resource with a delete endpoint also exposes `POST /{resource}/{id}/restore`. Restores clear `deleted_at` and write a `RESTORED` activity log entry. See per-resource sections for collision rules (e.g., restoring a category whose name now collides with an active one returns `409`).

**Optimistic locking:** All mutable resources include a `version` field in responses, incremented on every update. Clients can use this for conflict detection.

**Unknown request fields:** Every request body rejects unknown fields with `422 VALIDATION_ERROR` — no endpoint silently drops input (fail-closed). Since 2026-08-06 this is enforced structurally: all request models inherit `schemas.StrictModel` (`extra="forbid"`), including nested request objects (`transfer` on transactions and inbox) — Pydantic config does not propagate into nested models, so a nested fragment must inherit the base itself. The error names the offending key in `fields`, nested keys as dotted paths (`transfer.bogus`, `transactions.0.bogus`). Per-endpoint "unknown fields 422" notes below call out cases where the rejection carries extra meaning (a deliberately-deleted field, a locked field); the rule itself is global.

**Datetime inputs:** All datetime fields in request bodies must be RFC 3339 with a timezone offset. Accepted: `2026-04-25T16:30:00Z`, `2026-04-25T16:30:00+00:00`, `2026-04-25T11:30:00-05:00`. Rejected with `422 VALIDATION_ERROR`: naive datetimes (`2026-04-25T16:30:00`, `2026-04-25 16:30`, `2026-04-25`). Clients are responsible for resolving the user's local timezone and emitting canonical RFC 3339 — the engine never guesses a timezone for unqualified input. Response datetimes are always emitted in UTC with a `Z` suffix.

---

## Build Phases (Engine)

| Phase | Endpoints | Goal |
|---|---|---|
| 1 | Auth bootstrap, Accounts, Categories, Inbox, Transactions (ledger), Hashtags | Core tracking — fully working expense logger |
| 2 | ~~Sync endpoints~~ *(built, then deleted 2026-08-06 — `sql/023`)*, Activity log reads, Dashboard + reporting | Reportable |
| 3 | Reconciliations | Bank statement matching |
| 4 | Transfers + People (`/` syntax, person accounts) | Debt tracking |
| 5 | CSV import, split transactions, Recurrence | Power features |

Shipped ahead of plan: `POST /transactions/batch` landed with Step 6 rather than Phase 5 — the CLI needed bulk historical entry before CSV import existed. Phase 5 retains the CSV layer on top of it. Transfers (Phase 4) also shipped early with Step 7; only the People API half of Phase 4 remains.

Each phase is verified via Swagger UI before any CLI or iOS code is written.

---

## Health Check

### `GET /health`
Infrastructure endpoint. Returns `200` if the engine is running. No authentication required. Not versioned under `/v1`.

**Response:** `{"status": "ok"}`

---

## Auth & User Bootstrap

### `POST /auth/bootstrap`
Creates the `users` and `user_settings` rows for the authenticated caller if they don't exist (idempotent upsert), and bumps `last_login_at` on every call. Returns the full user profile.

**Status code:** Returns `200`, not `201`. Bootstrap has upsert semantics — first call creates the rows, subsequent calls bump `last_login_at` on the existing rows. First-call and replay statuses are both 200.

**Request body:**
```json
{
  "display_name": "Alex",
  "timezone": "America/Lima"
}
```

Note: `/auth/bootstrap` does **not** take a client-supplied `id` — the `users.id` is always the authenticated token's `user_id`. Both request fields are required and unknown fields 422 (`extra="forbid"`); `timezone` must be a valid IANA zone (`422` otherwise — `helpers/validation.validate_timezone`).

**Response:** `user` object + `settings` object.

`user` fields: `id`, `display_name`, `last_login_at`, `created_at`, `updated_at`. (No `email` — the column was dropped in `sql/024`; identity lives with the auth provider.)

`settings` fields: `user_id`, `main_currency`, `display_timezone`, `version`, `created_at`, `updated_at`. (The six client-preference fields — theme, week start, sort preference, three sidebar flags — were dropped in `sql/024`; clients own their display preferences.)

**Business logic:**
- If `users` row already exists for this `user_id`, skip creation but update `last_login_at` and `updated_at` to `now()`.
- If `user_settings` row already exists, skip creation.
- Always returns current state regardless of whether rows were created.

---

### `GET /auth/me`
Returns the authenticated user's profile and settings in a single response. Returns `404` if the user or settings row does not exist (edge case — should not occur after bootstrap).

### `PUT /auth/settings`
Updates `user_settings`. Partial update — only supplied fields are changed. If no fields are supplied, returns current settings without making changes. Every successful update bumps `version` and `updated_at`. The only updatable field is `display_timezone` (validated as an IANA zone, `422` otherwise); unknown fields 422 (`extra="forbid"`).

**`main_currency` is not updatable (since 2026-08-01).** The home currency is locked to **PEN** by `sql/018` (`CHECK (main_currency = 'PEN')`). Supplying `main_currency` in the request body — *even set to its current value* — returns `422 VALIDATION_ERROR` with `fields: {"main_currency": "The home currency is locked to PEN and is not updatable."}`.

The field is deliberately still declared on `SettingsUpdateRequest` so it can be rejected with a message that names the real rule. The schema is `extra="forbid"`, so dropping the field would still 422 — but with a generic unknown-field message, and a client would not learn that the currency is locked rather than misspelled. Note: an explicit `"main_currency": null` (or `"display_timezone": null`) fails the shared null-check first and returns `"Must not be null."` instead of the locked-currency message.

Other behaviors: a missing `user_settings` row returns `404` (bootstrap first); an empty-string timezone 422s with `"Must not be empty."`; every successful update writes an `UPDATED` activity entry under `resource_type = "user_settings"` with before/after snapshots.

Consequently there is **no home-currency recalculation pass**. `app/helpers/recalculate_home_currency.py` was deleted on 2026-08-01 (audit finding WP1.1 — it carried a silent `1.0` rate fallback), and `sql/021` then removed the stored values it recalculated. Since conversion happens at read time, unlocking `main_currency` someday needs only the `sql/018` CHECK lifted — every conversion would follow the new target on the next read, with no backfill.

**Response shape:** the full `user_settings` row. The former `recalculation` field is **removed** — not nulled — because the operation it summarised cannot occur.

**Settings preconditions:** Endpoints that read `user_settings` (dashboard, reports, transfers) return `422 SETTINGS_MISSING` with `fields: {"user_settings": "Must be provisioned via POST /v1/auth/bootstrap."}` if the user has not completed bootstrap. This is a precondition-unmet state, not a conflict.

**No exchange-rate preconditions on writes:** since `sql/021`, **no write path performs a rate lookup at all** — recording what happened is never blocked by a stale FX table. Conversion happens at read time, only on cross-currency aggregates; a row whose date has no resolvable rate surfaces there as `null` plus a non-zero `unconverted_count` (see Dashboard & Reporting). The former `422 RATE_UNAVAILABLE` write precondition is retired with the stored `exchange_rate`/`amount_home_cents` columns it protected.

### `PUT /auth/profile`
Partial update of identity fields on the `users` row. This is the single post-bootstrap path for changing `display_name` — `POST /auth/bootstrap` sets `display_name` only on the first call and never overwrites it on subsequent logins, and `PUT /auth/settings` mutates `user_settings`, not `users`.

**Request body:**
```json
{ "display_name": "Alex" }
```

All fields are optional; the endpoint is forward-compatible so future identity fields (e.g. `avatar_url`) can be added to `ProfileUpdateRequest` without changing the route or helper.

**Response (`200 OK`):** `UserResponse` shape — `id`, `display_name`, `last_login_at`, `created_at`, `updated_at`.

**Business logic:**
- Empty body → `422 VALIDATION_ERROR` with `fields: {"display_name": "Pass at least one field to update."}`. Unlike `PUT /auth/settings` (which treats empty-as-fetch), profile has one mutable field in v1, so empty-body is a client bug. Fail-fast.
- Explicit `"display_name": null` → `422 VALIDATION_ERROR` with `fields: {"display_name": "Must not be null."}`. Clearing the display name is not part of v1 scope.
- Mutable in v1: `display_name` only. `id` and `last_login_at` are returned for context but are **read-only** — `last_login_at` is owned by the bootstrap flow.
- Only `updated_at` and the supplied fields are touched in the UPDATE. `last_login_at` is explicitly preserved so the profile endpoint cannot masquerade as a login event.

**Idempotency:** standard `X-Idempotency-Key` semantics — same key + same body returns the stored response verbatim; same key + a differing body returns `409 CONFLICT` (the request fingerprint, `sql/026`).

**Activity log:** one `UPDATED` entry under `resource_type = "user"`, `resource_id = user_id`, with full `UserResponse`-shaped before/after snapshots.

### `POST /auth/pat`
Mints a new Personal Access Token for the authenticated caller. Long-lived, non-expiring; used by clients that can't do interactive JWT refresh (CLI, scripts, scheduled jobs).

**Request body:**
```json
{ "name": "laptop" }
```
`name` is optional and nullable — a freeform label users can set to distinguish tokens in a future management UI. No length or character restrictions in v1.

**Response (`201 Created`):**
```json
{
  "id": "…uuid…",
  "user_id": "…uuid…",
  "token": "ewe_pat_<~43-char urlsafe-base64 suffix>",
  "token_prefix": "ewe_pat_a3f9",
  "name": "laptop",
  "created_at": "2026-04-21T15:04:05Z",
  "revoked_at": null
}
```

The plaintext `token` is returned **exactly once**. The engine stores only its SHA-256 hash; losing the plaintext means the user must mint a new token and revoke the old one. The `token_prefix` (first 12 chars) is kept in cleartext for display.

**Idempotency exception (`sql/026`):** this is the one route whose response is never snapshotted into `idempotency_keys` — a stored copy would keep the plaintext in the database forever now that keys are permanent. The key is still claimed (concurrent retries stay serialized and cannot double-mint), but a replay returns `409 CONFLICT`; the client mints a fresh token with a new key instead.

**Design notes:**
- **Unlimited tokens per user.** Each device/integration can carry its own token; revoking one doesn't affect others. Matches GitHub/Stripe.
- **Activity log:** a single `CREATED` entry under `resource_type = "personal_access_token"`. The snapshot records `id`, `token_prefix`, `name`, and timestamps — never the hash, never the plaintext.
- **Callable with an existing PAT.** Any active PAT can mint new PATs; the new row is scoped to the caller's `user_id`. (The first PAT for a user is minted out-of-band — see `deploy/local/README.md`.)

### `DELETE /auth/pat/{pat_id}`
Revokes (soft-deletes) an active PAT. The token stops authenticating on the very next request.

Returns `200` with the revoked row (including `revoked_at` set). Returns `404` if the id is unknown to the caller or the token is already revoked (soft-deleted rows are excluded from the lookup, matching the codebase's soft-delete convention).

A PAT may revoke itself — that's fine; the row lookup succeeds, the `UPDATE` runs, and the next request with that token returns `401`.

**Not shipped in v1:**
- `GET /auth/pat` (list endpoint). Defer until a web dashboard needs a management UI.
- `last_used_at` tracking. Dropped to avoid a per-request DB write on every authenticated call.

---

## Bank Accounts

### `GET /accounts`
Returns active bank accounts. Includes `is_person = false` accounts only. Use `?include_people=true` to include person virtual accounts. Use `?include_archived=true` to include archived accounts. Use `?include_deleted=true` to include soft-deleted accounts.

**Supports standard pagination** (`?limit=50&offset=0`, response `{items, total, limit, offset}` — see Base Conventions). This endpoint is paginated like every other list: a client that ignores `limit` receives the first 50 accounts and a `total`, not the whole set. Read `total` and page rather than assuming one call returns everything — person accounts are the side that realistically grows past the default.

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

### `POST /accounts/{id}/opening-balance`
Seeds the account's opening balance as a transaction under the `@Opening` system category (`system_key = 'opening_balance'`, auto-created on first use). The seed is an **ordinary transaction** — editable and deletable via the transactions API — whose only special property is its category: flow reports exclude it entirely (see Dashboard & Reporting below). An opening balance is where tracking starts, not money that moved.

Since `sql/022` it is also the **first term of the account's balance**, which is a sum over the ledger rather than a stored figure. Under the old cached column a wrong or missing opening seed could hide behind the cache; now the account is wrong by exactly that amount, forever, on every screen. The invariant did not change — it became honest.

**Required:** `transaction_id` (client-supplied UUID), `amount_cents` (signed; positive = money you had, negative = starting debt), `date` (RFC 3339, must not be in the future)
**Optional:** `title` (defaults to `"Opening balance"`)
**Forbidden:** any unknown field → `422 VALIDATION_ERROR`.

**Validation:**
- Account must be active and non-archived (`422`, same rule as transaction creation) and must be a real account — person accounts return `422` (`@Debt` is their domain).
- `amount_cents` must not be zero; `date` must not be in the future (`422`).
- **At most one active opening balance per account** — a second POST returns `409 CONFLICT`. To adjust an opening balance, edit or delete the existing seed transaction instead.
- A `transaction_id` that already exists returns `409 CONFLICT`. Client-supplied ids make bulk-import re-runs deterministic: a replayed seed collides and is skipped, never double-applied.

**Response:** `201` with the standard transaction row. Standard `X-Idempotency-Key` semantics apply.

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
Returns all active categories, sorted by `sort_order`. System categories (`is_system = true`) are always included and always appear first. Supports standard pagination. Use `?include_deleted=true` to include soft-deleted categories. (Category archiving was deleted in `sql/024` — soft delete already hides a row from pickers while leaving its historical transactions intact, which is what archiving was for. Accounts keep their archive; see Bank Accounts.)

### `POST /categories`
**Required:** `id` (client-supplied UUID), `name`, `color`
**Optional:** `sort_order`

**Name normalization:** `name` is trimmed before storage. An empty-after-trim name returns `422 VALIDATION_ERROR` with `fields: {"name": "Must not be empty."}`. Uniqueness is **case-insensitive** per user: "Food", "food", and "FOOD" collide. A conflicting name returns `409 CONFLICT`. The database enforces this with a partial unique index on `(user_id, LOWER(name)) WHERE deleted_at IS NULL`, so deleting a category and creating a new one with the same name works as expected.

**Reserved names:** the system-category display names (`@Debt`, `@Transfer`, `@Opening` — derived from `SYSTEM_CATEGORY_DEFAULT_NAMES`, compared case-insensitively) cannot be claimed by a user category. Attempting to returns `422 VALIDATION_ERROR` with `fields: {"name": "… is reserved for system categories."}`. Without this, a user category squatting the name would make every later system-category seed hit the `LOWER(name)` unique index — which the seed's `ON CONFLICT (user_id, system_key)` arbiter does not cover — permanently 500ing transfers or opening balances (closed bug 7.4). As defense in depth for rows created before this check existed, the seeding INSERT itself catches the violation and returns a clean `409` naming the remedy (rename the squatting category).

Categories carry no type restriction. The same category can be used on expenses, income, and transfers — including refunds (same category as the original expense, positive amount).

**Auto-creation (engine-side, not via this endpoint):**
- `@Debt` — auto-created the first time a person account is involved in a transaction.
- `@Transfer` — auto-created the first time a real-account transfer is created.
- `@Opening` — auto-created the first time an account's opening balance is seeded via `POST /accounts/{id}/opening-balance`.
All are created with `is_system = true` and a stable `system_key` column (`"debt"` / `"transfer"` / `"opening_balance"`) — the engine looks them up by `system_key`, not by display name. This means users can freely rename the display text without breaking the pipelines that depend on them (which was a bug before the `system_key` column was added).

Category responses include `system_key` (`null` for user categories) — since 2026-08-07. It is the identity the rename-safety guarantee keys off, so clients get it too; without it a client wanting to label a specific system row had to string-match a renameable display name. Not an IDs-only violation: `system_key` is an immutable discriminator, not a hydrated copy of a mutable value.

### `PUT /categories/{id}`
System categories (`is_system = true`) CAN be renamed — the engine identifies them by `system_key`, not by `name`. Any other field is also editable. Returns `404` if the category is missing. The same name normalization rules as `POST` apply: renames are trimmed, empty names return `422`, and case-insensitive conflicts return `409`. The reserved-name rule applies to renames of **non-system** categories only: a system row may take any name, including its own default back; a user row renamed to `@Debt`/`@Transfer`/`@Opening` (any casing) returns `422`.

### `DELETE /categories/{id}`
Soft-delete. Returns `409` if the category is referenced by any non-deleted transaction (inbox or ledger). System categories (`is_system = true`) always return `403` — they must remain available for the transfer pipeline.

### `POST /categories/{id}/restore`
Undoes a soft-delete. Returns `404` if no soft-deleted category with that id exists. Returns `409` if an active category already uses the same name (the name collision check prevents silent duplicates). Writes a `RESTORED` activity log entry.

*(The `POST /categories/{id}/archive` / `/unarchive` routes and the archived-category attach guards were deleted with the `is_archived` column in `sql/024`. The soft-delete guards — `category_id` must reference an **active** category on every attach path — are untouched. Archived **accounts** still block attachment; see the account rules under Transactions and Inbox.)*

---

## Hashtags

### `GET /hashtags`
Returns all active hashtags, sorted by `sort_order`. Supports standard pagination. Use `?include_deleted=true` to include soft-deleted hashtags. (Hashtag archiving was deleted in `sql/024`, same rationale as categories.)

### `POST /hashtags`
**Required:** `id` (client-supplied UUID), `name`
**Optional:** `sort_order`

**Name normalization:** `name` is trimmed before storage. An empty-after-trim name returns `422 VALIDATION_ERROR`. Uniqueness is **case-insensitive** per user and scoped to non-deleted rows via a partial unique index on `(user_id, LOWER(name)) WHERE deleted_at IS NULL`. A conflicting name returns `409 CONFLICT`.

### `PUT /hashtags/{id}`
The same name normalization rules as `POST` apply to renames.
### `DELETE /hashtags/{id}`
Soft-delete. Cascades: soft-deletes all `expense_transaction_hashtags` junction rows for this hashtag and bumps each affected parent transaction's `version + updated_at` — a hashtag change is an edit to the transaction as clients see it (the embedded `hashtag_ids` array), so the parent's optimistic version must move. Writes a single `DELETED` activity log entry for the hashtag itself; per-junction-row entries are deliberately NOT written (see "Activity log aggregate exceptions" below).

### `POST /hashtags/{id}/restore`
Undoes a soft-delete of the hashtag row itself. Does NOT automatically restore the cascaded junction rows — the restored hashtag comes back as an empty label that the user can re-apply manually to transactions. Silently re-tagging could surprise users. Returns `404` if no soft-deleted hashtag with that id exists. Returns `409` if an active hashtag already uses the same name.

*(The `POST /hashtags/{id}/archive` / `/unarchive` routes and their attach guard were deleted with the `is_archived` column in `sql/024`. The soft-delete guard on `hashtag_ids` — every id must reference an active hashtag — is untouched.)*

---

## Inbox

### `GET /inbox`
Returns all active inbox items (`status = 1`, `deleted_at IS NULL`).

Optional filters: `?ready=true` (only items ready to promote — all required fields present and `date ≤ now()`), `?overdue=true` (items with `date` in the past).

`?ready=true` is the exact complement of the promote validation below: every row it returns promotes, and every row that promotes appears in it. In particular transfer items are **included without a `category_id`** — promote auto-assigns `@Transfer`/`@Debt` and never reads the field — and are **excluded** when their `transfer_account_id` points at a deleted or archived account, which promote rejects.

### `POST /inbox`
Creates a new inbox item.

**Required:** `id` (client-supplied UUID). All other fields are optional — the engine accepts sparse inbox rows and waits for later edits to complete them.

`amount_cents` follows the standard sign convention: negative = expense, positive = income. The engine infers `transaction_type` from the sign and stores `amount_cents` as positive (same as the ledger). `transaction_type` is stored on the inbox row so direction is preserved through to promotion.

**The `transfer` object** marks the item as a transfer draft. It takes two fields:

```json
"transfer": { "account_id": "<uuid>", "amount_cents": -5000 }
```

Unlike `POST /transactions`, it takes **no `id`** — that field is the sibling *ledger row's* UUID, and no ledger rows exist until promotion. The sibling's id is supplied later, as `transfer_id` on the promote call.

`transfer.amount_cents` is signed and must point the opposite way to the item's own `amount_cents`; a same-sign pair returns `422` on `transfer.amount_cents` — the same rule §Transfers → *Zero-sum validation* applies to `POST /transactions`, enforced here at draft time rather than deferred to promotion. When the item has no `amount_cents` yet, the sibling's sign alone determines direction.

Both amounts are stored **positive**. Direction lives on `transaction_type` (1=outflow, 2=inflow), which describes the **primary** leg — the inbox row itself — exactly as it does on `expense_transactions`. The sibling's direction is its inverse and is never stored. Supplying the item's `amount_cents` in a later `PUT` restates the primary's sign and flips both legs.

Send `"transfer": null` on `PUT /inbox/{id}` to clear it: the transfer columns are nulled and the item stays an ordinary outflow or inflow per its amount. This is the only field on an inbox item that accepts an explicit null.

**Response shape:** native currency only, exactly as on a ledger row — `amount_cents` and `transfer_amount_cents`, both positive, with `transaction_type` carrying the primary leg's direction. (The old response computed home-currency values from a stored `exchange_rate` whose `DEFAULT 1.0` was bug 1.4 — a $100 draft promoted as 100 PEN cents. Both columns died in `sql/021`.)

Pass `?debit_as_negative=true` on `GET /inbox` or `GET /inbox/{id}` to have amounts returned negated for the outflow side. For outflow items (`transaction_type = 1`) the primary `amount_cents` is negated. On a transfer the sibling is negated in the **opposite** direction — the two legs of a transfer never point the same way.

### `GET /inbox/{id}`
### `PUT /inbox/{id}`
Partial update. Re-evaluates promotion readiness after every update. Date and account changes need no rate handling — nothing on the row stores a conversion.

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
- `transfer_id` — the client-supplied UUID for the paired sibling ledger row when promoting a transfer inbox item. Required when the inbox row carries transfer fields; must be `null` (or omitted) otherwise. Returns `422` in **both** directions — missing when required, and present when not. (The two transfer columns are all-present-or-all-absent by database constraint, so either one identifies a transfer row.) Must differ from `id` — the same UUID for both legs is a `422` on `transfer.id`.

**Validation (engine enforces, not the client):**
- `title` is present and not `'UNTITLED'`
- `amount_cents` is present and not zero
- `date` is present and `≤ now()`
- `account_id` is present and references an active, non-archived account
- `category_id` is present and references an active category (non-transfer items only — transfer items auto-assign the system category)
- `transfer_account_id` references an active, non-archived account (transfer items only) — reported on `transfer.account_id`
- `transfer_id` is present for a transfer item and absent for a non-transfer one, and must differ from `id` (reported on `transfer_id`)

If any condition fails, returns `422` with **all** the failing fields, not just the first. (One edge check sits outside the accumulation and surfaces on its own: a non-transfer row with an amount but a null `transaction_type` — out-of-band data only, unreachable through the API. The transfer-engine checks `transfer.account_id ≠ account_id` and `transfer.id ≠ id` are accumulated like everything else since 2026-08-07.)

**Other statuses:** `200` on success (promote is not a pure create — the inbox row already existed). `404` when the inbox row is missing, already promoted, or soft-deleted. `409 CONFLICT` when `id` (or `transfer_id`) already exists in the ledger.

**On success (atomic):**
1. Creates `expense_transactions` row(s) using the client-supplied `id` (and `transfer_id` for the sibling). `inbox_id` on **both** legs points back to this inbox item — the draft produced the pair, so lineage is a fact about both rows. *(Amended 2026-08-07: previously only the primary leg carried the backlink; rows promoted before then keep a null sibling `inbox_id`.)* Copies `transaction_type` from the inbox row; for transfers, the sibling takes its inverse. Both legs share the draft's title, description, and date; `cleared` starts `false`.
2. Sets `status = 2` (promoted) on the inbox row.
3. Sets `deleted_at` on the inbox row (soft delete).
4. Writes `activity_log` entry (action=1 CREATED) for the new transaction(s).
5. Writes `activity_log` entry (action=3 DELETED) for the inbox item.

There is no balance step in this list, or in any other write flow below — an account's balance is the signed sum of its non-deleted transactions, computed at read time (`sql/022`), so inserting the row **is** the balance change. There is no rate step either — conversion happens at read time (`sql/021`).

`status = 2` distinguishes a promoted inbox item from a dismissed one (which stays at `status = 1` with `deleted_at` set) — both end up soft-deleted, but the reason is preserved via the status column. Only the PENDING + deleted combination is restorable via `POST /inbox/{id}/restore`.

Returns the newly created `expense_transactions` object (primary leg for transfer promotions).

---

## Transactions (Ledger)

**Hashtag wire format:** every transaction returned by any read endpoint includes a `hashtag_ids: [uuid, ...]` array (sorted ascending) listing every hashtag attached to it. This applies uniformly to `GET /transactions`, `GET /transactions/{id}`, the response body of `POST /transactions`, `PUT /transactions/{id}`, `DELETE /transactions/{id}`, `POST /transactions/{id}/restore`, `POST /transactions/batch`, `POST /inbox/{id}/promote`, and each embedded transaction inside `GET /reconciliations/{id}`. Transactions with no attached hashtags return `"hashtag_ids": []` (never `null`, never omitted). The junction table `expense_transaction_hashtags` is internal storage only — clients never see junction rows. Mutations to a transaction's hashtag set bump the parent transaction's `version` and `updated_at` in the same DB transaction — a hashtag change is an edit to the transaction as clients see it.

**Home-currency fields — absent, not `null`.** A transaction response carries **no** home-currency value: the row belongs to one account, the account governs the currency, and a per-row conversion would be a second copy of a number nothing on the row combines (`sql/021`). This is the documented exception to null-over-omission — a permanently-null key on every transaction forever is dead weight, so the key is absent rather than null. Conversion happens where currencies are combined: the dashboard and monthly report. (`parent_transaction_id` also left the payload when `sql/024` dropped the column; splits are Phase 5 and will re-add schema support when they ship.)

### `GET /transactions`
Returns all active ledger transactions. Supports filtering:
- `?account_id=` — filter by account
- `?category_id=` — filter by category
- `?hashtag_id=` — filter by hashtag
- `?reconciliation_id=` — filter to transactions assigned to one reconciliation batch. This is the standalone escape hatch referenced under `GET /reconciliations/{id}`, and unlike that endpoint's embedded window it supports the full filter surface below.
- `?date_from=` / `?date_to=` — date range (ISO 8601)
- `?cleared=true/false`
- `?search=` — full-text search across `title` and `description`

Standard `?include_deleted=true`, `?debit_as_negative=true`, and `?limit` / `?offset` also apply (see Base Conventions).

### `POST /transactions`
Creates a transaction directly in the ledger, bypassing the inbox. Used by the CLI for fast entry when all required fields are known.

**Required:** `id` (client-supplied UUID), `title`, `amount_cents`, `date`, `account_id`, `category_id` (required for normal transactions; omit for transfers — the engine auto-assigns `@Transfer`/`@Debt` and discards any `category_id` passed alongside a `transfer` object)
**Optional:** `description`, `cleared`, `hashtag_ids`, `transfer`
**Forbidden:** any unknown field → `422 VALIDATION_ERROR` (`extra="forbid"`). This is what makes the removal of `exchange_rate` (`sql/021`) visible to a caller still sending it — the engine no longer stores a rate anywhere, and a caller who believes the value matters deserves to be told it does not.

For transfer requests, the `transfer` object additionally requires its own `id` field — the UUID of the sibling ledger row. Both `id` and `transfer.id` must be distinct and client-generated. **This is the one field that differs from `POST /inbox`'s `transfer` object, which must omit it** — a draft creates no ledger rows, so there is no sibling to name until promotion. Example:

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

Returns `409 CONFLICT` if `id` or `transfer.id` already exists. No rate lookup, no conversion, no balance write — recording the row is the whole of the write.

**On success (atomic):**
1. Creates `expense_transactions` row.
2. Writes `activity_log` entry.

### `GET /transactions/{id}`
### `PUT /transactions/{id}`
Partial update.

**Updatable fields:** `title`, `amount_cents`, `date`, `account_id`, `category_id`, `description`, `cleared`, `hashtag_ids`, `reconciliation_id`. Every field is optional; omitted fields are left untouched. An empty body is a no-op that returns current state (no version bump, no activity entry). Unknown fields 422 (`extra="forbid"`). Explicit `null` is rejected with `422` `"Must not be null."` on every field **except** `reconciliation_id`, where `null` means unassign (see below) — clearing `description` or `cleared` via null is not supported.

The same value rules as `POST` apply: `amount_cents` must be non-zero, `title` non-empty after trim, `date` not in the future, `account_id` active and non-archived, `category_id` active, every `hashtag_ids` entry active.

**Reconciliation assignment:** `reconciliation_id` on this endpoint is the **only** way a transaction is assigned to or removed from a reconciliation batch — see *Assigning transactions* under Reconciliations below for the full rules.

**Field locking:** If the transaction belongs to a completed reconciliation (`reconciliation_id` is set and reconciliation `status = 2`), these fields are read-only: `amount_cents`, `account_id`, `title`, `date`. Attempting to update them returns `422`.

**Transfer edit guard:** If the transaction is part of a transfer pair (`transfer_transaction_id` is set), the guard is an **allow-list**: only `title`, `description`, `cleared`, `hashtag_ids` and `reconciliation_id` are editable per-leg (none has a cross-leg invariant — each leg clears and reconciles at its own bank, on its own account). Every other field returns `422`; a transfer must be deleted and re-created to change them. The PUT path mutates only the edited leg, so letting any paired field through would silently desync the pair: a one-sided `amount_cents` breaks the netting, `account_id` moves a leg to an account the pair was never between, a date mismatch makes `@Transfer` report a phantom spread in the month that has only one leg, and `category_id` strands the sibling in `@Transfer` with nothing to cancel against (blocked outright rather than mirrored — the legs legitimately hold *different* categories, `@Debt` on a person leg). Because it is an allow-list, any field added to the update schema later is blocked on transfer legs by default. (Closed bug 6.5; the guard was previously a deny-list that forgot `category_id`.)

**Date change:** needs no rate handling — nothing on the row stores a conversion (`sql/021`); read-time conversion always uses the row's current date.

**Balance:** nothing to update. Changing `amount_cents` changes what the row contributes; changing `account_id` moves that contribution from one account to the other. Both fall out of the single `UPDATE` on the transaction row.

### `DELETE /transactions/{id}`
Soft-delete. The balance sum excludes soft-deleted rows, so setting `deleted_at` is the reversal — there is no separate balance write.

**Response shape:** Always includes a `warnings: list[str]` field. Empty list when the delete is clean; populated with one or more strings when something notable happened. Currently the only warning emitted is `"Transaction belonged to a completed reconciliation. Reconciliation totals may be stale."` — the delete is still allowed (the engine does not auto-adjust the reconciliation's totals); the field surfaces the staleness so clients can render a notice.

If the transaction has a `transfer_transaction_id`, both the transaction and its paired sibling are soft-deleted atomically.

### `POST /transactions/{id}/restore`
Undoes a soft-delete on a transaction. Clearing `deleted_at` puts the row back into the balance sum, so the balance impact returns with no separate re-apply step. Also re-activates the cascaded hashtag junction rows, and atomically restores the transfer sibling if the row is part of a pair. Returns the restored transaction with the same `warnings: list[str]` envelope as DELETE (empty when restore is clean).

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
  "id": "<primary_uuid>",
  "title": "BCP to Chase",
  "amount_cents": -6000,
  "account_id": "<bcp_pen_id>",
  "category_id": "<other_category_id>",
  "date": "2024-03-15T00:00:00Z",
  "transfer": {
    "id": "<sibling_uuid>",
    "account_id": "<chase_usd_id>",
    "amount_cents": 1500
  }
}
```

⚠️ **The two endpoints take different `transfer` shapes.** On `POST /transactions` the object requires an `id` — the sibling ledger row's client-supplied UUID, since both rows are written immediately. On `POST /inbox` it must be **omitted**: no ledger rows exist yet and the sibling's id arrives later as `transfer_id` on the promote call. See `POST /inbox` above.

**Validation (all `422 VALIDATION_ERROR`, field-scoped, accumulated into one response):**
- **The two sides must be different accounts.** A `transfer.account_id` equal to the request's own `account_id` returns `fields: {"transfer.account_id": "Must be a different account."}` — a transfer to itself moves no money and would write two rows that cancel on one balance. Checked before either account is loaded, so it fires even for an account that doesn't exist; if the same id is also missing or archived, the existence message wins the field (the checks share one key).
- `transfer.amount_cents` must not be zero, and must carry the opposite sign to the primary `amount_cents` (see zero-sum validation below).
- `transfer.id` must differ from the request's own `id` — the same UUID for both legs returns `fields: {"transfer.id": "Must differ from the primary transaction id."}`.
- Both `account_id` values must reference the caller's own active, non-archived accounts.

**Business logic (atomic):**
1. Creates the primary transaction (the one in the request body).
2. Creates the paired transaction on `transfer.account_id` with `transfer.amount_cents`.
3. Links both via `transfer_transaction_id` (each row points to the other).
4. Auto-assigns categories: if either account `is_person = true`, that side gets `@Debt`; both real accounts get `@Transfer`. These override any `category_id` passed in the request.
5. Auto-creates `@Debt` or `@Transfer` system categories if they don't exist yet.
   - **Note:** The transfer engine does **not** auto-create person accounts. Both `account_id` values in the request must reference accounts that already exist and are non-archived. If `transfer.account_id` references a non-existent or archived person, the request returns `422 VALIDATION_ERROR`. Callers create person accounts explicitly via the People API before initiating a transfer to that person.
6. **Zero-sum validation:** The engine does not enforce that the two `amount_cents` values are equal in raw number — they may be in different currencies. It does enforce that the two transactions are directionally opposite (one negative, one positive). Returns `422` if both are the same sign. **Explicit decision:** No magnitude equality check is performed even when both accounts share the same currency. This keeps the logic simple and allows users to record unequal amounts intentionally (e.g., fees absorbed during transfer).

   **This is checked wherever both signs are supplied — including `POST`/`PUT /inbox`, before promotion.** A transfer draft is validated when it is written, not when it is promoted, because that is where the two signs still exist side by side; by promote time the row holds absolute amounts plus the primary's `transaction_type`, and the legs are re-signed from that column and cannot disagree. ⚠️ Until 2026-08-03 the inbox had no direction column and promotion *derived* the primary's sign from the sibling's, so this rule was unreachable for drafts and a same-sign pair was silently rewritten. Audit WP7.2; fixed in `sql/019`.
7. **Cross-currency transfers store no conversion.** Each leg records what its bank actually saw, in that account's native currency — nothing else is written (`sql/021`). At read time each leg converts independently by its date's rate, so the pair generally does **not** net to zero in home currency; the difference — the spread between the rate the user got and the reference rate — surfaces in the `@Transfer` category's report figures. That is deliberate (owner decision 2026-08-05): a non-zero `@Transfer` month figure *is* the cost of moving money. No separate `@FX` category is recognized — see `docs/currency-model-decision.md`, "Deferred: @FX".
8. Both account balances have moved by construction — each leg is an ordinary row on its own account, and the balance is a sum over rows.
9. Writes `activity_log` entries for both transactions.

---

## Reconciliations

### Assigning transactions

There is no `POST /reconciliations/{id}/transactions` endpoint. A transaction joins or leaves a batch through its **own** update endpoint:

```
PUT /transactions/{transaction_id}   { "reconciliation_id": "<recon_uuid>" }   ← assign
PUT /transactions/{transaction_id}   { "reconciliation_id": null }             ← unassign
```

The engine distinguishes *omitted* from *explicitly null*: leaving `reconciliation_id` out of the body preserves the current assignment, while sending it as `null` unassigns. This is why unassign has no dedicated route — `null` is a real value here, not a missing one.

**Validation (all `422 VALIDATION_ERROR`, field-scoped on `reconciliation_id`):**

| Condition | Message |
|---|---|
| Target reconciliation is missing, soft-deleted, or another user's | `"Must reference an active reconciliation."` |
| Reconciliation's `account_id` ≠ the transaction's account | `"Reconciliation account does not match transaction account."` |
| Target reconciliation is `status = 2` (completed) | `"Cannot assign transactions to a completed reconciliation."` |

The account check uses the transaction's *effective* account — if the same `PUT` also changes `account_id`, the new value is what must match. A batch and its transactions therefore always share one account.

Two behaviors of this path are **known state-machine gaps, not design** (bug 5.5 in `docs/open-bugs.md`): unassigning *away from* a completed reconciliation is currently allowed silently (the guard above only blocks assigning *into* one), and a `PUT` that supplies `reconciliation_id` bumps the transaction's `version` twice in one call (once for the column update, once for the assignment write).

Unassignment also happens implicitly in two places: `DELETE /reconciliations/{id}` cascade-unassigns every linked transaction, and `POST /transactions/{id}/restore` conditionally clears the link (see its table under Transactions).

### Ordering

The dates order the list. `GET /reconciliations?account_id=<id>` is sorted `date_start ASC NULLS LAST, created_at ASC` — undated rows sort last, and both dates stay nullable and clearable. Without `account_id` the list is `created_at DESC` (dates on different accounts are not comparable periods).

There is no user-controlled ordering: `sort_order`, the bulk reorder route, and reconciliation chaining were deleted in `sql/025`. The chaining cascade could rewrite a **completed** record's balances through a draft edit — the regression test that pins its absence is `tests/test_wp6_reconciliation_simplification.py` (editing one reconciliation leaves every other byte-identical, in draft and completed status).

### `difference_cents` — the add-up check

Every reconciliation response carries `difference_cents`, computed at read time and never stored (the same rule balances follow, `sql/022`):

```
difference_cents = (ending_balance_cents − beginning_balance_cents)
                   − SUM(signed amount of assigned non-deleted transactions)
```

The signed sum is projected in SQL via `home_currency.signed_expr` — no second rendering of the sign matrix. Zero means the batch adds up. Completing with a non-zero difference is **allowed**: the figure informs, the user decides. Native currency only — a reconciliation is scoped to one account, so there is nothing to convert.

### `GET /reconciliations`
Returns reconciliation batches for the user. Sorted `date_start ASC NULLS LAST, created_at ASC` when filtered by `account_id`; otherwise `created_at DESC`. Standard pagination via `limit` / `offset`.

### `POST /reconciliations`
Creates a new draft reconciliation batch.

**Required:** `id` (client-supplied UUID), `account_id`, `name`, `beginning_balance_cents`
**Optional:** `date_start`, `date_end`, `ending_balance_cents`
**Forbidden:** any unknown field → `422 VALIDATION_ERROR` (`extra="forbid"`). The deleted fields (`sort_order`, `beginning_balance_source`) 422 rather than vanish.

**`beginning_balance_cents` is required** — omitting it is a `422`, not an invitation to derive. A beginning balance is a fact the user reads off a statement; the engine never derives one (that was chaining, deleted in `sql/025`, deliberately superseding decision D3's one-time-prefill sketch).

**Response shape:** the reconciliation row — `id`, `user_id`, `account_id`, `name`, `date_start`, `date_end`, `status`, `beginning_balance_cents`, `ending_balance_cents`, `difference_cents`, `created_at`, `updated_at`, `version`, `deleted_at`. **Native currency only** — the former `beginning_balance_home_cents` / `ending_balance_home_cents` were the last per-account home values and were removed with `sql/021` (`docs/currency-model-decision.md` records the decision). Like transactions, the home fields are absent, not null.

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
Updates metadata fields: `name`, `date_start`, `date_end`, `beginning_balance_cents`, `ending_balance_cents`. Cannot update `status` — use the complete/revert endpoints. Unknown fields — including the deleted `sort_order` and `beginning_balance_source` — return `422 VALIDATION_ERROR` (`extra="forbid"`).

An empty body is a no-op returning current state (no version bump, no activity entry). `name` follows the standard normalization rules (trimmed; empty-after-trim `422`). `date_start` and `date_end` are the only fields that accept an explicit `null` (clearing a date); the balances and `name` reject null with `"Must not be null."`. Every successful update writes an `UPDATED` activity entry with before/after snapshots. The engine does not validate `date_start ≤ date_end` — the dates are labels the user reads off a statement.

**Field locking on COMPLETED status:** Once `status = 2`, four fields are frozen: `beginning_balance_cents`, `ending_balance_cents`, `date_start`, `date_end`. Any attempt to edit them returns `422 VALIDATION_ERROR` with a `fields` map naming each attempted locked key (`"Locked while reconciliation is completed."`). To edit these fields, call `POST /reconciliations/{id}/revert` first. `name` stays editable on completed batches so archived reconciliations can be re-labelled.

Editing a balance touches **only this row** — no other reconciliation is ever rewritten as a consequence (the chaining cascade is gone, `sql/025`). `difference_cents` on the next read reflects the new figures.

### `POST /reconciliations/{id}/complete`
Marks the reconciliation as complete (`status = 2`). From this point, the four locked fields (`amount_cents`, `account_id`, `title`, `date`) become read-only on all assigned transactions, and the reconciliation's own balance/date fields are locked (see `PUT` above).

**Atomicity:** the handler locks every assigned transaction with `SELECT ... FOR UPDATE` before flipping the status, bumps `version + updated_at` on each one, and writes the `activity_log` entry — all inside the same DB transaction. Concurrent transaction edits serialize behind the status flip, so the transaction-lock state and the reconciliation status change on the same tick.

**Validation:** Returns `422` with `fields: {"transactions": "At least one transaction must be assigned."}` if no non-deleted transactions are assigned to the batch. Returns `404` if the reconciliation is missing or soft-deleted. Calling on an already-completed batch is a silent no-op returning the current row (no activity entry) — and it short-circuits before the assignment check, so a completed batch whose transactions have since all been deleted still replays as `200`.

### `POST /reconciliations/{id}/revert`
Reverts status to draft (`status = 1`). Unlocks all fields on assigned transactions, including the reconciliation's own balance/date fields (both locks read the live status, so the flip is the unlock). Same atomicity guarantees as `complete`: assigned transactions are locked with `FOR UPDATE`, versions bumped, status flipped — all in one DB transaction. Restores only the status; nothing else on the row is rewritten. **Revert requires nothing** — the asymmetry with `complete` (which requires ≥1 assigned transaction) is deliberate: completing asserts a batch adds up, reverting merely withdraws that assertion. Returns `404` if missing or soft-deleted; reverting an already-draft batch is a silent no-op. Neither endpoint takes a request body; both honor `X-Idempotency-Key`.

### `DELETE /reconciliations/{id}`
Soft-delete. Only allowed if `status = 1` (draft). Returns `409` if status is completed — revert first. Cascade-unassigns every transaction that was linked to this batch (`reconciliation_id` set back to `null` with `version + updated_at` bumps).

### `POST /reconciliations/{id}/restore`
Undoes a soft-delete on the reconciliation row. The transactions that were unassigned during delete are NOT re-linked — they may have since been assigned elsewhere or edited in ways that break the original balance assumptions. The restored reconciliation comes back empty and the user re-assigns manually. Returns `404` if no soft-deleted reconciliation with that id exists.

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
      "spent_home_cents": -50000,
      "unconverted_count": 0,
      "hashtag_breakdown": [
        { "hashtag_ids": ["<lunch_id>", "<work_id>"], "spent_home_cents": -30000, "unconverted_count": 0 },
        { "hashtag_ids": ["<groceries_id>"], "spent_home_cents": -15000, "unconverted_count": 0 },
        { "hashtag_ids": [], "spent_home_cents": -5000, "unconverted_count": 0 }
      ]
    }
  ],
  "totals": {
    "inflow_home_cents": 800000,
    "outflow_home_cents": 320000,
    "net_home_cents": 480000,
    "unconverted_count": 0
  },
  "archived_accounts": null
}
```

**Field rules:**

- `bank_accounts` includes only `is_person = false`, `is_archived = false`, `deleted_at IS NULL`. Sorted by `sort_order`.
- `people` includes only `is_person = true`, `deleted_at IS NULL`. Same shape as `bank_accounts`, separated for client convenience. (Currently always `[]` — no endpoint can set `is_person`; see TODO.md.)
- `categories` includes every non-deleted category, even with nothing spent (so the client can render the full category list without a second call), **except the `@Opening` system row** (`system_key = 'opening_balance'`) — see the opening-balance rule below. Sorted by `sort_order`.
- **Aggregates are home-currency ONLY** — `spent_home_cents`, never a native `spent_cents`. `GROUP BY category_id` has no currency partition, so a category holding $15 and S/25 has no native total: `4000` would be a number in no currency at all. The only correct cross-account figures are converted ones (`CLAUDE.md`, Home currency).
- **Every aggregate is nullable and paired with `unconverted_count`** — the number of rows in the group whose date had no resolvable rate. A non-zero count makes the figure `null` rather than a partial total: `SUM` skips nulls, and the inflow/outflow `CASE` shape scores a null row as zero, so an unflagged aggregate would understate in silence.
- **`hashtag_breakdown`** — array of `{ hashtag_ids, spent_home_cents, unconverted_count }` rows. Aggregation is `GROUP BY (category_id, sorted_array_of_hashtag_ids)`. The hashtag set is sorted by `id` before grouping so `[#a, #b]` and `[#b, #a]` collapse to the same row. Transactions with no hashtags appear as a row with `hashtag_ids: []`. **The sum of all fully-converted `hashtag_breakdown` rows under a category equals that category's `spent_home_cents` exactly** — no double-counting, no orphaned amounts.
- **Opening balances are excluded from flow views entirely** (dashboard month panel and `/reports/monthly` alike): transactions under the `opening_balance` system category contribute nothing to `totals`, and the `@Opening` category row is omitted from `categories`. Rationale: an opening balance is where tracking starts, not money that moved — including it would report phantom income in the seed month. Exclusion keys off `system_key`, so renaming the category never breaks it. Account balances **do** include opening balances by construction, and the seed rows appear normally in transaction lists. Consequence: any transaction manually assigned to the `@Opening` category is likewise excluded from flow reports — the category carries the semantic.
- All `*_home_cents` fields are pre-converted by the engine. Clients never compute currency conversions.
- `bank_accounts[].current_balance_home_cents` and `people[].current_balance_home_cents` are `Optional[int]`. They are always populated for same-currency accounts (identity rate). For cross-currency accounts, they are `null` only when no exchange rate is available from the account's currency to `main_currency` for today's date (today resolved in the user's `display_timezone` via `exchange_rate.rate_lookup_date`, matching the reports) — in that case, clients should display the native balance as a fallback.
- "Current month" means `[first_day_of_month, last_day_of_month]` in the user's `display_timezone`.
- `?debit_as_negative=true` is accepted for API consistency with other read endpoints but is a no-op here — dashboard aggregates are already signed by construction (per-category `spent_home_cents` is positive for income and negative for expense; totals return split positive `inflow_home_cents`/`outflow_home_cents` with `net_home_cents` as their difference).

**`?include_archived=true`** — when set, the response's `archived_accounts` field is populated (same row shape as `bank_accounts`; `is_person = true` excluded); when false (the default) it is `null` per the null-over-omission rule. `current_balance_cents` is the lifetime balance — no further transactions can land on archived rows in clients that respect the picker.

*(The former `archived_categories` / `archived_hashtags` lifetime panels are gone: archiving a category was never a distinct feature — soft delete already hides a row from pickers while leaving its history intact — and these panels were the `is_archived` columns' last readers. An archived **account** is different: it still holds real money, which is why that one panel survives. `sql/024`.)*

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
      "spent_home_cents": -50000,
      "unconverted_count": 0,
      "hashtag_breakdown": [
        { "hashtag_ids": ["<lunch_id>", "<work_id>"], "spent_home_cents": -30000, "unconverted_count": 0 },
        { "hashtag_ids": ["<groceries_id>"], "spent_home_cents": -15000, "unconverted_count": 0 },
        { "hashtag_ids": [], "spent_home_cents": -5000, "unconverted_count": 0 }
      ]
    }
  ],
  "totals": {
    "inflow_home_cents": 800000,
    "outflow_home_cents": 320000,
    "net_home_cents": 480000,
    "unconverted_count": 0
  }
}
```

`categories` and `hashtag_breakdown` follow the exact same rules as `/dashboard` (home-currency only, nullable + `unconverted_count`, every non-deleted category included, breakdown rows sum to the parent category total, `hashtag_ids: []` for transactions with no hashtags). `totals` uses the same inflow/outflow/net structure. The report and the dashboard share one implementation (`compute_month_flow`), so the shapes cannot drift.

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

**Response fields:** each activity row includes `id`, `user_id`, `resource_type`, `resource_id`, `action`, `before_snapshot`, `after_snapshot`, `changed_by` (the user-id anchor), and `created_at`. (`actor_type` was dropped in `sql/024` — every writer only ever passed `"user"`; the multi-actor future it encoded does not exist at one user with no worker.)

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
2. *(Retired 2026-08-01)* The `recalculate_home_currency` bulk-UPDATE exception is gone with the helper — and since `sql/021` there is no stored conversion anywhere for any path to rewrite. Number kept so exception 3 is not silently renumbered against older references.
3. **`users.last_login_at` bumps** on repeat bootstrap calls are NOT logged. Login bumps are operational metadata, not user actions worth auditing. If session-level audit becomes a requirement, the right home is a dedicated `auth_sessions` table, not inflated `activity_log`.

---

## Exchange Rates

### `GET /exchange-rates`
**Query params:** `base` (default `USD`), `target`, `date` (ISO date; default: today in the user's `display_timezone` — `exchange_rate.rate_lookup_date`, the same "today" every current-date rate lookup uses since 2026-08-06; an invalid stored zone falls back to UTC)

Returns the rate for the given pair and date. Falls back to the most recent available rate if no exact match exists for the requested date.

**Errors:** a `base` or `target` not in `global_currencies` is a bad *input*, not a missing *resource* — `422 VALIDATION_ERROR` with the failing field(s), the same treatment the write paths give an unsupported `currency_code` (both codes are checked, both can appear in `fields`). `404 NOT_FOUND` is reserved for a supported pair with genuinely no rate row on or before the requested date. *(Amended 2026-08-07 — previously an unsupported currency also fell through to `404`.)*

Used internally by the engine. Also exposed for CLI use.

### `GET /exchange-rates/history`
**Query params:** `date` (optional ISO date — exact-day filter), `limit` (`[1, 200]`, default 50), `offset` (`≥ 0`, default 0)

Lists the stored `exchange_rates` rows, newest first, in the standard pagination envelope (`items` / `total` / `limit` / `offset`). Each item:

```json
{ "base": "USD", "target": "PEN", "rate_date": "2026-07-05", "rate": 3.6 }
```

- **Ordering:** `rate_date DESC, base ASC, target ASC` — pairs interleave deterministically within a day.
- **No fallback semantics**, unlike the lookup above: this returns exactly the rows that exist. A `date` with no rows (or an empty table) returns `items: []` with `total: 0` — not an error.
- `rate` = units of `target` per 1 `base`, serialized as a JSON number — same convention and serialization as the lookup.
- The `UNIQUE (base_currency, target_currency, rate_date)` constraint guarantees one row per pair per day; clients render rows verbatim, no dedup on either side.
- Read-only reference data (same posture as `GET /activity`): standard auth, standard error envelope.
